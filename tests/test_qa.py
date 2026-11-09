from __future__ import annotations

import pytest

import qa
from conftest import qa_config


def test_video_correcto_pasa(videos):
    result = qa.run_qa(videos["ok"], qa_config())
    assert result.passed, result.reasons
    assert result.metrics["ancho"] == 1080 and result.metrics["alto"] == 1920
    assert -16 <= result.metrics["lufs"] <= -12
    assert result.metrics["subtitulos"] == 4


def test_resolucion_fps_y_duracion(videos):
    assert any("resolución" in r for r in qa.run_qa(videos["horizontal"], qa_config()).reasons)
    assert any("fps" in r for r in qa.run_qa(videos["fps30"], qa_config()).reasons)
    assert any("duración" in r for r in qa.run_qa(videos["ok"], qa_config(duracion_min=10)).reasons)


def test_sin_audio_y_volumen_bajo(videos):
    assert "el vídeo no tiene audio" in qa.run_qa(videos["sin_audio"], qa_config()).reasons
    low = qa.run_qa(videos["bajo"], qa_config())
    assert not low.passed and any("LUFS" in r for r in low.reasons)


def test_negro_y_congelado(videos):
    assert any("negra" in r for r in qa.run_qa(videos["negro"], qa_config()).reasons)
    frozen = qa.run_qa(videos["congelado"], qa_config())
    assert any("congelada" in r for r in frozen.reasons), frozen.reasons


def test_subtitulos(videos, tmp_path):
    assert any("subtítulos" in r for r in qa.run_qa(videos["sin_subs"], qa_config()).reasons)
    few = tmp_path / "pocos.ass"
    few.write_text("[Events]\nDialogue: 0,0:00:00.00,0:00:01.00,Caption,,0,0,0,,HOLA\n", encoding="utf-8")
    assert any("pocos" in r for r in qa.run_qa(videos["ok"], qa_config(), few).reasons)
    assert qa.run_qa(videos["sin_subs"], qa_config(subtitulos_obligatorios=False)).passed


def test_herramienta_rota_bloquea(videos, monkeypatch, tmp_path):
    monkeypatch.setattr(qa, "ffmpeg_path", lambda: str(tmp_path / "no-existe-ffmpeg.exe"))
    monkeypatch.setattr(qa, "ffprobe_path", lambda: None)
    result = qa.run_qa(videos["ok"], qa_config())
    assert not result.passed and "QA bloqueado" in result.reason


def test_archivo_roto_y_ausente(tmp_path):
    broken = tmp_path / "roto.mp4"
    broken.write_bytes(b"esto no es un mp4")
    assert not qa.run_qa(broken, qa_config()).passed
    assert not qa.run_qa(tmp_path / "nada.mp4", qa_config()).passed


def test_caras_sin_modelo_bloquea(videos, monkeypatch, tmp_path):
    monkeypatch.setattr(qa, "FACE_MODEL", tmp_path / "falta.onnx")
    missing = qa.run_qa(videos["ok"], qa_config(caras={"activa": True}))
    assert not missing.passed and any("YuNet" in r for r in missing.reasons)


@pytest.mark.skipif(not qa.FACE_MODEL.is_file(), reason="falta assets/models/face_detection_yunet_2023mar.onnx")
def test_caras_opcional(videos):
    no_faces = qa.run_qa(videos["ok"], qa_config(caras={"activa": True, "muestras": 3}))
    assert not no_faces.passed and any("sin cara" in r for r in no_faces.reasons), no_faces.reasons
    assert no_faces.metrics["fotogramas_revisados"] >= 1


def test_config_antigua_usa_defaults():
    settings = qa.settings_from({"canal": "Clipazos ES"})
    assert settings["ancho"] == 1080 and settings["activa"] is True and settings["caras"]["activa"] is False
    assert qa.settings_from({"qa": {"fps": "rápido"}})["fps"] == 24.0
