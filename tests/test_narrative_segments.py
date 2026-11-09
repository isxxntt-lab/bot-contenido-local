from __future__ import annotations

import narrative_segments as ns
from transcripts import STORY, build, word_at


def settings(**overrides):
    base = dict(ns.DEFAULTS)
    base.update(overrides)
    return base


def test_frases_y_unidades():
    words = build(STORY)
    sentences = ns.split_sentences(words, 0.6, total=words[-1].end + 2)
    assert len(sentences) == 8
    assert sentences[3].text == "¡NOOO!"
    units = ns.group_units(sentences, 1.5)
    assert len(units) == 2  # la charla de los subs va aparte (3 s de pausa)
    assert (units[0].first_sentence, units[0].last_sentence) == (0, 5)


def test_pausa_larga_cierra_frase_sin_puntuacion():
    words = build([("hola qué tal", 0.0), ("esto es otra cosa", 0.8)])
    assert len(ns.split_sentences(words, 0.6)) == 2


def test_expande_a_frases_completas_con_preparacion_y_remate():
    words = build(STORY)
    peak = word_at(words, "NOOO").start + 0.1
    total = words[-1].end + 2
    segment = ns.segment_for_peak(words, peak, settings(segundos_min=8, segundos_max=30), total)
    first, last = words[segment.first_word], words[segment.last_word]
    assert first.text in ("Vale", "Estaba", "Y")  # empieza al principio de una frase
    assert last.text.endswith(".") or last.text.endswith("!")
    assert segment.start <= peak - 3.0 and segment.end >= peak + 2.0
    assert segment.complete_start and segment.complete_end
    assert "subs" not in " ".join(w.text for w in words[segment.first_word : segment.last_word + 1])
    # Los bordes caen entre palabras: ninguna palabra queda cortada.
    for word in words:
        assert not (word.start < segment.start < word.end)
        assert not (word.start < segment.end < word.end)


def test_respeta_maximo():
    words = build(STORY)
    peak = word_at(words, "NOOO").start
    segment = ns.segment_for_peak(words, peak, settings(segundos_min=1, segundos_max=6, preparacion_segundos=10, remate_segundos=10))
    assert segment.length <= 6 + 0.5
    assert segment.start <= peak <= segment.end


def test_minimo_suma_frases_vecinas():
    words = build(STORY)
    peak = word_at(words, "NOOO").start
    short = ns.segment_for_peak(words, peak, settings(segundos_min=0.2, segundos_max=40, preparacion_segundos=0, remate_segundos=0))
    longer = ns.segment_for_peak(words, peak, settings(segundos_min=12, segundos_max=40, preparacion_segundos=0, remate_segundos=0))
    assert short.sentences == 1 and short.length < 3
    assert longer.length >= 12 and longer.sentences > short.sentences


def test_minimo_imposible_se_anota():
    words = build([("Hola chat.", 0.0), ("¡Qué fuerte!", 0.2)])
    segment = ns.segment_for_peak(words, words[-1].start, settings(segundos_min=20), total=words[-1].end + 1)
    assert any("segundos_min" in note for note in segment.notes)


def test_frase_cortada_por_el_clip_no_cuenta_como_completa():
    # El clip empieza con la voz en marcha y acaba sin cerrar la frase.
    words = build([("de lo que te decía antes", 0.0), ("¡Mira eso!", 0.8), ("y entonces el tío va y", 0.2)], start=0.0)
    total = words[-1].end + 0.05
    segment = ns.segment_for_peak(words, words[6].start, settings(segundos_min=0.2), total)
    assert segment.complete_start and segment.complete_end  # se queda en «¡Mira eso!»
    whole = ns.segment_for_peak(words, words[0].start, settings(segundos_min=1), total)
    assert not whole.complete_start


def test_sin_palabras():
    segment = ns.segment_for_peak([], 5.0, settings(), total=20.0)
    assert segment.first_word is None and "sin transcripción" in segment.notes


def test_settings_por_defecto_y_valores_raros():
    assert ns.settings_from({})["max_momentos"] == 1
    assert ns.settings_from({"seleccion": {"max_momentos": 0}})["max_momentos"] == 1
    assert ns.settings_from({"seleccion": {"segundos_min": "x"}})["segundos_min"] == 10.0
    assert ns.settings_from({"seleccion": {"segundos_min": 30, "segundos_max": 5}})["segundos_max"] == 30
