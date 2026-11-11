"""Geometría de CaptionSplit: fuera de la webcam y dentro del lienzo.

Los fotogramas son sintéticos (color plano + la palabra TEST). No usan la fuente
de otro proceso ni material de streamers.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

import clipazos
import qa
from video_editor import SPLIT_TOP, CaptionCue, _ffmpeg_exe, _filter_path

WIDTH, HEIGHT = clipazos.FRAME_SIZE


def _moment(kind: str, word: str = "QUE", face=None) -> clipazos.Moment:
    candidate = clipazos.Candidate(
        "Prueba local", "TEST", "TEST NO ES UN STREAMER", 0, "local:test", "test-caption"
    )
    return clipazos.Moment(
        candidate, Path("unused.mp4"), 0.0, 0.5,
        [CaptionCue(word, 0.0, 0.45)],
        layout=clipazos.Layout(kind, 16 / 9, face),
    )


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


def test_el_margen_de_split_arranca_en_la_camara(tmp_path):
    ass = clipazos.write_short_captions(
        [_moment("split", face=(0.4, 0.2, 0.16, 0.22))],
        [[("TEST", True)]], "TEST", tmp_path / "split.mp4",
    )
    text = ass.read_text(encoding="utf-8-sig")
    assert f",8,60,60,{SPLIT_TOP},1" in text
    assert ",8,60,60,785,1" not in text
    reasons, metrics = qa.check_caption_geometry(ass)
    assert reasons == [], reasons
    assert metrics["split_y0"] >= SPLIT_TOP
    assert metrics["split_y1"] <= HEIGHT


def test_follow_y_blur_conservan_su_margen(tmp_path):
    follow = clipazos.write_short_captions(
        [_moment("follow", face=(0.4, 0.2, 0.2, 0.3))],
        [[("TEST", True)]], "TEST", tmp_path / "follow.mp4",
    ).read_text(encoding="utf-8-sig")
    blur = clipazos.write_short_captions(
        [_moment("blur")], [[("TEST", True)]], "TEST", tmp_path / "blur.mp4",
    ).read_text(encoding="utf-8-sig")
    caption = (HEIGHT + int(WIDTH * clipazos.SOURCE_KEEP * 9 / 16)) // 2 + 50
    follow_margin = (HEIGHT - clipazos.FOLLOW_BOX[1]) // 2 + 120
    assert f",2,60,60,{follow_margin},1" in follow
    assert f",8,60,60,{caption},1" in blur
    assert qa.check_caption_geometry(tmp_path / "follow.ass")[1]["split_eventos"] == 0
    assert qa.check_caption_geometry(tmp_path / "blur.ass")[1]["split_eventos"] == 0


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


def _frame(ass: Path, index: int, burn: bool) -> np.ndarray:
    draw = (
        f"drawbox=x=0:y=0:w={WIDTH}:h={SPLIT_TOP}:color=0x143C14:t=fill,"
        f"drawbox=x=340:y=180:w=400:h=480:color=0xC4A574:t=fill"
    )
    if burn:
        draw += f",ass=filename='{_filter_path(ass)}':fontsdir='{_filter_path(Path(r'C:\Windows\Fonts'))}'"
    draw += f",format=rgb24,select='eq(n\\,{index})'"
    raw = subprocess.run(
        [_ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i",
         f"color=c=0x1A4D1A:s={WIDTH}x{HEIGHT}:r=50:d=0.6",
         "-vf", draw, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        check=True, capture_output=True,
    ).stdout
    assert len(raw) == WIDTH * HEIGHT * 3
    return np.frombuffer(raw, np.uint8).reshape(HEIGHT, WIDTH, 3)


def _ink(ass: Path, index: int) -> tuple[int, int, int, int, int]:
    base = _frame(ass, index, burn=False)
    image = _frame(ass, index, burn=True)
    diff = np.abs(image.astype(np.int16) - base.astype(np.int16)).sum(axis=2) > 40
    assert diff.any(), "el subtítulo no pintó nada"
    ys, xs = np.nonzero(diff)
    return int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max()), int(diff[:SPLIT_TOP].sum())


def test_la_tinta_real_queda_bajo_la_webcam_y_dentro_del_lienzo(tmp_path):
    words = ("QUE", "ÁÉÍÓÚÑÜ", "WWWWWWWWWWW", "SUPERCALIFRAGILISTICO")
    for word in words:
        ass = clipazos.write_short_captions(
            [_moment("split", word, (0.4, 0.2, 0.16, 0.22))],
            [[("TEST", True)]], "TEST", tmp_path / f"{word}.mp4",
        )
        only = tmp_path / f"solo_{word}.ass"
        kept = [
            line for line in ass.read_text(encoding="utf-8-sig").splitlines()
            if not line.startswith("Dialogue:") or ",CaptionSplit," in line
        ]
        only.write_text("\n".join(kept) + "\n", encoding="utf-8-sig")
        for index in (1, 10):
            x0, x1, y0, y1, invaded = _ink(only, index)
            assert invaded == 0, (word, index, y0, invaded)
            assert y0 >= SPLIT_TOP
            assert 1 < x0 and x1 < WIDTH - 2 and y1 < HEIGHT - 2, (word, index, x0, x1, y0, y1)
    two = tmp_path / "dos_lineas.ass"
    source = (tmp_path / "solo_QUE.ass").read_text(encoding="utf-8-sig")
    two.write_text(source.replace("QUE", "QUE\\NDOS", 1), encoding="utf-8")
    _x0, _x1, y0, y1, invaded = _ink(two, 10)
    assert invaded == 0 and y0 >= SPLIT_TOP and y1 < HEIGHT - 2
