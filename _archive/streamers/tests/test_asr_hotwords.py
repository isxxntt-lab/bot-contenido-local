"""hotwords de faster-whisper, detrás de asr.hotwords_activa (apagado por defecto)."""

from __future__ import annotations

import numpy as np

import clipazos


class ModeloFalso:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append({"audio": audio, "kwargs": kwargs})
        return [], None


def test_flag_apagado_deja_la_llamada_igual(monkeypatch):
    modelo = ModeloFalso()
    monkeypatch.setattr(clipazos, "_whisper", lambda: modelo)
    audio = np.zeros(160, dtype=np.float32)
    assert clipazos.transcribe(audio, "es") == []
    assert clipazos.transcribe(
        audio, "es", {"asr": {"hotwords_activa": False, "hotwords": ["joder", "hostia"]}}, "Ibai"
    ) == []
    actual = {"language": "es", "word_timestamps": True, "vad_filter": True}
    assert modelo.calls[0]["kwargs"] == actual
    assert modelo.calls[1]["kwargs"] == actual
    assert "hotwords" not in modelo.calls[0]["kwargs"]
    assert "prefix" not in modelo.calls[0]["kwargs"]


def test_flag_encendido_pasa_hotwords_sin_prefix(monkeypatch):
    modelo = ModeloFalso()
    monkeypatch.setattr(clipazos, "_whisper", lambda: modelo)
    audio = np.zeros(160, dtype=np.float32)
    config = {"asr": {"hotwords_activa": True, "hotwords": ["hostia", "joder", "hostia"]}}
    assert clipazos.transcribe(audio, "es", config, "@Ibai") == []
    kwargs = modelo.calls[0]["kwargs"]
    assert set(kwargs) == {"language", "word_timestamps", "vad_filter", "hotwords"}
    assert kwargs["hotwords"] == "hostia joder Ibai"
    assert "prefix" not in kwargs
    assert len(kwargs["hotwords"]) <= 200


def test_hotwords_reservan_el_streamer_y_no_pasan_de_200():
    config = {"asr": {"hotwords_activa": True, "hotwords": ["x" * 40] * 10}}
    text = clipazos.asr_hotwords(config, "ElMariana")
    assert text.endswith("ElMariana")
    assert len(text) <= 200
    assert clipazos.asr_hotwords({"asr": {"hotwords_activa": False, "hotwords": ["joder"]}}, "Ibai") is None


def test_hotwords_no_viajan_con_prefix():
    options = clipazos._opciones_transcripcion(
        "es", {"asr": {"hotwords_activa": True, "hotwords": ["joder"]}}, "Ibai", prefix="vale"
    )
    assert options["prefix"] == "vale"
    assert "hotwords" not in options
