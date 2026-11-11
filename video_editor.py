"""Utilidades de subtítulos ASS y FFmpeg.

Reloj ASS, ajuste de intervalos y el binario embebido de imageio-ffmpeg. El
paquete ``base`` monta el vertical de curiosidades encima de esto. ``SPLIT_TOP``
se conserva para el QA de los .ass antiguos con estilo CaptionSplit.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"

FRAME_SIZE = (1080, 1920)
FPS = 24
# Altura de la webcam cuando el Short parte la imagen: cámara arriba, juego abajo.
SPLIT_TOP = 840
# Amarillo #FFFF00 en el orden BGR que usa el formato ASS.
ACTIVE_ASS = "00FFFF"


@dataclass(frozen=True)
class CaptionCue:
    """Una palabra del audio, con su inicio y fin en segundos."""

    text: str
    start: float
    end: float


def _phrase_groups(cues: list[CaptionCue]) -> dict[int, list[int]]:
    """Hasta tres palabras por línea, el corte corto de los subtítulos de CapCut."""
    groups: list[list[int]] = []
    bucket: list[int] = []
    for index, cue in enumerate(cues):
        bucket.append(index)
        if len(bucket) >= 3 or cue.text.endswith((".", "!", "?", ";", ":")):
            groups.append(bucket)
            bucket = []
    if bucket:
        groups.append(bucket)
    return {index: group for group in groups for index in group}


def _caption_label(word: str) -> str:
    cleaned = re.sub(r"^[^\wáéíóúñü]+|[^\wáéíóúñü]+$", "", word, flags=re.IGNORECASE)
    return (cleaned or word).upper()


def sync_cues(cues: list[CaptionCue]) -> list[CaptionCue]:
    """Recorta cada palabra para que su intervalo no pise el de la siguiente."""
    ordered = sorted(cues, key=lambda cue: (cue.start, cue.end))
    synced: list[CaptionCue] = []
    for cue in ordered:
        start = max(0.0, cue.start)
        end = cue.end
        if synced and start < synced[-1].end:
            previous = synced[-1]
            if start <= previous.start:
                continue
            synced[-1] = CaptionCue(previous.text, previous.start, start)
        if end <= start:
            end = start + 0.04
        synced.append(CaptionCue(cue.text, start, end))
    tightened: list[CaptionCue] = []
    for index, cue in enumerate(synced):
        end = cue.end
        if index + 1 < len(synced):
            end = min(end, synced[index + 1].start)
        if end <= cue.start:
            end = cue.start + 0.04
        tightened.append(CaptionCue(cue.text, cue.start, end))
    return tightened


def _snap_time(seconds: float) -> float:
    return round(max(0.0, seconds), 2)


def _ass_clock(seconds: float) -> str:
    total = int(round(max(0.0, seconds) * 100))
    centis = total % 100
    total //= 100
    secs = total % 60
    total //= 60
    minutes = total % 60
    hours = total // 60
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _ass_word(word: str) -> str:
    label = _caption_label(word)
    return label.replace("\\", "").replace("{", "").replace("}", "")


def _phrase_text(words: list[str], active: int | None) -> str:
    chunks: list[str] = []
    for index, word in enumerate(words):
        label = _ass_word(word)
        if not label:
            continue
        if index == active:
            chunks.append("{\\c&H" + ACTIVE_ASS + "&}" + label + "{\\c&HFFFFFF&}")
        else:
            chunks.append(label)
    return "{\\q2}" + " ".join(chunks)


def _caption_events(cues: list[CaptionCue]) -> list[tuple[float, float, str]]:
    """Un evento por palabra. El hueco entre medias queda en blanco, sin solaparse."""
    groups = _phrase_groups(cues)
    raw: list[tuple[float, float, str]] = []
    for index, cue in enumerate(cues):
        group = groups.get(index, [index])
        words = [cues[item].text for item in group]
        offset = group.index(index)
        raw.append((cue.start, cue.end, _phrase_text(words, offset)))
        if index + 1 < len(cues):
            gap = cues[index + 1].start - cue.end
            if gap > 0.02:
                raw.append((cue.end, cues[index + 1].start, _phrase_text(words, None)))
    events: list[tuple[float, float, str]] = []
    for start, end, text in raw:
        start = _snap_time(start)
        end = _snap_time(end)
        if events and start < events[-1][1]:
            previous_start, _previous_end, previous_text = events[-1]
            events[-1] = (previous_start, start, previous_text)
        if end <= start:
            end = round(start + 0.04, 2)
        events.append((start, end, text))
    for index in range(1, len(events)):
        if events[index][0] < events[index - 1][1]:
            raise RuntimeError("Los subtítulos se solapan después de ajustarlos.")
    return events


def _ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _filter_path(path: Path) -> str:
    text = path.resolve().as_posix().replace("\\", "/")
    return text.replace(":", "\\:").replace("'", "\\'")


def _run_ffmpeg(command: list[str]) -> None:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        tail = "\n".join((result.stderr or "").splitlines()[-30:])
        raise RuntimeError(f"ffmpeg falló:\n{tail}")


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except Exception:
            pass
