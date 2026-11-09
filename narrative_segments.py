"""Unidades narrativas a partir de la transcripción con tiempos por palabra.

faster-whisper da cada palabra con su inicio y su fin. Aquí se agrupan en:

- frases: se cierran con puntuación final (. ! ? …) o con una pausa de al menos
  ``pausa_frase`` segundos;
- unidades narrativas: frases seguidas sin una pausa larga (``pausa_unidad``),
  es decir, la misma intervención con continuidad.

``segment_for_peak`` devuelve el tramo que contiene el pico: arranca en el inicio
de una frase con algo de preparación antes del pico y termina al final de una
frase con el remate después, dentro de una duración mínima y máxima. Nunca corta
una palabra por la mitad.

Funciona con cualquier objeto que tenga ``text``, ``start`` y ``end`` (como
``CaptionCue``) y no depende de modelos ni de red.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

SENTENCE_END = (".", "!", "?", "…")

DEFAULTS: dict = {
    "max_momentos": 1,
    "candidatos_a_evaluar": 4,
    "segundos_min": 10.0,
    "segundos_max": 45.0,
    "preparacion_segundos": 3.0,
    "remate_segundos": 2.0,
    "pausa_frase": 0.6,
    "pausa_unidad": 1.5,
    "margen_inicio": 0.15,
    "margen_final": 0.35,
    "silencio_corte": 1.0,
    "silencio_margen": 0.3,
    "union_max_hueco_segundos": 5.0,
    "borde_clip_segundos": 0.3,
}

_INTEGERS = ("max_momentos", "candidatos_a_evaluar")


def settings_from(config: dict | None) -> dict:
    """Bloque "seleccion" de clipazos.json con valores seguros para lo que falte."""
    raw = (config or {}).get("seleccion")
    raw = raw if isinstance(raw, dict) else {}
    settings: dict = {}
    for key, default in DEFAULTS.items():
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            value = default
        settings[key] = int(value) if key in _INTEGERS else float(value)
    settings["max_momentos"] = max(1, settings["max_momentos"])
    settings["candidatos_a_evaluar"] = max(1, settings["candidatos_a_evaluar"])
    if settings["segundos_max"] < settings["segundos_min"]:
        settings["segundos_max"] = settings["segundos_min"]
    return settings


@dataclass
class Sentence:
    first: int
    last: int
    start: float
    end: float
    text: str
    closed: bool  # termina con puntuación o pausa (no es el final cortado del clip)
    clean_start: bool = True  # no empieza con la voz ya en marcha al inicio del clip


@dataclass
class Unit:
    first_sentence: int
    last_sentence: int
    start: float
    end: float


@dataclass
class Segment:
    start: float
    end: float
    peak: float
    first_word: int | None
    last_word: int | None
    first_word_start: float | None = None
    last_word_end: float | None = None
    complete_start: bool = False
    complete_end: bool = False
    sentences: int = 0
    crosses_units: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def length(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        data = asdict(self)
        data["duracion"] = round(self.length, 2)
        return data


def _ends_sentence(text: str) -> bool:
    return text.rstrip().rstrip("\"'»”)").endswith(SENTENCE_END)


def split_sentences(
    words: list,
    pause: float = DEFAULTS["pausa_frase"],
    total: float | None = None,
    edge: float = DEFAULTS["borde_clip_segundos"],
) -> list[Sentence]:
    """Frases por puntuación final o pausa larga.

    La primera frase está «abierta» si la voz ya sonaba al empezar el clip (empieza
    a menos de ``edge`` segundos del inicio). La última solo está cerrada si tiene
    puntuación: el silencio que queda al final del archivo no es un punto.
    """
    sentences: list[Sentence] = []
    first = 0
    for index, word in enumerate(words):
        following = words[index + 1] if index + 1 < len(words) else None
        gap = (following.start - word.end) if following is not None else None
        punctuated = _ends_sentence(word.text)
        if following is None or punctuated or (gap is not None and gap >= pause):
            chunk = words[first : index + 1]
            sentences.append(
                Sentence(
                    first, index, chunk[0].start, chunk[-1].end,
                    " ".join(item.text.strip() for item in chunk),
                    closed=punctuated or following is not None,
                    clean_start=bool(first > 0 or chunk[0].start >= edge),
                )
            )
            first = index + 1
    return sentences


def group_units(sentences: list[Sentence], unit_pause: float = DEFAULTS["pausa_unidad"]) -> list[Unit]:
    """Frases seguidas sin una pausa larga forman la misma unidad narrativa."""
    units: list[Unit] = []
    for index, sentence in enumerate(sentences):
        if units and sentence.start - sentences[index - 1].end < unit_pause:
            units[-1].last_sentence = index
            units[-1].end = sentence.end
        else:
            units.append(Unit(index, index, sentence.start, sentence.end))
    return units


def _unit_of(units: list[Unit], sentence_index: int) -> int:
    for number, unit in enumerate(units):
        if unit.first_sentence <= sentence_index <= unit.last_sentence:
            return number
    return 0


def _peak_sentence(sentences: list[Sentence], peak: float) -> int:
    for index, sentence in enumerate(sentences):
        if sentence.start <= peak <= sentence.end:
            return index
    before = [index for index, sentence in enumerate(sentences) if sentence.end < peak]
    return before[-1] if before else 0


def segment_for_peak(
    words: list,
    peak: float,
    settings: dict | None = None,
    total: float | None = None,
) -> Segment:
    """Tramo de frases completas que contiene el pico, con preparación y remate.

    1. Parte de la frase que contiene el pico (o la anterior si cae en un silencio).
    2. Añade frases antes hasta cubrir ``preparacion_segundos`` antes del pico y
       frases después hasta cubrir ``remate_segundos`` tras el pico, sin salir de
       la unidad narrativa y sin pasar de ``segundos_max``.
    3. Si aún no llega a ``segundos_min``, suma frases vecinas (primero de la misma
       unidad, luego de la siguiente) mientras quepa en ``segundos_max``.
    4. Los bordes caen entre palabras: un margen corto antes de la primera y
       después de la última, sin pisar la palabra vecina.
    """
    s = dict(DEFAULTS)
    s.update(settings or {})
    minimum, maximum = float(s["segundos_min"]), float(s["segundos_max"])
    notes: list[str] = []
    if not words:
        end = total if total is not None else peak + s["remate_segundos"]
        start = max(0.0, peak - s["preparacion_segundos"])
        return Segment(start, max(start, end), peak, None, None, notes=["sin transcripción"])

    sentences = split_sentences(words, s["pausa_frase"], total, s["borde_clip_segundos"])
    units = group_units(sentences, s["pausa_unidad"])
    core = _peak_sentence(sentences, peak)
    first = last = core
    home = _unit_of(units, core)

    def span(a: int, b: int) -> float:
        return sentences[b].end - sentences[a].start

    def usable(index: int) -> bool:
        # Una frase cortada por el borde del clip no se añade para rellenar.
        return sentences[index].clean_start and sentences[index].closed

    if span(first, last) > maximum:
        notes.append("la frase del pico es más larga que segundos_max; se recorta por palabras")
        return _trim_long_sentence(words, sentences[core], peak, s, total, notes)

    # Preparación antes del pico, dentro de la misma unidad.
    while (
        first > 0
        and sentences[first].start > peak - s["preparacion_segundos"]
        and _unit_of(units, first - 1) == home
        and usable(first - 1)
        and span(first - 1, last) <= maximum
    ):
        first -= 1
    # Remate después del pico, dentro de la misma unidad.
    while (
        last + 1 < len(sentences)
        and sentences[last].end < peak + s["remate_segundos"]
        and _unit_of(units, last + 1) == home
        and usable(last + 1)
        and span(first, last + 1) <= maximum
    ):
        last += 1
    # Duración mínima: primero la misma unidad, después las vecinas.
    crosses = False
    for same_unit_only in (True, False):
        while span(first, last) < minimum:
            options = []
            if last + 1 < len(sentences) and usable(last + 1) and span(first, last + 1) <= maximum:
                if not same_unit_only or _unit_of(units, last + 1) == home:
                    options.append(("after", sentences[last + 1].start - sentences[last].end))
            if first > 0 and usable(first - 1) and span(first - 1, last) <= maximum:
                if not same_unit_only or _unit_of(units, first - 1) == home:
                    options.append(("before", sentences[first].start - sentences[first - 1].end))
            if not options:
                break
            # La frase más pegada gana; a igualdad, la de después (el remate).
            options.sort(key=lambda item: (item[1], item[0] != "after"))
            if options[0][0] == "after":
                last += 1
            else:
                first -= 1
            if _unit_of(units, first) != home or _unit_of(units, last) != home:
                crosses = True
    if span(first, last) < minimum:
        notes.append("no hay frases suficientes para llegar a segundos_min")
    if crosses:
        notes.append("une frases de otra unidad narrativa para llegar al mínimo")

    first_word = sentences[first].first
    last_word = sentences[last].last
    start, end = _edges(words, first_word, last_word, s, total)
    complete_start = sentences[first].clean_start
    complete_end = sentences[last].closed
    return Segment(
        start, end, peak, first_word, last_word,
        first_word_start=words[first_word].start,
        last_word_end=words[last_word].end,
        complete_start=bool(complete_start),
        complete_end=bool(complete_end),
        sentences=last - first + 1,
        crosses_units=crosses,
        notes=notes,
    )


def _edges(words: list, first_word: int, last_word: int, s: dict, total: float | None) -> tuple[float, float]:
    start = words[first_word].start - s["margen_inicio"]
    if first_word > 0:
        start = max(start, words[first_word - 1].end)
    end = words[last_word].end + s["margen_final"]
    if last_word + 1 < len(words):
        end = min(end, words[last_word + 1].start)
    if total is not None:
        end = min(end, total)
    return max(0.0, start), end


def _trim_long_sentence(words, sentence: Sentence, peak: float, s: dict, total, notes) -> Segment:
    """Frase larguísima: se recorta por palabras alrededor del pico (marcado incompleto)."""
    maximum = float(s["segundos_max"])
    indices = list(range(sentence.first, sentence.last + 1))
    lo = hi = min(indices, key=lambda i: abs(words[i].start - peak))
    while True:
        grew = False
        if lo > sentence.first and words[hi].end - words[lo - 1].start <= maximum and words[lo].start > peak - s["preparacion_segundos"]:
            lo -= 1
            grew = True
        if hi < sentence.last and words[hi + 1].end - words[lo].start <= maximum:
            hi += 1
            grew = True
        if not grew:
            break
    start, end = _edges(words, lo, hi, s, total)
    return Segment(
        start, end, peak, lo, hi,
        first_word_start=words[lo].start, last_word_end=words[hi].end,
        complete_start=lo == sentence.first and sentence.clean_start,
        complete_end=hi == sentence.last and sentence.closed,
        sentences=1, notes=notes,
    )
