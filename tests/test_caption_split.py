"""Geometría de subtítulos.

Los .ass con estilo CaptionSplit siguen pasando por qa.check_caption_geometry:
ese estilo es el de los Shorts de streamers ya montados. La posición del formato
nuevo (zona segura, sin webcam) se prueba en test_base.py, sin quemar fotogramas.
"""

from __future__ import annotations

import qa
from video_editor import FRAME_SIZE, SPLIT_TOP

WIDTH, HEIGHT = FRAME_SIZE


def _style(margin_v: int, alignment: int = 8) -> str:
    return "\n".join([
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: CaptionSplit,Arial Black,80,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        f"-1,0,0,0,100,100,1,0,1,5,3,{alignment},60,60,{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        "Dialogue: 0,0:00:00.00,0:00:00.40,CaptionSplit,,0,0,0,,{\\fs124\\fscx130\\fscy130}HOLA",
    ]) + "\n"


def test_el_margen_viejo_invade_la_webcam(tmp_path):
    path = tmp_path / "viejo.ass"
    path.write_text(_style(SPLIT_TOP - 55), encoding="utf-8")
    reasons, metrics = qa.check_caption_geometry(path)
    assert any("webcam" in reason for reason in reasons), reasons
    assert metrics["split_y0"] == SPLIT_TOP - 55


def test_varias_lineas_dentro_y_un_bloque_demasiado_alto(tmp_path):
    two = tmp_path / "dos.ass"
    two.write_text(_style(SPLIT_TOP).replace("HOLA", "HOLA\\NDOS"), encoding="utf-8")
    reasons, metrics = qa.check_caption_geometry(two)
    assert reasons == [], reasons
    assert metrics["split_y0"] >= SPLIT_TOP
    assert metrics["split_y1"] <= HEIGHT
    many = tmp_path / "muchas.ass"
    many.write_text(_style(SPLIT_TOP).replace("HOLA", "\\N".join(["HOLA"] * 20)), encoding="utf-8")
    reasons, _metrics = qa.check_caption_geometry(many)
    assert any("lienzo" in reason for reason in reasons), reasons


def test_posicion_libre_o_alineacion_rara_no_pasan(tmp_path):
    moved = tmp_path / "move.ass"
    moved.write_text(_style(SPLIT_TOP).replace("HOLA", "{\\pos(10,10)}HOLA"), encoding="utf-8")
    assert any("posición libre" in reason for reason in qa.check_caption_geometry(moved)[0])
    odd = tmp_path / "abajo.ass"
    odd.write_text(_style(120, alignment=2), encoding="utf-8")
    assert any("alineación" in reason for reason in qa.check_caption_geometry(odd)[0])


def test_un_ass_sin_estilo_split_no_cambia_el_qa_de_siempre(videos):
    result = qa.run_qa(videos["ok"], {"qa": {"duracion_min": 1, "duracion_max": 60}})
    assert result.passed, result.reasons
    assert "split_y0" not in result.metrics


def test_el_lienzo_de_referencia_sigue_siendo_vertical():
    assert (WIDTH, HEIGHT) == (1080, 1920)
