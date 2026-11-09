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
        assert verdict.criteria["final_completo"]["ok"] is True
        assert "cierre_colgante" not in PROD["seleccion"]["juez"]["obligatorios"]


def test_pario_y_dialogo_con_dos_unidades_siguen_aceptados():
    words = build([
        ("Vale hoy el jefe final tiene la barra casi vacía.", 0.0),
        ("Salto esquivo el primer ataque y preparo el golpe.", 0.3),
        ("NOOO", 2.0),
        ("La puta que te parió!", 0.2),
    ])
    peak = word_at(words, "NOOO").start
    segment = ns.segment_for_peak(words, peak, ns.settings_from(PROD), total=words[-1].end + 2)
    verdict = clip_judge.judge(words, segment, length=segment.length, config=PROD)
    assert verdict.criteria["cierre_colgante"]["ok"] is True
    assert segment.crosses_units is True, segment.to_dict()
    assert any("mezcla dos intervenciones" in item for item in verdict.warnings)
    assert not any(item.startswith("cierre_colgante:") for item in verdict.reasons)
    assert verdict.accepted, (verdict.reasons, verdict.warnings, segment.to_dict())
