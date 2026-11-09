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
    segment = ns.Segment(0.0, 3.0, 1.0, 0, 3, complete_start=True, complete_end=True)
    relaxed = {"seleccion": {"segundos_min": 1, "juez": {"obligatorios": [], "puntuacion_min": 0}}}
    assert clip_judge.judge(words, segment, config=relaxed).accepted
    assert clip_judge.settings_from({"seleccion": {"juez": {"obligatorios": ["inventado"]}}})["obligatorios"] == []
