from __future__ import annotations

import clip_judge
import narrative_segments as ns
from transcripts import STORY, build, word_at

CONFIG = {"seleccion": {"segundos_min": 8, "segundos_max": 40}}


def test_acepta_un_momento_con_arco():
    words = build(STORY)
    peak = word_at(words, "NOOO").start
    segment = ns.segment_for_peak(words, peak, ns.settings_from(CONFIG), total=words[-1].end + 2)
    verdict = clip_judge.judge(words, segment, config=CONFIG)
    assert verdict.accepted, verdict.reasons
    assert verdict.score >= 0.9
    assert set(verdict.criteria) == set(clip_judge.WEIGHTS)
    assert all("motivo" in item for item in verdict.criteria.values())


def test_descarta_un_momento_cortado():
    words = build(STORY)
    peak = word_at(words, "NOOO").start
    # Tramo a mano: empieza a mitad de frase y acaba justo en el grito.
    cut = ns.Segment(
        start=word_at(words, "repente").start - 0.05,
        end=word_at(words, "NOOO").end,
        peak=peak,
        first_word=words.index(word_at(words, "repente")),
        last_word=words.index(word_at(words, "NOOO")),
        complete_start=False,
        complete_end=True,
    )
    verdict = clip_judge.judge(words, cut, config=CONFIG)
    assert not verdict.accepted
    text = verdict.reason
    assert "inicio_completo" in text and "remate" in text and "duracion" in text


def test_autocontencion_y_hook():
    words = build([("pero entonces dijo que", 2.0)])
    segment = ns.Segment(0.0, words[-1].end + 0.3, words[0].start, 0, len(words) - 1, complete_start=True, complete_end=True)
    verdict = clip_judge.judge(words, segment, config=CONFIG)
    assert verdict.criteria["autocontencion"]["puntos"] == 0.0
    assert verdict.criteria["hook"]["puntos"] < 1.0


def test_sin_voz_se_descarta():
    segment = ns.Segment(0.0, 15.0, 5.0, None, None)
    verdict = clip_judge.judge([], segment, config=CONFIG)
    assert not verdict.accepted


def test_obligatorios_configurables():
    words = build(STORY)
    end_word = word_at(words, "hoy")
    segment = ns.Segment(
        0.0, end_word.end + 0.2, words[2].start, 0, words.index(end_word),
        complete_start=True, complete_end=True,
    )
    relaxed = {"seleccion": {"segundos_min": 1, "juez": {"obligatorios": [], "puntuacion_min": 0}}}
    verdict = clip_judge.judge(words, segment, config=relaxed)
    assert verdict.accepted, verdict.reasons
    assert clip_judge.settings_from({"seleccion": {"juez": {"obligatorios": ["inventado"]}}})["obligatorios"] == []


# Misma lista que clipazos.json: el cierre colgado no está en obligatorios y aun así rechaza.
PROD = {
    "seleccion": {
        "segundos_min": 10,
        "segundos_max": 45,
        "pausa_frase": 0.6,
        "pausa_unidad": 1.5,
        "juez": {
            "puntuacion_min": 0.6,
            "obligatorios": ["inicio_completo", "final_completo", "remate", "duracion"],
        },
    }
}


def _ending(last: str) -> list:
    return build([
        ("Vale chat os cuento lo que acaba de pasar en la partida final.", 0.0),
        ("El jefe sale por detrás y casi me mata del susto enorme.", 0.2),
        ("NOOO", 0.2),
        (f"Me ha dejado temblando y sin ninguna vida {last}", 0.2),
    ])


def test_por_con_puntuacion_de_asr_se_rechaza():
    for last in ("por", "por.", "por!", "por?", "por…", "por..."):
        words = _ending(last)
        peak = word_at(words, "NOOO").start
        segment = ns.segment_for_peak(words, peak, ns.settings_from(PROD), total=words[-1].end + 2)
        verdict = clip_judge.judge(words, segment, config=PROD)
        assert not verdict.accepted, (last, verdict.reasons, segment.to_dict())
        assert any(item.startswith("cierre_colgante:") for item in verdict.reasons), (last, verdict.reasons)
        if last != "por":
            assert verdict.criteria["final_completo"]["ok"] is True, last
        assert "cierre_colgante" not in PROD["seleccion"]["juez"]["obligatorios"]


def test_pario_en_la_misma_unidad_sigue_aceptado():
    """Diálogo de varias frases con pausa corta: una unidad, cierre de verdad."""
    words = build([
        ("Vale hoy el jefe final tiene la barra casi vacía.", 0.0),
        ("Salto esquivo el primer ataque y preparo el golpe.", 0.4),
        ("NOOO", 0.4),
        ("La puta que te parió!", 0.2),
    ])
    peak = word_at(words, "NOOO").start
    segment = ns.segment_for_peak(words, peak, ns.settings_from(PROD), total=words[-1].end + 2)
    verdict = clip_judge.judge(words, segment, length=segment.length, config=PROD)
    assert segment.crosses_units is False, segment.to_dict()
    assert verdict.criteria["cierre_colgante"]["ok"] is True
    assert verdict.decision == "aceptado", (verdict.reasons, verdict.abstentions, segment.to_dict())
    assert verdict.accepted


def test_frase_larga_en_por_y_otra_despues_no_se_acepta():
    larga = (
        "Vale chat mirad lo que me ha pasado hoy en la partida final del jefe "
        "porque salió por detrás y casi me deja sin ninguna vida en el intento "
        "y al final me suelta ostias por"
    )
    words = build([
        (larga, 0.0),
        ("Después el chat empieza a reírse de toda la jugada.", 2.0),
    ])
    peak = words[8].start
    segment = ns.segment_for_peak(words, peak, ns.settings_from(PROD), total=words[-1].end + 2)
    verdict = clip_judge.judge(words, segment, length=segment.length, config=PROD)
    assert words[segment.last_word].text == "por", (words[segment.last_word].text, segment.to_dict())
    assert segment.complete_end is True
    assert not verdict.accepted
    assert any(item.startswith("cierre_colgante:") for item in verdict.reasons)


def test_dos_unidades_unidas_para_durar_se_abstiene():
    words = build([
        ("Bueno vamos a empezar esta partida con mucha calma y sin ninguna prisa en el directo de hoy.", 0.0),
        ("Cada uno saca su celular y espera con calma el siguiente aviso antes de seguir jugando.", 2.0),
    ])
    peak = words[3].start
    segment = ns.segment_for_peak(words, peak, ns.settings_from(PROD), total=words[-1].end + 2)
    verdict = clip_judge.judge(words, segment, length=segment.length, config=PROD)
    assert segment.crosses_units is True, segment.to_dict()
    assert segment.length >= 10
    assert verdict.decision == "abstencion", (verdict.reasons, verdict.abstentions, segment.to_dict())
    assert not verdict.accepted
    assert verdict.score >= 0.6


def test_cola_de_silencio_no_cierra_ostias_por():
    words = build([("vaya ostias por", 0.0)])
    short = ns.split_sentences(words, 0.6, total=words[-1].end + 0.05)
    long = ns.split_sentences(words, 0.6, total=words[-1].end + 0.40)
    assert short[-1].closed is False and long[-1].closed is False


def test_intro_con_el_pico_al_principio_no_tiene_remate():
    words = build([(
        "Bueno vamos a hacerlo muy sencillo muy fácil y cada uno saca su celular ahora mismo para seguir.",
        0.0,
    )])
    peak = words[0].start
    segment = ns.segment_for_peak(words, peak, ns.settings_from(PROD), total=words[-1].end + 2)
    verdict = clip_judge.judge(words, segment, length=segment.length, config=PROD)
    assert not verdict.criteria["remate"]["ok"]
    assert not verdict.accepted
    assert any(item.startswith("remate:") for item in verdict.reasons)


def test_el_nexo_sin_tilde_no_se_confunde_con_la_palabra_acentuada():
    """Límite léxico: no es una prueba de que la frase tenga sentido."""
    samples = {
        "Sí.": False,
        "Si.": True,
        "¿Cómo?": False,
        "Como?": True,
        "él.": False,
        "el.": True,
        "por.": True,
        "por!": True,
        "que?": True,
        "Y?": True,
    }
    for text, dangling in samples.items():
        words = build([(text, 0.0)])
        assert clip_judge._dangling_close(words[-1].text) is dangling, text
    open_end = build([("todo queda resuelto", 0.0)])
    closed_end = build([("todo queda resuelto.", 0.0)])
    assert ns.split_sentences(open_end, 0.6, total=open_end[-1].end + 2)[-1].closed is False
    assert ns.split_sentences(closed_end, 0.6, total=closed_end[-1].end + 0.05)[-1].closed is True


# Ventana guardada del manifiesto 2040 de DuendePablo. No es una transcripción nueva.
DUENDE_2040 = {
    "start": 5.08,
    "end": 17.84,
    "peak": 16.75,
    "first_word": 5,
    "last_word": 30,
    "complete_start": True,
    "complete_end": True,
    "crosses_units": True,
    "notes": ["une frases de otra unidad narrativa para llegar al mínimo"],
}


def test_replay_del_segmento_guardado_de_duendepablo_se_abstiene():
    from video_editor import CaptionCue
    words = [
        CaptionCue("No", 6.0, 6.2), CaptionCue("puedo", 6.3, 6.6),
        CaptionCue("pasar", 6.7, 7.0), CaptionCue("esa", 7.1, 7.3),
        CaptionCue("parte", 7.4, 7.8), CaptionCue("salta", 15.0, 15.4),
        CaptionCue("parió!", 17.1, 17.49),
    ]
    segment = ns.Segment(
        DUENDE_2040["start"], DUENDE_2040["end"], DUENDE_2040["peak"],
        0, len(words) - 1,
        complete_start=DUENDE_2040["complete_start"],
        complete_end=DUENDE_2040["complete_end"],
        crosses_units=DUENDE_2040["crosses_units"],
        notes=list(DUENDE_2040["notes"]),
    )
    verdict = clip_judge.judge(words, segment, length=10.22, config=PROD)
    assert verdict.decision == "abstencion", (verdict.reasons, verdict.abstentions)
    assert verdict.criteria["cierre_colgante"]["ok"] is True
    assert not verdict.accepted
