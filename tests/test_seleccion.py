from __future__ import annotations

import json
import shutil

import numpy as np
import pytest

import clipazos
from conftest import ROOT
from transcripts import STORY, build, word_at

ALLOWED = {"rights": {"streamers": {"ana": "allowed", "beto": "allowed"}}, "qa": {"duracion_min": 1}}


def cand(name="Ana", key="twitch-a", vod=None, offset=None):
    candidate = clipazos.Candidate("Twitch", name, "Momentazo", 1000, "u", key)
    candidate.vod_id, candidate.offset = vod, offset
    return candidate


def fake_moment(candidate, length=12.0, accepted=True, first=1.0, last=12.0):
    moment = clipazos.Moment(candidate, ROOT / "x.mp4", 0.5, length, [])
    moment.segment = {"end": 0.5 + length, "first_word_start": first, "last_word_end": last, "notes": []}
    moment.judgement = {"aceptado": accepted, "puntuacion": 0.95 if accepted else 0.3,
                        "motivos": [] if accepted else ["remate: acaba en el pico"], "criterios": {}}
    return moment


@pytest.fixture(autouse=True)
def clean_log():
    clipazos._SELECTION_LOG.clear()
    yield
    clipazos._SELECTION_LOG.clear()


def test_config_real_tiene_un_solo_momento():
    config = json.loads((ROOT / "clipazos.json").read_text(encoding="utf-8"))
    assert config["seleccion"]["max_momentos"] == 1
    assert config["subida_youtube"]["privacidad"] == "public"


def test_un_solo_momento_por_defecto(monkeypatch):
    built = []

    def fake_build(candidate, *args, **kwargs):
        built.append(candidate.key)
        return fake_moment(candidate)

    monkeypatch.setattr(clipazos, "build_moment", fake_build)
    pool = [cand("Ana", "twitch-a"), cand("Ana", "twitch-b"), cand("Ana", "twitch-c")]
    moments = clipazos.collect_moments(pool, ALLOWED, "es")
    assert [m.candidate.key for m in moments] == ["twitch-a"]
    assert built == ["twitch-a"]  # no descarga más clips para «rellenar» 40 s


def test_descartados_por_el_juez_y_siguiente_candidato(monkeypatch):
    verdicts = iter([False, True])
    monkeypatch.setattr(clipazos, "build_moment", lambda c, *a, **k: fake_moment(c, accepted=next(verdicts)))
    moments = clipazos.collect_moments([cand(key="twitch-a"), cand(key="twitch-b")], ALLOWED, "es")
    assert [m.candidate.key for m in moments] == ["twitch-b"]
    log = clipazos._SELECTION_LOG
    assert [entry["estado"] for entry in log] == ["descartado", "aceptado"]
    assert log[0]["puntuacion"] == 0.3 and log[0]["motivos"] and log[0]["aceptado"] is False


def test_presupuesto_de_candidatos(monkeypatch):
    calls = []
    monkeypatch.setattr(clipazos, "build_moment", lambda c, *a, **k: calls.append(c.key) or fake_moment(c, accepted=False))
    config = {**ALLOWED, "seleccion": {"candidatos_a_evaluar": 2}}
    assert clipazos.collect_moments([cand(key=f"k{i}") for i in range(5)], config, "es") == []
    assert len(calls) == 2


def test_no_concatena_temas_distintos(monkeypatch):
    monkeypatch.setattr(clipazos, "build_moment", lambda c, *a, **k: fake_moment(c))
    config = {**ALLOWED, "seleccion": {"max_momentos": 3}, "segundos_objetivo": 60}
    pool = [
        cand("Ana", "twitch-a", vod="v1", offset=100.0),
        cand("Beto", "twitch-b", vod="v2", offset=10.0),     # otro streamer
        cand("Ana", "twitch-c", vod="v1", offset=900.0),     # mismo directo, otro momento
        cand("Ana", "twitch-d", vod="v1", offset=112.0),     # justo a continuación del primero
    ]
    moments = clipazos.collect_moments(pool, config, "es")
    assert [m.candidate.key for m in moments] == ["twitch-a", "twitch-d"]
    states = {entry["clip"]: entry["estado"] for entry in clipazos._SELECTION_LOG}
    assert states == {"twitch-a": "aceptado", "twitch-b": "no_unido", "twitch-c": "no_unido", "twitch-d": "aceptado"}


def test_same_story_sin_vod_no_une():
    joined, why = clipazos.same_story(fake_moment(cand(key="a")), fake_moment(cand(key="b")), {})
    assert not joined and "mismo directo" in why


def test_make_short_con_un_momento_y_seleccion_en_el_reporte(tmp_path, videos, monkeypatch):
    monkeypatch.setattr(clipazos, "build_moment", lambda c, *a, **k: fake_moment(c))

    rendered = []

    def fake_render(moments, _title, _channel, destination):
        rendered.append(len(moments))
        shutil.copy(videos["ok"], destination)
        shutil.copy(videos["ok"].with_suffix(".ass"), destination.with_suffix(".ass"))
        return destination

    monkeypatch.setattr(clipazos, "render_short", fake_render)
    post = clipazos.make_short([cand(key="twitch-a"), cand(key="twitch-b")], ALLOWED, [[("X", False)]], "Titular", tmp_path / "s.mp4", [])
    assert post is not None and rendered == [1]
    assert post["clips"] == ["twitch-a"]
    assert post["seleccion"][0]["aceptado"] is True and post["seleccion"][0]["puntuacion"] == 0.95
    assert post["qa"]["ok"] is True


def test_make_short_sin_momentos_devuelve_none(monkeypatch):
    monkeypatch.setattr(clipazos, "build_moment", lambda c, *a, **k: fake_moment(c, accepted=False))
    assert clipazos.make_short([cand()], ALLOWED, [[("X", False)]], "T", ROOT / "nunca.mp4", []) is None


def test_select_segment_con_transcripcion_sintetica(monkeypatch):
    words = build(STORY)
    peak_word = word_at(words, "NOOO")
    total = words[-1].end + 2.0
    audio = np.zeros(int(total * clipazos.SAMPLE_RATE), dtype=np.float32)
    monkeypatch.setattr(clipazos, "find_peak", lambda _audio, _cues: peak_word.start + 0.1)
    config = {"seleccion": {"segundos_min": 8, "segundos_max": 40}}
    segment, length, cues, peak, cuts, verdict = clipazos.select_segment(audio, words, config)
    assert verdict.accepted, verdict.reasons
    assert cues[0].text in ("Vale", "Estaba", "Y") and cues[-1].text.endswith((".", "!"))
    assert 0 <= peak <= length
    assert cuts and all(right > left for left, right in cuts)


def test_tighten_no_corta_dentro_de_una_frase():
    # Pausa de 1,5 s a mitad de frase (sin puntuación) y otra igual tras un punto.
    words = build([("estaba yo", 0.0), ("pensando en eso.", 1.5), ("Luego vino él.", 1.5)], start=0.0)
    span = words[-1].end + 0.2
    ranges = clipazos._keep_ranges(span, words, cut=1.0, keep=0.3, sentence_only=True)
    assert len(ranges) == 2  # solo un corte: el de después del punto
    old = clipazos._keep_ranges(span, words)  # comportamiento antiguo: corta las dos
    assert len(old) == 3


def test_flag_clips_sube_max_momentos(monkeypatch, tmp_path):
    config = {"canal": "C"}
    monkeypatch.setattr(clipazos, "load_config", lambda path: config)

    def stop(*args, **kwargs):
        raise SystemExit(0)

    monkeypatch.setattr(clipazos, "clean_cache", stop)
    monkeypatch.setattr(clipazos, "CLIPAZOS_DIR", tmp_path)
    with pytest.raises(SystemExit):
        clipazos.main(["--clips", "2", "--forzar"])
    assert config["seleccion"]["max_momentos"] == 2
