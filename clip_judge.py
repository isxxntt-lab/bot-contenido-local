"""Juez local de momentos: puntúa si un tramo tiene un arco completo.

Versión simple, sin LLM ni red. Cada criterio da de 0 a 1 punto con un motivo
legible; la puntuación final es la media ponderada. Un momento se descarta si
falla un criterio obligatorio (por defecto inicio, final, remate y duración) o
si no llega a ``puntuacion_min``.

Criterios:
- hook: hay voz desde el primer segundo y varias palabras en los primeros
  ``hook_segundos``.
- inicio_completo: el tramo empieza al inicio de una frase (no con la voz del
  clip ya en marcha).
- final_completo: el tramo acaba al cerrar una frase (puntuación o pausa), no
  cortado por el final del clip.
- remate: tras el pico quedan al menos ``remate_min_segundos`` y alguna palabra.
- autocontencion: no empieza con un conector («y», «pero», «porque»...), no
  acaba colgando («de», «que», «el»...) y tiene al menos ``min_palabras``.
  Cruzar dos unidades solo avisa: un diálogo seguido puede hacerlo.
- cierre_colgante: la última palabra es gramatical («por», «de», «que»...),
  aunque el reconocedor le pegue «.», «!», «?» o puntos suspensivos. Pesa 0
  para no mover la nota del resto y, aun así, rechaza siempre: no depende de
  ``obligatorios`` ni de la puntuación. No decide si la historia tiene causa
  o remate; eso siguen midiéndolo final_completo (pausa o puntuación) y
  remate (tiempo y alguna palabra tras el pico).
- duracion: la duración final está entre seleccion.segundos_min y segundos_max.
- arco: informativo, peso 0. Al menos 3 frases, voz 6 s antes del pico y una
  proporción de voz. No rechaza por sí solo.

Es una heurística sobre la transcripción: no entiende el chiste ni el contexto.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import narrative_segments

WEIGHTS = {
    "hook": 0.2,
    "inicio_completo": 0.2,
    "final_completo": 0.2,
    "remate": 0.2,
    "autocontencion": 0.1,
    "duracion": 0.1,
    # Peso 0: no cambia la media. El rechazo de cierre_colgante va aparte.
    "cierre_colgante": 0.0,
    # Peso 0 e informativo: no entra en el rechazo si no está en obligatorios.
    "arco": 0.0,
}

DEFAULTS: dict = {
    "puntuacion_min": 0.6,
    "hook_segundos": 3.0,
    "hook_palabras_min": 3,
    "remate_min_segundos": 1.0,
    "min_palabras": 6,
    "obligatorios": ["inicio_completo", "final_completo", "remate", "duracion"],
}

START_CONNECTORS = {
    "y", "e", "pero", "porque", "pues", "entonces", "aunque", "además", "ademas",
    "también", "tambien", "osea", "sino", "que", "o",
}
DANGLING_ENDS = {
    "y", "e", "o", "u", "pero", "porque", "que", "de", "del", "a", "al", "en", "con",
    "por", "para", "el", "la", "los", "las", "un", "una", "mi", "tu", "su", "le", "lo",
    "se", "me", "te", "si", "como", "cuando", "donde",
}


def settings_from(config: dict | None) -> dict:
    """Sub-bloque "seleccion.juez" con valores seguros."""
    selection = (config or {}).get("seleccion")
    raw = selection.get("juez") if isinstance(selection, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    settings = dict(DEFAULTS)
    for key in ("puntuacion_min", "hook_segundos", "hook_palabras_min", "remate_min_segundos", "min_palabras"):
        value = raw.get(key, DEFAULTS[key])
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            value = DEFAULTS[key]
        settings[key] = value
    required = raw.get("obligatorios", DEFAULTS["obligatorios"])
    if not isinstance(required, list):
        required = DEFAULTS["obligatorios"]
    settings["obligatorios"] = [name for name in required if name in WEIGHTS]
    return settings


@dataclass
class Judgement:
    accepted: bool
    score: float
    criteria: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    abstentions: list[str] = field(default_factory=list)
    decision: str = "rechazo"

    def to_dict(self) -> dict:
        return {
            "aceptado": self.accepted,
            "decision": self.decision,
            "puntuacion": round(self.score, 3),
            "criterios": self.criteria,
            "motivos": self.reasons,
            "avisos": self.warnings,
            "abstenciones": self.abstentions,
        }

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def _plain(word: str) -> str:
    return re.sub(r"[^\wáéíóúñü]", "", word, flags=re.IGNORECASE).casefold()


def _dangling_close(text: str) -> bool:
    """«por», «por.» o «que?» siguen colgados. «Sí.», «¿Cómo?» y «él» no.

    La lista guarda la forma sin tilde (``si``, ``como``, ``el``). ``_plain``
    conserva la tilde, así que «Sí» no es ``si`` y «él» no es ``el``. Eso no
    prueba que la frase tenga sentido: solo separa el nexo escrito así del
    monosílabo acentuado. Una frase de contenido sin punto tampoco se da por
    cerrada; lo decide el segmentador, no esta lista.
    """
    return _plain(text) in DANGLING_ENDS


def judge(
    words: list,
    segment: narrative_segments.Segment,
    length: float | None = None,
    config: dict | None = None,
) -> Judgement:
    """Puntúa un tramo. ``words`` es la transcripción del clip entero (tiempos del clip).

    ``length`` es la duración final tras quitar silencios; si falta se usa la del tramo.
    """
    s = settings_from(config)
    selection = narrative_segments.settings_from(config)
    inside = [word for word in words if segment.start <= word.start < segment.end]
    duration = float(length if length is not None else segment.length)
    criteria: dict[str, dict] = {}

    def put(name: str, points: float, reason: str) -> None:
        criteria[name] = {"puntos": round(max(0.0, min(1.0, points)), 3), "ok": points >= 1.0 - 1e-9, "motivo": reason}

    # hook
    hook_end = segment.start + float(s["hook_segundos"])
    early = [word for word in inside if word.start < hook_end]
    first_gap = (inside[0].start - segment.start) if inside else None
    voice_now = first_gap is not None and first_gap <= 1.0
    enough = len(early) >= int(s["hook_palabras_min"])
    put(
        "hook",
        (0.5 if voice_now else 0.0) + (0.5 if enough else 0.0),
        f"{len(early)} palabras en los primeros {s['hook_segundos']:.0f} s"
        + (f", la voz entra a {first_gap:.1f} s" if first_gap is not None else ", sin voz"),
    )
    # inicio y final
    put(
        "inicio_completo",
        1.0 if inside and segment.complete_start else 0.0,
        "empieza al inicio de una frase" if inside and segment.complete_start else "empieza a mitad de frase o sin voz",
    )
    put(
        "final_completo",
        1.0 if inside and segment.complete_end else 0.0,
        "acaba al cerrar una frase" if inside and segment.complete_end else "acaba a mitad de frase (cortado)",
    )
    # remate
    after = segment.end - segment.peak
    words_after = [word for word in inside if word.start >= segment.peak]
    before = [word for word in inside if word.start < segment.peak - 1e-3]
    payoff = after >= float(s["remate_min_segundos"]) and bool(words_after) and bool(before)
    if not before:
        remate_motivo = "el pico abre el tramo; el tiempo posterior no demuestra un remate"
    elif payoff:
        remate_motivo = f"{after:.1f} s y {len(words_after)} palabras tras el pico, con voz antes"
    else:
        remate_motivo = f"acaba en el pico: {after:.1f} s y {len(words_after)} palabras después"
    put(
        "remate",
        1.0 if payoff else (0.5 if after >= float(s["remate_min_segundos"]) and bool(before) else 0.0),
        remate_motivo,
    )
    # autocontención
    points = 1.0
    notes: list[str] = []
    if inside and _plain(inside[0].text) in START_CONNECTORS:
        points -= 0.5
        notes.append(f"empieza con «{inside[0].text.strip()}»")
    dangling = bool(inside) and _dangling_close(inside[-1].text)
    if dangling:
        points -= 0.5
        notes.append(f"acaba colgando en «{inside[-1].text.strip()}»")
    if len(inside) < int(s["min_palabras"]):
        points = 0.0
        notes.append(f"solo {len(inside)} palabras")
    if segment.crosses_units:
        points -= 0.25
        notes.append("mezcla dos intervenciones")
    put("autocontencion", points, "; ".join(notes) or "se entiende solo")
    put(
        "cierre_colgante",
        0.0 if dangling else 1.0,
        f"la última palabra «{inside[-1].text.strip()}» no cierra" if dangling else "la última palabra no es un nexo colgado",
    )
    # arco: no mueve la nota. Sirve para leer en el reporte si había historia.
    frases = int(segment.sentences or 0)
    previos = [word for word in inside if word.start < segment.peak - 1e-3]
    planteo = (segment.peak - previos[0].start) if previos else 0.0
    voz = sum(max(0.0, word.end - word.start) for word in inside)
    proporcion = (voz / duration) if duration > 0 else 0.0
    arco_ok = frases >= 3 and planteo >= 6.0 and proporcion >= 0.35
    put(
        "arco",
        1.0 if arco_ok else 0.0,
        f"{frases} frases, {planteo:.1f} s antes del pico, voz {proporcion:.0%}",
    )
    # duración
    ok_length = selection["segundos_min"] <= duration <= selection["segundos_max"]
    put(
        "duracion",
        1.0 if ok_length else 0.0,
        f"{duration:.1f} s (rango {selection['segundos_min']:.0f}-{selection['segundos_max']:.0f} s)",
    )

    score = sum(WEIGHTS[name] * criteria[name]["puntos"] for name in WEIGHTS) / sum(WEIGHTS.values())
    reasons = [f"{name}: {criteria[name]['motivo']}" for name in s["obligatorios"] if not criteria[name]["ok"]]
    # Invariante: el JSON de producción lista otros obligatorios y no puede apagar esto.
    if not criteria["cierre_colgante"]["ok"]:
        reasons.append(f"cierre_colgante: {criteria['cierre_colgante']['motivo']}")
    if score < float(s["puntuacion_min"]):
        reasons.append(f"puntuación {score:.2f} < {float(s['puntuacion_min']):.2f}")
    reasons = list(dict.fromkeys(reasons))
    # Solo cuando el mínimo de duración obliga a traer otra unidad. Una pausa
    # corta, dentro de la misma unidad, no entra aquí.
    abstentions = []
    if segment.crosses_units:
        abstentions.append(
            "abstencion: se unió otra unidad solo para la duración mínima; no hay evidencia de una sola historia"
        )
    blocking = {item.split(":", 1)[0] for item in reasons}
    warnings = [
        f"{name}: {criteria[name]['motivo']}"
        for name, item in criteria.items()
        if not item["ok"] and name not in blocking
    ]
    accepted = not reasons and not abstentions
    decision = "aceptado" if accepted else "rechazo" if reasons else "abstencion"
    return Judgement(accepted, score, criteria, reasons, warnings, abstentions, decision)
