"""Clipazos ES: Short diario con los momentazos de streamers hispanos.

Flujo 100 % local y sin claves:
  1. Twitch (API pública de su web): clips en español de las últimas 24 h en los
     juegos más vistos y en las categorías fijas. Kick y YouTube: streamers fijos.
  2. Radar: guarda las vistas por juego y por streamer de cada día para detectar
     qué juego se dispara (Short especial de hype) y quién está subiendo.
  3. yt-dlp localiza cada clip y FFmpeg recorta solo el tramo necesario.
  4. faster-whisper transcribe en local con tiempos por palabra; el tramo elegido
     mezcla energía de audio (gritos, risas) y que se esté hablando.
  5. FFmpeg monta el vertical: fondo desenfocado, clip centrado, título, crédito
     del streamer y subtítulos con la palabra activa en amarillo.

La configuración del canal vive en clipazos.json.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from video_editor import (
    ACTIVE_ASS,
    BASE_DIR,
    FPS,
    FRAME_SIZE,
    OUTPUT_DIR,
    CaptionCue,
    _ass_clock,
    _caption_events,
    _ffmpeg_exe,
    _filter_path,
    _run_ffmpeg,
    _utf8_stdio,
    sync_cues,
)

CONFIG_PATH = BASE_DIR / "clipazos.json"
CLIPAZOS_DIR = OUTPUT_DIR / "clipazos"
CACHE_DIR = CLIPAZOS_DIR / "cache"
RADAR_PATH = CLIPAZOS_DIR / "radar.json"
CACHE_DAYS = 3

SAMPLE_RATE = 16_000
FRAME_STEP = 0.25
PAUSE_SECONDS = 0.35
DEAD_AIR = 0.32
PEAK_BONUS = 4.0
PUNCH_ZOOM = 0.10
DRIFT_ZOOM = 0.04
WHISPER_MODEL = "small"
YOUTUBE_SECTION = 30.0

TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
NON_GAMES = {
    "Just Chatting",
    "IRL",
    "Talk Shows & Podcasts",
    "Sports",
    "Music",
    "Art",
    "Always On",
    "Special Events",
    "Slots",
    "Virtual Casino",
    "ASMR",
    "Pools, Hot Tubs, and Beaches",
}

VIDEO_TOP = (FRAME_SIZE[1] - FRAME_SIZE[0] * 9 // 16) // 2
FACE_MODEL = BASE_DIR / "assets" / "models" / "face_detection_yunet_2023mar.onnx"
SCAN_SIZE = (640, 360)
SPLIT_TOP = 840
FOLLOW_BOX = (1080, 1440)
SOURCE_KEEP = 0.86
INTRO_SECONDS = 3.0
YELLOW = "{\\c&H" + ACTIVE_ASS + "&}"
WHITE = "{\\c&HFFFFFF&}"


@dataclass
class Candidate:
    platform: str
    streamer: str
    title: str
    views: int
    url: str
    key: str
    game: str | None = None
    created: datetime | None = None
    section: tuple[float, float] | None = None


@dataclass
class Moment:
    candidate: Candidate
    path: Path
    start: float
    length: float
    cues: list[CaptionCue]
    peak: float = 0.0
    layout: Layout | None = None


@dataclass
class Layout:
    """Cómo encajar el clip horizontal en el vertical.

    kind: "split" (cámara arriba, juego abajo), "follow" (plano cerrado en la cara)
    o "blur" (clip entero sobre fondo desenfocado). face va en coordenadas 0-1.
    """

    kind: str
    aspect: float
    face: tuple[float, float, float, float] | None = None


def load_config(path: Path = CONFIG_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _gql(query: str) -> dict:
    request = urllib.request.Request(
        TWITCH_GQL,
        data=json.dumps({"query": query}).encode(),
        headers={"Client-ID": TWITCH_WEB_CLIENT_ID, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.load(response)
    if data.get("errors"):
        raise RuntimeError(data["errors"][0].get("message", "error de Twitch"))
    return data.get("data") or {}


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def twitch_categories(settings: dict) -> list[str]:
    """Juegos con más espectadores ahora mismo más las categorías fijas."""
    limit = min(30, int(settings.get("juegos_top", 15)))
    data = _gql(
        "query{games(first:%d, options:{sort:VIEWER_COUNT}){edges{node{name}}}}" % limit
    )
    names = [edge["node"]["name"] for edge in data["games"]["edges"]]
    excluded = set(settings.get("categorias_excluidas", []))
    ordered: list[str] = []
    for name in list(settings.get("categorias_fijas", [])) + names:
        if name not in excluded and name not in ordered:
            ordered.append(name)
    return ordered


def scout_twitch(settings: dict, language: str) -> list[Candidate]:
    found: dict[str, Candidate] = {}
    for category in twitch_categories(settings):
        query = (
            "query{game(name:%s){clips(criteria:{period:LAST_DAY, languages:[%s]}, first:30)"
            "{edges{node{slug title viewCount durationSeconds createdAt "
            "broadcaster{login displayName} game{name}}}}}}"
        ) % (json.dumps(category), language.upper())
        try:
            game = _gql(query).get("game")
        except (OSError, RuntimeError) as exc:
            print(f"  Aviso: Twitch no respondió para {category} ({exc})", file=sys.stderr)
            continue
        if not game:
            continue
        for edge in game["clips"]["edges"]:
            node = edge["node"]
            broadcaster = node.get("broadcaster") or {}
            if not broadcaster.get("login") or node["durationSeconds"] < 10:
                continue
            key = f"twitch-{node['slug']}"
            found[key] = Candidate(
                platform="Twitch",
                streamer=broadcaster.get("displayName") or broadcaster["login"],
                title=node["title"],
                views=int(node["viewCount"]),
                url=f"https://clips.twitch.tv/{node['slug']}",
                key=key,
                game=(node.get("game") or {}).get("name") or category,
                created=_parse_time(node.get("createdAt")),
            )
    return sorted(found.values(), key=lambda candidate: candidate.views, reverse=True)


def _ytdlp(options: dict | None = None):
    import yt_dlp

    base = {"quiet": True, "no_warnings": True, "noprogress": True}
    base.update(options or {})
    return yt_dlp.YoutubeDL(base)


def scout_kick(settings: dict) -> list[Candidate]:
    limit = datetime.now(timezone.utc) - timedelta(hours=float(settings.get("horas", 48)))
    minimum = int(settings.get("vistas_minimas", 0))
    found: list[Candidate] = []
    for name in settings.get("streamers", []):
        try:
            with _ytdlp({"extract_flat": True, "playlistend": 20}) as downloader:
                listing = downloader.extract_info(f"https://kick.com/{name}/clips", download=False)
        except Exception as exc:  # noqa: BLE001 - yt-dlp lanza tipos muy variados
            print(f"  Aviso: Kick no respondió para {name} ({exc})", file=sys.stderr)
            continue
        for entry in listing.get("entries") or []:
            stamp = entry.get("timestamp")
            created = datetime.fromtimestamp(stamp, timezone.utc) if stamp else None
            views = int(entry.get("view_count") or 0)
            if created is None or created < limit or views < minimum:
                continue
            found.append(
                Candidate(
                    platform="Kick",
                    streamer=entry.get("channel") or entry.get("uploader") or name,
                    title=entry.get("title") or "",
                    views=views,
                    url=entry["url"],
                    key=f"kick-{entry['id']}",
                    created=created,
                )
            )
    return sorted(found, key=lambda candidate: candidate.views, reverse=True)


def _heatmap_peak(info: dict) -> float | None:
    duration = float(info.get("duration") or 0)
    points = [
        point
        for point in info.get("heatmap") or []
        if duration * 0.03 < point["start_time"] < duration * 0.98
    ]
    if not points:
        return None
    return float(max(points, key=lambda point: point["value"])["start_time"])


def scout_youtube(settings: dict) -> list[Candidate]:
    """Último directo de cada canal y su minuto más repetido."""
    limit = datetime.now(timezone.utc) - timedelta(hours=float(settings.get("horas", 48)))
    found: list[Candidate] = []
    for channel in settings.get("canales", []):
        try:
            with _ytdlp({"extract_flat": True, "playlistend": 4}) as downloader:
                listing = downloader.extract_info(
                    f"https://www.youtube.com/{channel}/streams", download=False
                )
            for entry in listing.get("entries") or []:
                if entry.get("live_status") in ("is_live", "is_upcoming"):
                    continue
                try:
                    with _ytdlp() as downloader:
                        info = downloader.extract_info(entry["url"], download=False)
                except Exception:  # noqa: BLE001 - directo programado o retirado
                    continue
                if info.get("live_status") in ("is_live", "is_upcoming"):
                    continue
                stamp = info.get("release_timestamp") or info.get("timestamp")
                created = datetime.fromtimestamp(stamp, timezone.utc) if stamp else None
                if created is None or created < limit:
                    break
                peak = _heatmap_peak(info)
                if peak is None:
                    continue
                start = max(0.0, peak - 4)
                found.append(
                    Candidate(
                        platform="YouTube",
                        streamer=info.get("channel") or channel,
                        title=info.get("title") or "",
                        views=int(info.get("view_count") or 0),
                        url=info["webpage_url"],
                        key=f"youtube-{info['id']}-{int(start)}",
                        created=created,
                        section=(start, YOUTUBE_SECTION),
                    )
                )
                break
        except Exception as exc:  # noqa: BLE001
            print(f"  Aviso: YouTube no respondió para {channel} ({exc})", file=sys.stderr)
    return found


def _load_radar() -> dict:
    if RADAR_PATH.is_file():
        return json.loads(RADAR_PATH.read_text(encoding="utf-8"))
    return {}


def update_radar(twitch: list[Candidate], today: str, keep_days: int) -> dict:
    """Vistas de clips del día por juego y por streamer (solo Twitch, es comparable)."""
    games: dict[str, int] = {}
    streamers: dict[str, int] = {}
    for candidate in twitch:
        if candidate.game and candidate.game not in NON_GAMES:
            games[candidate.game] = games.get(candidate.game, 0) + candidate.views
        streamers[candidate.streamer] = streamers.get(candidate.streamer, 0) + candidate.views
    history = _load_radar()
    history[today] = {"juegos": games, "streamers": streamers}
    for day in sorted(history)[:-keep_days]:
        del history[day]
    RADAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    RADAR_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    return history


def _baseline(history: dict, today: str, section: str, name: str) -> float | None:
    previous = [day for day in history if day < today]
    if not previous:
        return None
    return sum(history[day][section].get(name, 0) for day in previous) / len(previous)


def hype_games(history: dict, today: str, settings: dict) -> list[tuple[str, int, float]]:
    """Juegos cuyas vistas de hoy multiplican su media de días anteriores."""
    factor = float(settings.get("multiplicador", 2.0))
    minimum = int(settings.get("vistas_minimas", 3000))
    hot: list[tuple[str, int, float]] = []
    for game, views in history.get(today, {}).get("juegos", {}).items():
        baseline = _baseline(history, today, "juegos", game)
        if baseline is None or views < minimum:
            continue
        if views >= factor * max(baseline, 1.0):
            hot.append((game, views, baseline))
    return sorted(hot, key=lambda item: item[1], reverse=True)


def rising_streamers(history: dict, today: str, limit: int = 5) -> list[tuple[str, int, float]]:
    rising: list[tuple[str, int, float]] = []
    for name, views in history.get(today, {}).get("streamers", {}).items():
        baseline = _baseline(history, today, "streamers", name)
        if baseline is None or views < 500:
            continue
        rising.append((name, views, baseline))
    return sorted(rising, key=lambda item: item[1] / max(item[2], 1.0), reverse=True)[:limit]


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text)[:80]


def clean_cache(days: int = CACHE_DAYS) -> None:
    if not CACHE_DIR.is_dir():
        return
    limit = time.time() - days * 86400
    for path in CACHE_DIR.glob("*.mp4"):
        if path.stat().st_mtime < limit:
            path.unlink(missing_ok=True)


def fetch_media(candidate: Candidate) -> Path:
    """Localiza el clip con yt-dlp y FFmpeg baja solo el tramo que hace falta."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    destination = CACHE_DIR / f"{_safe_name(candidate.key)}.mp4"
    if destination.is_file() and destination.stat().st_size > 0:
        return destination
    with _ytdlp({"format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b"}) as downloader:
        info = downloader.extract_info(candidate.url, download=False)
    formats = info.get("requested_formats") or [info]
    command = [_ffmpeg_exe(), "-y", "-v", "error"]
    for media in formats:
        headers = media.get("http_headers") or {}
        if headers:
            command.extend(["-headers", "".join(f"{key}: {value}\r\n" for key, value in headers.items())])
        if candidate.section:
            start, length = candidate.section
            command.extend(["-ss", f"{start:.2f}", "-t", f"{length:.2f}"])
        command.extend(["-i", media["url"]])
    command.extend(
        ["-map", "0:v:0", "-map", f"{len(formats) - 1}:a:0",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
         "-c:a", "aac", "-b:a", "160k", str(destination)]
    )
    try:
        _run_ffmpeg(command)
    except RuntimeError:
        destination.unlink(missing_ok=True)
        raise
    return destination


def load_audio(path: Path) -> np.ndarray:
    raw = subprocess.run(
        [_ffmpeg_exe(), "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SAMPLE_RATE),
         "-f", "s16le", "-"],
        capture_output=True,
        check=True,
    ).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


_MODEL = None


def _whisper():
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel

        _MODEL = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _MODEL


def transcribe(audio: np.ndarray, language: str) -> list[CaptionCue]:
    segments, _info = _whisper().transcribe(
        audio, language=language, word_timestamps=True, vad_filter=True
    )
    return sync_cues(
        [
            CaptionCue(word.word.strip(), float(word.start), float(word.end))
            for segment in segments
            for word in segment.words
            if word.word.strip()
        ]
    )


def _frame_scores(audio: np.ndarray, cues: list[CaptionCue]) -> tuple[np.ndarray, np.ndarray]:
    """Energía suavizada (0-1) y habla (0/1) por fotograma de FRAME_STEP segundos."""
    step = int(SAMPLE_RATE * FRAME_STEP)
    frames = max(1, len(audio) // step)
    energy = np.sqrt(np.mean(audio[: frames * step].reshape(frames, step) ** 2, axis=1))
    energy = np.convolve(energy, np.ones(3) / 3, mode="same")
    energy = energy / max(float(energy.max()), 1e-6)
    speech = np.zeros(frames)
    for cue in cues:
        first = int(cue.start / FRAME_STEP)
        last = min(frames - 1, int(cue.end / FRAME_STEP))
        speech[first : last + 1] = 1.0
    return energy, speech


def _cut_points(cues: list[CaptionCue], total: float) -> tuple[list[float], list[float]]:
    """Inicios de frase y finales tras la reacción: donde un editor cortaría."""
    if not cues:
        return [0.0], [total]
    starts = [max(0.0, cues[0].start - 0.3)]
    ends: list[float] = []
    for previous, following in zip(cues, cues[1:]):
        gap = following.start - previous.end
        if gap >= PAUSE_SECONDS or previous.text.endswith((".", "!", "?", "…")):
            starts.append(max(previous.end, following.start - 0.25))
            ends.append(min(following.start, previous.end + 0.9))
    ends.append(min(total, cues[-1].end + 1.2))
    ends.append(total)
    return starts, ends


def choose_window(
    audio: np.ndarray, cues: list[CaptionCue], shortest: float, longest: float
) -> tuple[float, float, float]:
    """El momento completo: empieza en una frase, contiene el pico y acaba tras la reacción.

    Devuelve (inicio, duración, pico relativo al inicio). La duración varía con el
    momento, como en un clip editado a mano.
    """
    total = len(audio) / SAMPLE_RATE
    energy, speech = _frame_scores(audio, cues)
    value = 0.6 * energy + 0.4 * speech - DEAD_AIR
    prefix = np.concatenate([[0.0], np.cumsum(value)])
    peak_frame = int(np.argmax(energy * (0.5 + 0.5 * speech)))
    peak = peak_frame * FRAME_STEP
    starts, ends = _cut_points(cues, total)
    best: tuple[float, float, float] | None = None
    for start in starts:
        for end in ends:
            span = end - start
            if not shortest <= span <= longest:
                continue
            first, last = int(start / FRAME_STEP), int(end / FRAME_STEP)
            score = prefix[last] - prefix[first]
            if start <= peak - 1.0 and end >= peak + 1.5:
                score += PEAK_BONUS
            if best is None or score > best[0]:
                best = (score, start, span)
    if best is None:
        span = min(total, max(shortest, min(longest, 12.0)))
        start = min(max(0.0, peak - span * 0.45), total - span)
    else:
        _score, start, span = best
    first, last = int(start / FRAME_STEP), max(int(start / FRAME_STEP) + 1, int((start + span) / FRAME_STEP))
    local_peak = (first + int(np.argmax(energy[first:last]))) * FRAME_STEP - start
    return start, span, max(0.0, local_peak)


def _video_size(path: Path) -> tuple[int, int]:
    result = subprocess.run(
        [_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    match = re.search(r"Video:.*?(\d{3,5})x(\d{3,5})", result.stderr)
    return (int(match[1]), int(match[2])) if match else (1280, 720)


_DETECTOR = None


def _face_detector():
    global _DETECTOR
    if _DETECTOR is None:
        import cv2

        _DETECTOR = cv2.FaceDetectorYN.create(str(FACE_MODEL), "", SCAN_SIZE, 0.7)
    return _DETECTOR


def detect_layout(path: Path, start: float, length: float) -> Layout:
    """Busca la cara del streamer en el tramo (YuNet, local) y elige el formato.

    La cara que más se repite es la del streamer; si hay dos cámaras gana la que
    sale en más fotogramas. En el centro, la cámara ocupa la pantalla (follow).
    En un lateral solo es una webcam superpuesta (split) si es pequeña, casi no
    se mueve y hay una sola cara en esa zona; varias caras moviéndose por la
    misma zona es un IRL con gente y se usa blur.
    """
    width, height = _video_size(path)
    aspect = width / height
    if not FACE_MODEL.is_file():
        return Layout("blur", aspect)
    scan_width, scan_height = SCAN_SIZE
    raw = subprocess.run(
        [_ffmpeg_exe(), "-v", "error", "-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", str(path),
         "-vf", f"fps=1,scale={scan_width}:{scan_height}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        capture_output=True,
    ).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, scan_height, scan_width, 3)
    if not len(frames):
        return Layout("blur", aspect)
    hits: dict[tuple[int, int], list[tuple[float, float, float, float]]] = {}
    detector = _face_detector()
    for frame in frames:
        _found, faces = detector.detect(np.ascontiguousarray(frame))
        for face in faces if faces is not None else []:
            x, y, w, h = (float(value) for value in face[:4])
            box = (x / scan_width, y / scan_height, w / scan_width, h / scan_height)
            cell = (int((box[0] + box[2] / 2) / 0.15), int((box[1] + box[3] / 2) / 0.15))
            hits.setdefault(cell, []).append(box)
    if not hits:
        return Layout("blur", aspect)
    def summary(boxes: list[tuple[float, float, float, float]]) -> tuple[tuple[float, ...], float]:
        found = np.array(boxes)
        centers = found[:, :2] + found[:, 2:] / 2
        return tuple(float(v) for v in np.median(found, axis=0)), float(centers.std(axis=0).max())

    boxes = max(hits.values(), key=lambda found: (len(found), float(np.mean([b[2] for b in found]))))
    if len(boxes) >= max(2, 0.4 * len(frames)):
        face, _spread = summary(boxes)
        if 0.28 <= face[0] + face[2] / 2 <= 0.72:
            return Layout("follow", aspect, face)
    webcams = []
    for boxes in hits.values():
        face, spread = summary(boxes)
        if (
            0.6 * len(frames) <= len(boxes) <= 1.1 * len(frames)
            and face[2] <= 0.14
            and spread <= 0.04
            and not 0.28 <= face[0] + face[2] / 2 <= 0.72
        ):
            webcams.append((len(boxes), -spread, face))
    if webcams:
        return Layout("split", aspect, max(webcams)[2])
    return Layout("blur", aspect)


def build_moment(candidate: Candidate, shortest: float, longest: float, language: str) -> Moment:
    path = fetch_media(candidate)
    audio = load_audio(path)
    if audio.size == 0:
        raise RuntimeError("el clip no tiene audio")
    words = transcribe(audio, language)
    start, span, peak = choose_window(audio, words, shortest, longest)
    cues = [
        CaptionCue(cue.text, cue.start - start, min(cue.end, start + span) - start)
        for cue in words
        if start <= cue.start < start + span
    ]
    return Moment(candidate, path, start, span, cues, peak, detect_layout(path, start, span))


def collect_moments(
    candidates: list[Candidate], config: dict, language: str
) -> list[Moment]:
    """Momentos de duración natural hasta llegar a la duración objetivo del Short."""
    limit = int(config.get("clips_maximos", 5))
    target = float(config.get("segundos_objetivo", 40))
    shortest = float(config.get("segundos_min_clip", 6))
    longest = float(config.get("segundos_max_clip", 22))
    moments: list[Moment] = []
    total = 0.0
    for candidate in candidates:
        if len(moments) >= limit or total >= target - 3:
            break
        room = max(shortest, min(longest, target - total + 6))
        try:
            moment = build_moment(candidate, shortest, room, language)
        except Exception as exc:  # noqa: BLE001 - un clip roto no debe parar el vídeo
            print(f"  Salto @{candidate.streamer} ({candidate.platform}): {exc}", file=sys.stderr)
            continue
        moments.append(moment)
        total += moment.length
        print(
            f"  #{len(moments)} @{candidate.streamer} ({candidate.platform}, {candidate.views} vistas): "
            f"«{candidate.title}» -> {moment.start:.1f}-{moment.start + moment.length:.1f}s "
            f"({moment.length:.1f}s, pico en {moment.peak:.1f}s, {len(moment.cues)} palabras, "
            f"formato {moment.layout.kind if moment.layout else 'blur'})"
        )
    return moments


def _title_text(lines: list[list[tuple[str, bool]]]) -> str:
    longest = max(sum(len(text) for text, _yellow in line) for line in lines)
    size = min(84, int((FRAME_SIZE[0] - 120) / (0.72 * max(longest, 1))))
    rendered = []
    for line in lines:
        rendered.append("".join((YELLOW + text + WHITE) if yellow else text for text, yellow in line))
    return "{\\fs%d}" % size + "\\N".join(rendered)


def _clip_title(title: str) -> str:
    """Título del clip sin emojis (Arial Black no los tiene), corto y en mayúsculas."""
    cleaned = re.sub(r"[^\w\s¿?¡!.,:;'áéíóúñü-]", "", title, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,")
    if len(cleaned) < 3:
        return ""
    return cleaned.upper()[:34]


def _source_label(candidate: Candidate, channel: str) -> str:
    home = {"Twitch": "TWITCH.TV/", "Kick": "KICK.COM/", "YouTube": "YOUTUBE.COM/@"}
    return f"{channel.upper()} · {home.get(candidate.platform, '')}{candidate.streamer.upper()}"


def _pop(text: str) -> str:
    """La palabra activa entra un 25 % más grande y vuelve a su tamaño en 0,12 s."""
    active = YELLOW[:-1] + "\\fscx125\\fscy125\\t(0,120,\\fscx100\\fscy100)}"
    reset = WHITE[:-1] + "\\fscx100\\fscy100}"
    return text.replace(YELLOW, active).replace(WHITE, reset)


def write_short_captions(
    moments: list[Moment], title: list[list[tuple[str, bool]]], channel: str, destination: Path
) -> Path:
    width, height = FRAME_SIZE
    total = sum(moment.length for moment in moments)
    style = "&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,1,0"
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "ScaledBorderAndShadow: yes",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Title,Arial Black,84,{style},1,5,3,8,60,60,110,1",
        f"Style: Credit,Arial Black,48,{style},1,4,2,8,60,60,110,1",
        f"Style: Caption,Arial Black,76,{style},1,4,3,8,60,60,"
        f"{(height + int(width * SOURCE_KEEP * 9 / 16)) // 2 + 50},1",
        f"Style: CaptionSplit,Arial Black,80,{style},1,5,3,8,60,60,{SPLIT_TOP - 55},1",
        f"Style: CaptionFollow,Arial Black,80,{style},1,5,3,2,60,60,"
        f"{(height - FOLLOW_BOX[1]) // 2 + 120},1",
        "Style: Brand,Arial Black,40,&H40FFFFFF,&H000000FF,&H40000000,&H00000000,"
        "-1,0,0,0,100,100,2,0,1,3,0,2,60,60,110,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        f"Dialogue: 2,{_ass_clock(0)},{_ass_clock(min(INTRO_SECONDS, total))},Title,,0,0,0,,"
        f"{{\\fad(150,250)\\fscx115\\fscy115\\t(0,250,\\fscx100\\fscy100)}}{_title_text(title)}",
    ]
    caption_style = {"split": "CaptionSplit", "follow": "CaptionFollow"}
    offset = 0.0
    for moment in moments:
        candidate = moment.candidate
        start, end = _ass_clock(offset), _ass_clock(offset + moment.length)
        context = _clip_title(candidate.title)
        context_start = offset + (INTRO_SECONDS if offset == 0 else 0.0)
        context_end = min(offset + moment.length, context_start + INTRO_SECONDS)
        if context and context_end > context_start:
            lines.append(
                f"Dialogue: 1,{_ass_clock(context_start)},{_ass_clock(context_end)},Credit,,0,0,0,,"
                f"{{\\fad(200,250)}}{YELLOW}«{context}»{WHITE}"
            )
        lines.append(f"Dialogue: 2,{start},{end},Brand,,0,0,0,,{_source_label(candidate, channel)}")
        style_name = caption_style.get(moment.layout.kind if moment.layout else "", "Caption")
        shifted = [
            CaptionCue(cue.text, cue.start + offset, cue.end + offset) for cue in moment.cues
        ]
        for word_start, word_end, text in _caption_events(shifted) if shifted else []:
            lines.append(
                f"Dialogue: 0,{_ass_clock(word_start)},{_ass_clock(word_end)},{style_name},,0,0,0,,"
                f"{_pop(text)}"
            )
        offset += moment.length
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return destination


def _zoom(moment: Moment, width: int, height: int) -> str:
    """Zoom lento continuo más un golpe de zoom en el pico del momento."""
    amount = (
        f"(1+{DRIFT_ZOOM}*t/{moment.length:.3f}"
        f"+{PUNCH_ZOOM}*exp(-pow((t-{moment.peak:.2f})/0.45\\,2)))"
    )
    return (
        f"scale=w='trunc(iw*{amount}/2)*2':h='trunc(ih*{amount}/2)*2':eval=frame,"
        f"crop={width}:{height}"
    )


def _region(center_x: float, center_y: float, rel_w: float, rel_h: float) -> str:
    """Recorte relativo (0-1) centrado en un punto, sin salirse del fotograma."""
    rel_w, rel_h = min(rel_w, 1.0), min(rel_h, 1.0)
    x = min(max(center_x - rel_w / 2, 0.0), 1.0 - rel_w)
    y = min(max(center_y - rel_h / 2, 0.0), 1.0 - rel_h)
    return f"crop=w='iw*{rel_w:.4f}':h='ih*{rel_h:.4f}':x='iw*{x:.4f}':y='ih*{y:.4f}'"


def _layout_filters(index: int, moment: Moment, cut: str, entrance: str) -> list[str]:
    width, height = FRAME_SIZE
    layout = moment.layout or Layout("blur", 16 / 9)
    finish = f"setsar=1,format=yuv420p,{entrance}[v{index}]"
    blurred = (
        f"[raw{index}]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=24:2,eq=brightness=-0.18:saturation=1.2[bg{index}]"
    )
    if layout.kind == "split" and layout.face:
        x, y, w, h = layout.face
        cam_ratio = width / SPLIT_TOP
        cam_w = min(max(w * 3.4, 0.16), 0.45)
        cam = _region(x + w / 2, y + h * 0.65, cam_w, cam_w * layout.aspect / cam_ratio)
        game_h = SOURCE_KEEP
        game = _region(0.5, game_h / 2, game_h * (width / (height - SPLIT_TOP)) / layout.aspect, game_h)
        return [
            f"[{index}:v]{cut},fps={FPS},split[cam{index}][game{index}]",
            f"[cam{index}]{cam},scale={width}:{SPLIT_TOP},setsar=1[c{index}]",
            f"[game{index}]{game},scale={width}:{height - SPLIT_TOP},setsar=1[g{index}]",
            f"[c{index}][g{index}]vstack,{_zoom(moment, width, height)},{finish}",
        ]
    if layout.kind == "follow" and layout.face:
        x, _y, w, _h = layout.face
        box_w, box_h = FOLLOW_BOX
        region = _region(x + w / 2, SOURCE_KEEP / 2, SOURCE_KEEP * (box_w / box_h) / layout.aspect, SOURCE_KEEP)
        return [
            f"[{index}:v]{cut},fps={FPS},split[raw{index}][top{index}]",
            blurred,
            f"[top{index}]{region},scale={box_w}:{box_h},{_zoom(moment, box_w, box_h)}[fg{index}]",
            f"[bg{index}][fg{index}]overlay=(W-w)/2:(H-h)/2,{finish}",
        ]
    box = min(height - 2 * VIDEO_TOP, int(width * SOURCE_KEEP / layout.aspect) // 2 * 2)
    return [
        f"[{index}:v]{cut},fps={FPS},split[raw{index}][top{index}]",
        blurred,
        f"[top{index}]crop=iw:ih*{SOURCE_KEEP}:0:0,scale={width}:{box}:force_original_aspect_ratio=increase,"
        f"crop={width}:{box},{_zoom(moment, width, box)}[fg{index}]",
        f"[bg{index}][fg{index}]overlay=(W-w)/2:(H-h)/2,{finish}",
    ]


def render_short(
    moments: list[Moment], title: list[list[tuple[str, bool]]], channel: str, destination: Path
) -> Path:
    width, height = FRAME_SIZE
    work = destination.with_suffix("")
    captions = write_short_captions(moments, title, channel, work.with_name(work.name + ".ass"))
    parts: list[str] = []
    for index, moment in enumerate(moments):
        cut = f"trim=start={moment.start:.3f}:duration={moment.length:.3f},setpts=PTS-STARTPTS"
        entrance = "fade=t=in:st=0:d=0.25" if index == 0 else "fade=t=in:st=0:d=0.12:color=white"
        parts.extend(_layout_filters(index, moment, cut, entrance))
        parts.append(
            f"[{index}:a]atrim=start={moment.start:.3f}:duration={moment.length:.3f},"
            f"asetpts=PTS-STARTPTS,aresample=44100,loudnorm=I=-14:TP=-1.5:LRA=11,"
            f"aresample=44100[a{index}]"
        )
    joined = "".join(f"[v{index}][a{index}]" for index in range(len(moments)))
    parts.append(f"{joined}concat=n={len(moments)}:v=1:a=1[vcat][aout]")
    fonts = _filter_path(Path(r"C:\Windows\Fonts"))
    parts.append(f"[vcat]ass=filename='{_filter_path(captions)}':fontsdir='{fonts}'[vout]")
    script = work.with_name(work.name + "_filter.txt")
    script.write_text(";\n".join(parts), encoding="utf-8")

    command = [_ffmpeg_exe(), "-y"]
    for moment in moments:
        command.extend(["-i", str(moment.path)])
    command.extend(
        ["-filter_complex_script", str(script), "-map", "[vout]", "-map", "[aout]",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(destination)]
    )
    _run_ffmpeg(command)
    return destination


def _hashtag(text: str) -> str:
    return "#" + re.sub(r"[^\wáéíóúñü]+", "", text, flags=re.IGNORECASE)


def post_text(moments: list[Moment], headline: str, extra_tags: list[str]) -> dict:
    names = list(dict.fromkeys(moment.candidate.streamer for moment in moments))
    credits = [
        f"#{rank} @{moment.candidate.streamer} ({moment.candidate.platform}): {moment.candidate.url}"
        for rank, moment in enumerate(moments, start=1)
    ]
    platforms = list(dict.fromkeys(f"#{moment.candidate.platform.lower()}" for moment in moments))
    tags = ["#clipazos", "#streamers", *platforms, "#clipsespañol"] + extra_tags
    tags += [_hashtag(name).lower() for name in names[:3]]
    title = headline if len(names) == 1 else f"{headline}: {', '.join(names[:3])}"
    return {
        "titulo": title,
        "descripcion": "\n".join(["Créditos a los streamers:", *credits]),
        "hashtags": list(dict.fromkeys(tags)),
    }


def make_short(
    candidates: list[Candidate],
    config: dict,
    title: list[list[tuple[str, bool]]],
    headline: str,
    destination: Path,
    extra_tags: list[str],
) -> dict | None:
    moments = collect_moments(candidates, config, config.get("idioma", "es"))
    if len(moments) < 2:
        print("  No hay clips suficientes para este Short.", file=sys.stderr)
        return None
    render_short(moments, title, config.get("canal", "Clipazos ES"), destination)
    post = post_text(moments, headline, extra_tags)
    post["video"] = str(destination)
    post["duracion"] = round(sum(moment.length for moment in moments), 1)
    print(f"  Vídeo: {destination} ({post['duracion']}s, {destination.stat().st_size} bytes)")
    return post


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clipazos ES: Short diario de streamers hispanos.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--solo-radar", action="store_true", help="Busca y actualiza el radar sin montar vídeos.")
    parser.add_argument("--sin-hype", action="store_true", help="No monta el Short especial de juego en hype.")
    parser.add_argument("--streamer", help="Short con los mejores clips de un solo streamer.")
    parser.add_argument("--clips", type=int, help="Máximo de clips por vídeo (clips_maximos).")
    parser.add_argument("--segundos", type=float, help="Duración objetivo del Short (segundos_objetivo).")
    return parser.parse_args(argv)


def streamer_pool(candidates: list[Candidate], name: str) -> list[Candidate]:
    wanted = name.casefold().lstrip("@")
    pool = [candidate for candidate in candidates if candidate.streamer.casefold() == wanted]
    return sorted(pool, key=lambda candidate: candidate.views, reverse=True)


def top_streamers(
    candidates: list[Candidate], count: int, skip: set[str], min_clips: int = 2, best_of: int = 4
) -> list[tuple[str, list[Candidate]]]:
    """Streamers con más vistas en sus mejores clips del día, cada uno con sus clips."""
    pools: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        if candidate.streamer.casefold() in skip:
            continue
        pools.setdefault(candidate.streamer, []).append(candidate)
    ranked = [
        (name, sorted(pool, key=lambda candidate: candidate.views, reverse=True))
        for name, pool in pools.items()
        if len(pool) >= min_clips
    ]
    ranked.sort(key=lambda item: sum(c.views for c in item[1][:best_of]), reverse=True)
    return ranked[:count]


def recently_posted(today: str, days: int) -> set[str]:
    """Streamers con Short en los últimos días, para no repetir siempre al mismo."""
    if days <= 0:
        return set()
    limit = (datetime.strptime(today, "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")
    names: set[str] = set()
    for path in CLIPAZOS_DIR.glob("reporte_*.json"):
        day = path.stem.split("_")[1]
        if not (limit <= day < today):
            continue
        try:
            shorts = json.loads(path.read_text(encoding="utf-8")).get("shorts", [])
        except (OSError, ValueError):
            continue
        names.update(short["streamer"].casefold() for short in shorts if short.get("streamer"))
    return names


def streamer_short(
    pool: list[Candidate], name: str, config: dict, today: str, game: str | None = None
) -> dict | None:
    """Short de un solo streamer: sus mejores momentos del día, del más visto al menos."""
    name = pool[0].streamer if pool else name
    print(f"\nShort de @{name}: {len(pool)} clips en las últimas 24 h")
    if game:
        title = [[("@" + name.upper(), True)], [("EN " + game.upper(), False)]]
        headline = f"Lo mejor de {name} en {game} hoy"
        tags = [_hashtag(game).lower()]
    else:
        title = [[("LO MEJOR DE", False)], [("@" + name.upper(), True), (" HOY", False)]]
        headline = f"Lo mejor de {name} hoy"
        tags = []
    short = make_short(
        pool, config, title, headline,
        CLIPAZOS_DIR / f"streamer_{_safe_name(name).lower()}_{today}.mp4", tags,
    )
    if short:
        short["streamer"] = name
    return short


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    args = parse_args(argv)
    config = load_config(args.config)
    if args.clips:
        config["clips_maximos"] = args.clips
    if args.segundos:
        config["segundos_objetivo"] = args.segundos
    channel = config.get("canal", "Clipazos ES")
    language = config.get("idioma", "es")
    today = datetime.now().strftime("%Y%m%d")
    excluded = {name.casefold() for name in config.get("excluir_streamers", [])}
    print(f"{channel}: buscando momentazos de las últimas 24 h...")
    clean_cache()

    twitch = scout_twitch(config.get("twitch", {}), language)
    kick = scout_kick(config.get("kick", {}))
    youtube = scout_youtube(config.get("youtube", {}))
    print(f"Clips encontrados: Twitch {len(twitch)}, Kick {len(kick)}, YouTube {len(youtube)}")

    radar_settings = config.get("radar", {})
    history = update_radar(twitch, today, int(radar_settings.get("dias_historial", 14)))
    hot = hype_games(history, today, radar_settings)
    rising = rising_streamers(history, today)
    days = len(history)
    print(f"Radar ({days} día{'s' if days != 1 else ''} de historial):")
    if days < 2:
        print("  Primer día: mañana ya podrá comparar juegos y streamers.")
    for game, views, baseline in hot:
        print(f"  HYPE {game}: {views} vistas hoy (media {baseline:.0f})")
    for name, views, baseline in rising:
        print(f"  Subiendo @{name}: {views} vistas hoy (media {baseline:.0f})")

    report = {
        "canal": channel,
        "fecha": today,
        "hype": [{"juego": game, "vistas": views, "media": round(baseline)} for game, views, baseline in hot],
        "subiendo": [{"streamer": name, "vistas": views, "media": round(baseline)} for name, views, baseline in rising],
        "shorts": [],
    }
    if args.streamer:
        pool = streamer_pool(twitch + kick + youtube, args.streamer)
        short = streamer_short(pool, args.streamer.lstrip("@"), config, today)
        if short:
            report["shorts"].append(short)
    elif not args.solo_radar:
        wanted = int(config.get("shorts_por_dia", 1))
        recent = recently_posted(today, int(config.get("no_repetir_dias", 2)))
        picked = top_streamers(twitch, wanted, excluded | recent)
        if not picked:
            print("\nNo hay ningún streamer con clips suficientes hoy.", file=sys.stderr)
        for name, pool in picked:
            short = streamer_short(pool, name, config, today)
            if short:
                report["shorts"].append(short)
        kick_wanted = int(config.get("kick", {}).get("shorts_por_dia", 0))
        used = {short["streamer"].casefold() for short in report["shorts"]}
        for name, pool in top_streamers(kick, kick_wanted, excluded | recent | used):
            print("\nDesde Kick:")
            short = streamer_short(pool, name, config, today)
            if short:
                report["shorts"].append(short)
        if hot and not args.sin_hype:
            game = hot[0][0]
            used = {short["streamer"].casefold() for short in report["shorts"]}
            in_game = [c for c in twitch if c.game == game]
            for name, pool in top_streamers(in_game, 1, excluded | used):
                print(f"\nJuego en hype: {game}")
                short = streamer_short(pool, name, config, today, game=game)
                if short:
                    report["shorts"].append(short)

    CLIPAZOS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"_{_safe_name(args.streamer.lstrip('@')).lower()}" if args.streamer else ""
    report_path = CLIPAZOS_DIR / f"reporte_{today}{suffix}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for short in report["shorts"]:
        print(f"\n{short['titulo']}\n{short['descripcion']}\n{' '.join(short['hashtags'])}")
    print(f"\nReporte: {report_path}")
    upload = config.get("subida_youtube", {})
    if upload.get("activa") and report["shorts"]:
        import youtube_upload

        if youtube_upload.is_ready():
            print("\nSubiendo a YouTube...")
            youtube_upload.upload_report(report_path, upload.get("privacidad", "private"))
        else:
            print("\nSubida a YouTube activa pero sin autorizar: python youtube_upload.py --autorizar")
    if not args.solo_radar and not report["shorts"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
