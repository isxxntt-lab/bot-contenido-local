"""Vídeos cortos 9:16 a partir de las noticias guardadas en output/.

El guión se adapta al español con reglas locales (frases y palabras), sin API
de traducción. La voz sale de edge-tts y el montaje de moviepy. Coste: 0.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import edge_tts
import numpy as np
from moviepy import AudioFileClip, VideoClip, VideoFileClip
from moviepy.video.fx import Loop
from PIL import Image, ImageDraw, ImageFont


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
BACKGROUNDS_DIR = BASE_DIR / "assets" / "backgrounds"
TEST_AUDIO = OUTPUT_DIR / "audio_test.mp3"
TEST_VIDEO = OUTPUT_DIR / "video_test.mp4"
LATEST_VIDEO = OUTPUT_DIR / "latest_video.mp4"

# 100 nanosegundos, la unidad de tiempo de los eventos WordBoundary de edge-tts.
_TICKS_PER_SECOND = 10_000_000

# Voz neuronal en español de España. Alternativa: es-MX-JorgeNeural.
DEFAULT_VOICE = "es-ES-AlvaroNeural"
FRAME_SIZE = (1080, 1920)
FPS = 24
_FONT_CACHE: dict[tuple[int, bool], ImageFont.ImageFont] = {}


@dataclass(frozen=True)
class CaptionCue:
    """Una palabra del audio, con su inicio y fin en segundos."""

    text: str
    start: float
    end: float


_OPENERS = (
    "Esto acaba de publicarse.",
    "Una noticia breve, directa al grano.",
    "El radar de tecnología se mueve otra vez.",
    "Quédate con este dato.",
)

# Nombres y productos que deben oírse tal cual.
_PROTECTED_NAMES = (
    "Amazon Web Services",
    "Hugging Face",
    "The Verge",
    "TechCrunch",
    "Android Auto",
    "Blackstone",
    "Anthropic",
    "Microsoft",
    "OpenAI",
    "ChatGPT",
    "DeepMind",
    "NVIDIA",
    "Gemini",
    "Claude",
    "Google",
    "WIRED",
    "Apple",
    "Meta",
    "Muse",
    "PHP",
)

# Frases largas primero: así no las parte el diccionario palabra por palabra.
_PHRASES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), replacement)
    for pattern, replacement in (
        (
            r"hackers exploited the (.+?) vulnerabilities days before they were patched",
            r"unos hackers explotaron las vulnerabilidades de \1 días antes de que fueran parcheadas",
        ),
        (
            r"logged attempts to leverage the internet-melting (.+?) bugs four days ago",
            r"registró intentos de aprovechar los fallos \1, que están causando estragos en internet, hace cuatro días",
        ),
        (
            r"they(?:'re| are) now being broadly exploited across the internet",
            r"ahora se están explotando de forma masiva en todo internet",
        ),
        (r"artificial intelligence", "inteligencia artificial"),
        (r"machine learning", "aprendizaje automático"),
        (r"open source", "código abierto"),
        (r"data centers?", "centros de datos"),
        (r"air quality", "calidad del aire"),
        (r"small business(?:es)?", "pequeños negocios"),
        (r"according to", "según"),
        (r"more than", "más de"),
        (r"as part of", "como parte de"),
        (r"four days ago", "hace cuatro días"),
        (r"days ago", "hace días"),
        (r"across the internet", "en todo internet"),
        (r"are overblown", "están exagerados"),
        (r"internet-melting", "que causan estragos en internet"),
        (r"heading to", "llega a"),
        (r"takes on", "compite con"),
        (r"you need to know", "que tienes que saber"),
    )
)

_WORDS = {
    "a": "un",
    "an": "un",
    "the": "el",
    "of": "de",
    "to": "para",
    "in": "en",
    "on": "sobre",
    "for": "para",
    "and": "y",
    "or": "o",
    "with": "con",
    "from": "desde",
    "by": "por",
    "at": "en",
    "into": "hacia",
    "over": "sobre",
    "after": "después de",
    "before": "antes de",
    "without": "sin",
    "about": "sobre",
    "that": "que",
    "this": "este",
    "these": "estos",
    "those": "esos",
    "their": "su",
    "its": "su",
    "it": "esto",
    "they": "ellos",
    "them": "ellos",
    "not": "no",
    "but": "pero",
    "if": "si",
    "when": "cuando",
    "while": "mientras",
    "than": "que",
    "because": "porque",
    "during": "durante",
    "against": "contra",
    "new": "nuevo",
    "news": "noticia",
    "is": "es",
    "are": "están",
    "was": "fue",
    "were": "fueron",
    "has": "tiene",
    "have": "tienen",
    "had": "tenía",
    "says": "dice que",
    "said": "dijo que",
    "will": "va a",
    "can": "puede",
    "could": "podría",
    "may": "puede",
    "must": "debe",
    "isn't": "no es",
    "aren't": "no están",
    "it's": "es",
    "they're": "están",
    "don't": "no",
    "users": "usuarios",
    "user": "usuario",
    "company": "empresa",
    "companies": "empresas",
    "startup": "startup",
    "model": "modelo",
    "models": "modelos",
    "agent": "agente",
    "hackers": "hackers",
    "hacker": "hacker",
    "exploited": "explotaron",
    "exploit": "explotar",
    "vulnerabilities": "vulnerabilidades",
    "vulnerability": "vulnerabilidad",
    "patched": "parcheadas",
    "patch": "parche",
    "bugs": "fallos",
    "bug": "fallo",
    "days": "días",
    "day": "día",
    "logged": "registró",
    "attempts": "intentos",
    "attempt": "intento",
    "leverage": "aprovechar",
    "broadly": "de forma masiva",
    "internet": "internet",
    "fears": "los temores",
    "debt": "deuda",
    "overblown": "exagerados",
    "market": "mercado",
    "quality": "calidad",
    "gains": "avances",
    "erase": "borrar",
    "decades": "décadas",
    "shares": "acciones",
    "fund": "financiar",
    "push": "impulso",
    "shopping": "compras",
    "activity": "actividad",
    "everything": "todo",
    "founder": "fundador",
    "summit": "cumbre",
    "security": "seguridad",
    "researchers": "investigadores",
    "government": "gobierno",
    "protests": "protestas",
    "climate": "clima",
    "goals": "objetivos",
    "europe": "Europa",
    "million": "millones",
    "billion": "mil millones",
    "app": "aplicación",
    "car": "coche",
    "cars": "coches",
    "now": "ahora",
    "just": "acaba de",
    "announced": "anunció",
    "released": "lanzó",
    "launching": "está lanzando",
    "available": "disponible",
    "report": "informe",
    "research": "investigación",
    "latest": "último",
    "top": "principal",
    "class": "clase",
    "create": "crear",
    "ai": "inteligencia artificial",
}


def latest_posts_file(output_dir: Path = OUTPUT_DIR) -> Path:
    """Devuelve el posts_*.json modificado más recientemente."""
    files = [path for path in output_dir.glob("posts_*.json") if path.is_file()]
    if not files:
        raise FileNotFoundError(
            f"No hay posts_*.json en {output_dir}. Ejecuta main.py antes de generar el vídeo."
        )
    return max(files, key=lambda path: path.stat().st_mtime)


def _published_at(article: dict) -> datetime:
    raw = str(article.get("published") or "")
    try:
        moment = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):
        return datetime.min.replace(tzinfo=timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def load_latest_article(output_dir: Path = OUTPUT_DIR) -> dict:
    """Lee el JSON más nuevo y elige la noticia con la fecha de publicación mayor."""
    path = latest_posts_file(output_dir)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        articles = payload.get("posts") or []
    elif isinstance(payload, list):
        articles = payload
    else:
        articles = []

    articles = [item for item in articles if isinstance(item, dict)]
    if not articles:
        raise ValueError(f"{path.name} no contiene noticias.")

    article = dict(max(articles, key=_published_at))
    article["source_file"] = path.name
    return article


def _stable_index(text: str, size: int) -> int:
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % size


def _protect(text: str) -> tuple[str, dict[str, str]]:
    """Aparta nombres propios para que el diccionario no los traduzca."""
    slots: dict[str, str] = {}
    index = 0

    def stash(value: str) -> str:
        nonlocal index
        token = f"«{index}»"
        index += 1
        slots[token] = value
        return token

    for name in sorted(_PROTECTED_NAMES, key=len, reverse=True):
        pattern = re.compile(re.escape(name), re.IGNORECASE)
        text = pattern.sub(lambda match: stash(match.group(0)), text)

    text = re.sub(
        r"\b[A-Za-z][A-Za-z0-9]*\d[A-Za-z0-9]*\b",
        lambda match: stash(match.group(0)),
        text,
    )
    return text, slots


def _restore(text: str, slots: dict[str, str]) -> str:
    for token, value in slots.items():
        text = text.replace(token, value)
    return text


def _match_case(source: str, translated: str) -> str:
    if source.isupper():
        return translated.upper()
    if source[:1].isupper():
        return translated[:1].upper() + translated[1:]
    return translated


def localize_to_spanish(text: str) -> str:
    """Adapta un titular o resumen inglés a español con frases y palabras locales."""
    cleaned = (
        text.replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", "")
        .replace("\u201d", "")
        .replace('"', "")
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return ""

    protected, slots = _protect(cleaned)
    adapted = protected
    for pattern, replacement in _PHRASES:
        adapted = pattern.sub(replacement, adapted)

    def replace_word(match: re.Match[str]) -> str:
        word = match.group(0)
        translated = _WORDS.get(word.lower())
        if translated is None:
            return word
        return _match_case(word, translated)

    adapted = re.sub(r"[A-Za-z]+(?:'[A-Za-z]+)?", replace_word, adapted)
    adapted = _restore(adapted, slots)
    adapted = re.sub(r"\s+", " ", adapted)
    adapted = re.sub(r"\s+([,.;:!?])", r"\1", adapted)
    return adapted.strip(" ,;")


def _as_sentence(text: str) -> str:
    text = text.strip(" .")
    if not text:
        return ""
    text = text[:1].upper() + text[1:]
    text = re.sub(
        r"([.!?]\s+)([a-záéíóúñü])",
        lambda match: match.group(1) + match.group(2).upper(),
        text,
    )
    return text if text.endswith((".", "!", "?")) else f"{text}."


def build_script(article: dict) -> str:
    """Guión hablado en español: gancho, fuente, titular y resumen."""
    source = str(article.get("source") or "").strip() or "un medio tecnológico"
    title = localize_to_spanish(str(article.get("title") or ""))
    summary = localize_to_spanish(str(article.get("summary") or ""))
    if not title and not summary:
        raise ValueError("La noticia no tiene título ni resumen para narrar.")

    opener = _OPENERS[_stable_index(title or summary, len(_OPENERS))]
    parts = [opener, f"Lo cuenta {source}."]
    if title:
        parts.append(_as_sentence(title))
    if summary and summary.casefold() not in title.casefold():
        parts.append(_as_sentence(summary))
    return " ".join(parts)


def _even_cues(script: str, duration: float) -> list[CaptionCue]:
    """Reparte el guión en el tiempo si edge-tts no manda marcas de palabra."""
    words = script.split()
    if not words or duration <= 0:
        return []
    step = duration / len(words)
    return [
        CaptionCue(word, index * step, (index + 1) * step)
        for index, word in enumerate(words)
    ]


async def synthesize(
    text: str,
    destination: Path,
    voice: str = DEFAULT_VOICE,
) -> tuple[Path, list[CaptionCue]]:
    """Genera el mp3 y las marcas de tiempo de cada palabra."""
    script = " ".join(text.split())
    if not script:
        raise ValueError("El guión está vacío.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    communicate = edge_tts.Communicate(script, voice, boundary="WordBoundary")
    audio = bytearray()
    cues: list[CaptionCue] = []
    async for message in communicate.stream():
        kind = message["type"]
        if kind == "audio":
            audio.extend(message["data"])
            continue
        if kind != "WordBoundary":
            continue
        word = str(message.get("text") or "").strip()
        if not word:
            continue
        start = float(message["offset"]) / _TICKS_PER_SECOND
        end = start + float(message["duration"]) / _TICKS_PER_SECOND
        cues.append(CaptionCue(word, start, max(end, start + 0.04)))

    if not audio:
        raise RuntimeError(f"edge-tts no escribió audio en {destination}.")
    destination.write_bytes(audio)
    return destination, cues


def generate_audio(
    text: str,
    destination: Path,
    voice: str = DEFAULT_VOICE,
) -> tuple[Path, list[CaptionCue]]:
    """Versión síncrona de synthesize, para llamarla desde el resto del bot."""
    return asyncio.run(synthesize(text, destination, voice))


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    key = (size, bold)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    filename = "arialbd.ttf" if bold else "arial.ttf"
    path = windir / "Fonts" / filename
    font = ImageFont.truetype(str(path), size=size) if path.is_file() else ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = word if not current else f"{current} {word}"
        if draw.textlength(trial, font=font) <= width:
            current = trial
            continue
        if current:
            lines.append(current)
        current = word
    if current:
        lines.append(current)
    return lines


def _text_overlay(title: str, source: str) -> np.ndarray:
    """Cartel fijo: rótulo arriba y fuente abajo. El centro queda para los subtítulos."""
    width, height = FRAME_SIZE
    image = Image.new("RGBA", FRAME_SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = 90
    title_font = _font(44, bold=True)
    kicker_font = _font(34, bold=True)
    source_font = _font(36, bold=True)

    draw.text((margin, 140), "NOTICIA IA", font=kicker_font, fill=(120, 230, 214, 255))
    for offset, line in enumerate(_wrap(draw, title, title_font, width - margin * 2)[:2]):
        y = 210 + offset * 58
        draw.text((margin + 2, y + 2), line, font=title_font, fill=(0, 0, 0, 160))
        draw.text((margin, y), line, font=title_font, fill=(255, 255, 255, 255))

    label = source or "Tecnología"
    pill_width = int(draw.textlength(label, font=source_font)) + 64
    pill = (margin, height - 250, margin + pill_width, height - 170)
    draw.rounded_rectangle(pill, radius=28, fill=(8, 16, 32, 180))
    draw.text((margin + 32, height - 232), label, font=source_font, fill=(255, 255, 255, 255))
    return np.array(image)


def _phrase_groups(cues: list[CaptionCue]) -> dict[int, list[int]]:
    """Agrupa las palabras en frases cortas y devuelve el grupo de cada índice."""
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


def _cue_at(cues: list[CaptionCue], moment: float) -> int:
    found = 0
    low = 0
    high = len(cues) - 1
    while low <= high:
        mid = (low + high) // 2
        if cues[mid].start <= moment:
            found = mid
            low = mid + 1
        else:
            high = mid - 1
    return found


def _draw_outlined(
    draw: ImageDraw.ImageDraw,
    position: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
) -> None:
    x, y = position
    for dx, dy in ((-3, 0), (3, 0), (0, -3), (0, 3), (-2, -2), (2, 2), (-2, 2), (2, -2)):
        draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0, 230))
    draw.text((x, y), text, font=font, fill=fill)


def _caption_strip(cues: list[CaptionCue], active: int, groups: dict[int, list[int]]) -> np.ndarray:
    """Frase corta centrada. La palabra que suena va en amarillo y un poco más alta."""
    width = FRAME_SIZE[0]
    height = 520
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    normal = _font(64, bold=True)
    active_font = _font(80, bold=True)
    group = groups.get(active, [active])
    gap = 22
    max_width = width - 140

    lines: list[list[tuple[str, ImageFont.ImageFont, bool, float]]] = [[]]
    line_width = 0.0
    for index in group:
        word = cues[index].text
        is_active = index == active
        font = active_font if is_active else normal
        word_width = float(draw.textlength(word, font=font))
        if lines[-1] and line_width + gap + word_width > max_width:
            lines.append([])
            line_width = 0.0
        if lines[-1]:
            line_width += gap
        lines[-1].append((word, font, is_active, word_width))
        line_width += word_width

    line_height = 110
    block_height = len(lines) * line_height
    top = max(20, (height - block_height) // 2)
    for line_index, line in enumerate(lines):
        used = sum(item[3] for item in line) + gap * (len(line) - 1)
        x = int((width - used) / 2)
        y = top + line_index * line_height
        pad = 24
        draw.rounded_rectangle(
            (x - pad, y - 8, x + used + pad, y + 92),
            radius=22,
            fill=(6, 10, 24, 150),
        )
        for word, font, is_active, word_width in line:
            word_y = y - 10 if is_active else y
            fill = (255, 224, 64, 255) if is_active else (255, 255, 255, 255)
            _draw_outlined(draw, (int(x), word_y), word, font, fill)
            x += int(word_width) + gap
    return np.array(image)


def _background_frame(t: float, duration: float) -> np.ndarray:
    """Gradiente vertical que cambia de color y una barra que recorre la pantalla."""
    width, height = FRAME_SIZE
    phase = (t / duration) if duration else 0.0
    phase = min(max(phase, 0.0), 1.0)
    blend = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None, None]
    shift = 0.12 * np.sin(phase * 6.28318)
    blend = np.clip(blend + shift, 0.0, 1.0)
    top = np.array([8, 16, 42], dtype=np.float32)
    bottom = np.array([14, 78, 112], dtype=np.float32) * (1.0 - phase) + np.array(
        [86, 28, 92], dtype=np.float32
    ) * phase
    gradient = top * (1.0 - blend) + bottom * blend
    frame = np.broadcast_to(gradient, (height, width, 3)).copy()
    bar_y = int((0.08 + 0.84 * phase) * (height - 10))
    frame[bar_y : bar_y + 8, :, :] = (90, 230, 210)
    return frame


def _blit(frame: np.ndarray, overlay: np.ndarray, y: int = 0, x: int = 0) -> np.ndarray:
    """Pega una capa RGBA sobre un fotograma RGB."""
    frame = np.ascontiguousarray(frame)
    overlay_h, overlay_w = overlay.shape[:2]
    y2 = min(y + overlay_h, frame.shape[0])
    x2 = min(x + overlay_w, frame.shape[1])
    height = y2 - y
    width = x2 - x
    if height <= 0 or width <= 0:
        return frame
    patch = overlay[:height, :width]
    alpha = patch[..., 3:4].astype(np.float32) / 255.0
    color = patch[..., :3].astype(np.float32)
    region = frame[y:y2, x:x2].astype(np.float32)
    frame[y:y2, x:x2] = np.clip(region * (1.0 - alpha) + color * alpha, 0, 255).astype(np.uint8)
    return frame


def list_backgrounds(folder: Path = BACKGROUNDS_DIR) -> list[Path]:
    """Clips .mp4 disponibles para el fondo. Crea la carpeta si todavía no existe."""
    folder.mkdir(parents=True, exist_ok=True)
    return sorted(path for path in folder.glob("*.mp4") if path.is_file())


def pick_background(folder: Path = BACKGROUNDS_DIR) -> Path | None:
    clips = list_backgrounds(folder)
    if not clips:
        return None
    return random.choice(clips)


def _cover_vertical(clip: VideoFileClip, duration: float) -> VideoFileClip:
    """Escala el clip hasta cubrir 9:16, lo recorta al centro y lo ajusta al audio."""
    target_w, target_h = FRAME_SIZE
    clip_w, clip_h = clip.size
    scale = max(target_w / clip_w, target_h / clip_h)
    resized = clip.resized(new_size=(int(clip_w * scale), int(clip_h * scale))).without_audio()
    resized_w, resized_h = resized.size
    x1 = max(0, (resized_w - target_w) // 2)
    y1 = max(0, (resized_h - target_h) // 2)
    covered = resized.cropped(x1=x1, y1=y1, width=target_w, height=target_h)
    if covered.duration + 0.05 < duration:
        covered = covered.with_effects([Loop(duration=duration)])
    if covered.duration > duration:
        covered = covered.subclipped(0, duration)
    return covered


def render_short(
    audio_path: Path,
    destination: Path,
    title: str,
    source: str,
    cues: list[CaptionCue],
    script: str = "",
    background_path: Path | None = None,
) -> Path:
    """Monta el mp3 sobre un fondo 1080x1920, con subtítulos palabra a palabra."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    audio = AudioFileClip(str(audio_path))
    duration = float(audio.duration)
    if not cues:
        cues = _even_cues(script, duration)
    groups = _phrase_groups(cues) if cues else {}
    chrome = _text_overlay(title, source)
    caption_cache: dict[int, np.ndarray] = {}
    caption_top = (FRAME_SIZE[1] - 520) // 2
    raw_background = None
    background = None
    if background_path is not None:
        raw_background = VideoFileClip(str(background_path))
        background = _cover_vertical(raw_background, duration)

    def make_frame(t: float) -> np.ndarray:
        moment = min(max(float(t), 0.0), max(duration - (1 / FPS), 0.0))
        if background is not None:
            frame = background.get_frame(min(moment, max(background.duration - (1 / FPS), 0.0)))
            frame = np.ascontiguousarray(frame)
            if frame.dtype != np.uint8:
                frame = np.clip(frame, 0, 255).astype(np.uint8)
            if frame.ndim == 3 and frame.shape[2] == 4:
                frame = frame[..., :3]
        else:
            frame = _background_frame(moment, duration)
        frame = _blit(frame, chrome, 0, 0)
        if not cues:
            return frame
        active = _cue_at(cues, moment)
        strip = caption_cache.get(active)
        if strip is None:
            strip = _caption_strip(cues, active, groups)
            caption_cache[active] = strip
        return _blit(frame, strip, caption_top, 0)

    video = VideoClip(make_frame, duration=duration).with_fps(FPS).with_audio(audio)
    try:
        video.write_videofile(
            str(destination),
            fps=FPS,
            codec="libx264",
            audio_codec="aac",
            preset="ultrafast",
            threads=2,
            pixel_format="yuv420p",
            logger="bar",
        )
    finally:
        video.close()
        audio.close()
        if background is not None:
            background.close()
        if raw_background is not None and raw_background is not background:
            raw_background.close()

    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError(f"moviepy no escribió el vídeo en {destination}.")
    return destination


def select_article(records: list[dict]) -> dict:
    """Elige la noticia con la fecha de publicación más reciente."""
    if not records:
        raise ValueError("No hay noticias para convertir en vídeo.")
    return max(records, key=_published_at)


def render_news_video(
    article: dict,
    destination: Path = LATEST_VIDEO,
    audio_path: Path | None = None,
    background_path: Path | None = None,
) -> dict:
    """Guión en español, audio con marcas de palabra y mp4 vertical."""
    script = build_script(article)
    if audio_path is None:
        audio_path = destination.with_name("latest_audio.mp3")
    if background_path is None:
        background_path = pick_background()
    audio, cues = generate_audio(script, audio_path)
    title = _as_sentence(localize_to_spanish(str(article.get("title") or ""))).rstrip(".")
    video = render_short(
        audio,
        destination,
        title or "Noticia de tecnología",
        str(article.get("source") or ""),
        cues,
        script,
        background_path,
    )
    return {
        "script": script,
        "audio": audio,
        "video": video,
        "cues": len(cues),
        "background": str(background_path) if background_path else "",
        "source": article.get("source", ""),
        "source_file": article.get("source_file", ""),
    }


def create_test_short() -> dict:
    """Genera audio_test.mp3 y el vídeo vertical video_test.mp4."""
    article = load_latest_article(TEST_AUDIO.parent)
    return render_news_video(article, TEST_VIDEO, TEST_AUDIO)


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except Exception:
            pass


def main() -> int:
    _utf8_stdio()
    print(f"Voz: {DEFAULT_VOICE}")
    print(f"Formato: {FRAME_SIZE[0]}x{FRAME_SIZE[1]}")
    try:
        result = create_test_short()
    except Exception as exc:
        print(f"No se pudo generar el vídeo: {exc}", file=sys.stderr)
        return 1

    audio = result["audio"]
    video = result["video"]
    print(f"Fuente: {result['source']} ({result['source_file']})")
    print(f"Guión: {result['script']}")
    print(f"Palabras sincronizadas: {result['cues']}")
    print(f"Fondo: {result['background'] or 'degradado generado'}")
    print(f"Audio: {audio} ({audio.stat().st_size} bytes)")
    print(f"Vídeo: {video} ({video.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
