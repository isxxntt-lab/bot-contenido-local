"""Pruebas sin clips reales, sin credenciales y sin red: vídeos sintéticos con FFmpeg."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from video_editor import _ffmpeg_exe  # noqa: E402

ASS_OK = "\n".join(
    [
        "[Script Info]",
        "ScriptType: v4.00+",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        "Dialogue: 2,0:00:00.00,0:00:03.00,Title,,0,0,0,,TITULO",
        "Dialogue: 0,0:00:00.10,0:00:00.50,Caption,,0,0,0,,HOLA",
        "Dialogue: 0,0:00:00.50,0:00:01.00,CaptionSplit,,0,0,0,,QUE",
        "Dialogue: 0,0:00:01.00,0:00:01.50,CaptionFollow,,0,0,0,,PASA",
        "Dialogue: 0,0:00:01.50,0:00:02.00,Caption,,0,0,0,,CHAT",
    ]
) + "\n"


def make_video(
    path: Path,
    *,
    size: str = "1080x1920",
    fps: int = 24,
    duration: float = 4.0,
    audio: str = "normal",
    black: tuple[float, float] | None = None,
    freeze_after: float | None = None,
    ass: bool = True,
) -> Path:
    """MP4 sintético: testsrc2 (imagen en movimiento) + tono normalizado a -14 LUFS."""
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [_ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}:duration={duration}"]
    if audio != "none":
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={duration}"]
    filters = []
    if black:
        filters.append(f"drawbox=color=black:t=fill:enable='between(t,{black[0]},{black[1]})'")
    if freeze_after is not None:
        # Del fotograma freeze_after*fps al final se repite el mismo: imagen congelada.
        first = int(freeze_after * fps)
        filters.append(f"split[fa][fb];[fa][fb]freezeframes=first={first}:last={int(duration * fps) + 10}:replace={first}")
    if filters:
        command += ["-vf", ",".join(filters)]
    if audio == "normal":
        command += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=44100"]
    elif audio == "quiet":
        command += ["-af", "volume=-45dB"]
    command += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio != "none":
        command += ["-c:a", "aac", "-b:a", "128k", "-shortest"]
    command += ["-movflags", "+faststart", str(path)]
    subprocess.run(command, check=True, capture_output=True)
    if ass:
        path.with_suffix(".ass").write_text(ASS_OK, encoding="utf-8-sig")
    return path


@pytest.fixture(scope="session")
def videos(tmp_path_factory) -> dict[str, Path]:
    folder = tmp_path_factory.mktemp("videos")
    return {
        "ok": make_video(folder / "ok.mp4"),
        "horizontal": make_video(folder / "horizontal.mp4", size="1920x1080"),
        "fps30": make_video(folder / "fps30.mp4", fps=30),
        "sin_audio": make_video(folder / "sin_audio.mp4", audio="none"),
        "bajo": make_video(folder / "bajo.mp4", audio="quiet"),
        "negro": make_video(folder / "negro.mp4", black=(1.0, 3.0)),
        "congelado": make_video(folder / "congelado.mp4", freeze_after=1.0, duration=5.0),
        "sin_subs": make_video(folder / "sin_subs.mp4", ass=False),
    }


def qa_config(**overrides) -> dict:
    settings = {"duracion_min": 1, "duracion_max": 60}
    settings.update(overrides)
    return {"qa": settings}


def allowed_config(tmp_path: Path, **publication) -> dict:
    """Config con un streamer permitido y la base SQLite en una carpeta temporal."""
    pub = {
        "max_por_dia": 3,
        "max_por_ejecucion": 3,
        "max_por_streamer_dia": 1,
        "db_path": str(tmp_path / "pub.sqlite3"),
        "kill_switch_file": str(tmp_path / "STOP_YOUTUBE"),
    }
    pub.update(publication)
    return {
        "subida_youtube": {"activa": True, "privacidad": "private"},
        "publication": pub,
        "rights": {"streamers": {"twitch:ana": {"estado": "allowed"}, "twitch:beto": "allowed"}},
        "qa": {"duracion_min": 1, "duracion_max": 60},
    }
