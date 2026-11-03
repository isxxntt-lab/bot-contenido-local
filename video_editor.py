"""Montaje de Shorts con FFmpeg: borde limpio, fundido y zoom lento.

El guión se adapta al español con reglas locales. La voz sale de edge-tts
al +15 %. FFmpeg quema los subtítulos, funde las fotos y aplica un Ken Burns
muy lento sobre el fondo de assets/backgrounds/.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from io import BytesIO
from pathlib import Path

import edge_tts
from PIL import Image


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
BACKGROUNDS_DIR = BASE_DIR / "assets" / "backgrounds"
BROLL_DIR = OUTPUT_DIR / "broll"
TEST_AUDIO = OUTPUT_DIR / "audio_test.mp3"
TEST_VIDEO = OUTPUT_DIR / "video_test.mp4"
LATEST_VIDEO = OUTPUT_DIR / "latest_video.mp4"

# 100 nanosegundos, la unidad de tiempo de los eventos WordBoundary de edge-tts.
_TICKS_PER_SECOND = 10_000_000

# Voz neuronal en español de España. Alternativa: es-MX-JorgeNeural.
DEFAULT_VOICE = "es-ES-AlvaroNeural"
# Ritmo de noticia rápida. edge-tts acepta porcentajes como "+15%".
DEFAULT_RATE = "+15%"
FRAME_SIZE = (1080, 1920)
FPS = 24
BROLL_COUNT = 3
KEN_BURNS_ZOOM = 0.06
FADE_SECONDS = 0.4
STROKE_PX = 4
# Amarillo #FFFF00 en el orden BGR que usa el formato ASS.
ACTIVE_ASS = "00FFFF"
_IMAGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


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

# Palabras que se pintan en verde neón. El resto queda en blanco.
_IMPACT_WORDS = {
    "alerta",
    "ahora",
    "bomba",
    "censura",
    "colapsa",
    "crisis",
    "estragos",
    "explotaron",
    "fallo",
    "fallos",
    "filtra",
    "hackers",
    "inteligencia",
    "masiva",
    "millones",
    "nunca",
    "parcheadas",
    "peligro",
    "prohibido",
    "rompe",
    "secreto",
    "urgente",
    "viral",
    "vulnerabilidades",
}


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
    communicate = edge_tts.Communicate(
        script,
        voice,
        rate=DEFAULT_RATE,
        boundary="WordBoundary",
    )
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


def _plain_word(word: str) -> str:
    return re.sub(r"[^\wáéíóúñü]+", "", word, flags=re.IGNORECASE).casefold()


def _is_impact(word: str) -> bool:
    token = _plain_word(word)
    if token in _IMPACT_WORDS:
        return True
    return any(character.isdigit() for character in word)


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


# token del audio -> (nombre de archivo, búsqueda en inglés para fotos de stock)
_BROLL_QUERIES = {
    "hackers": ("hackers", "hacker server room pexels"),
    "hacker": ("hackers", "hacker server room pexels"),
    "php": ("php", "programming code screen pexels"),
    "php2shell": ("php", "programming code screen pexels"),
    "amazon": ("aws", "data center servers pexels"),
    "aws": ("aws", "data center servers pexels"),
    "internet": ("internet", "fiber optic cables pexels"),
    "vulnerabilidades": ("security", "cyber security digital lock"),
    "vulnerabilidad": ("security", "cyber security digital lock"),
    "explotaron": ("attack", "cyber attack on a computer"),
    "explotando": ("attack", "cyber attack on a computer"),
    "estragos": ("servers", "server room lights"),
    "fallos": ("bug", "software code on a monitor"),
    "fallo": ("bug", "software code on a monitor"),
    "openai": ("ai", "artificial intelligence technology"),
    "chatgpt": ("ai", "artificial intelligence technology"),
    "inteligencia": ("ai", "artificial intelligence technology"),
    "nvidia": ("chip", "computer gpu chip close up"),
    "google": ("google", "data center servers"),
    "microsoft": ("software", "software developer at a computer"),
    "apple": ("phone", "smartphone in hand close up"),
    "meta": ("social", "person using a smartphone social media"),
    "anthropic": ("ai", "artificial intelligence technology"),
    "claude": ("ai", "artificial intelligence technology"),
}


@dataclass
class BrollShot:
    """Una foto de apoyo y el tramo del audio en el que se muestra."""

    token: str
    query: str
    start: float
    end: float
    path: Path
    provider: str


def _broll_choice(word: str) -> tuple[str, str] | None:
    token = _plain_word(word)
    if token.startswith("php"):
        token = "php"
    return _BROLL_QUERIES.get(token)


def _caption_label(word: str) -> str:
    cleaned = re.sub(r"^[^\wáéíóúñü]+|[^\wáéíóúñü]+$", "", word, flags=re.IGNORECASE)
    return (cleaned or word).upper()


def _pexels_urls(query: str) -> list[str]:
    api_key = os.environ.get("PEXELS_API_KEY", "").strip()
    if not api_key:
        return []
    url = "https://api.pexels.com/v1/search?" + urllib.parse.urlencode(
        {"query": query, "per_page": "6", "orientation": "portrait"}
    )
    request = urllib.request.Request(
        url,
        headers={"Authorization": api_key, "User-Agent": _IMAGE_UA},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        print(f"Pexels no respondió: {exc}", file=sys.stderr)
        return []
    urls: list[str] = []
    for photo in payload.get("photos") or []:
        src = photo.get("src") or {}
        image = src.get("portrait") or src.get("large2x") or src.get("large")
        if image:
            urls.append(str(image))
    return urls


_CLEAN_HOSTS = (
    "images.pexels.com",
    "images.unsplash.com",
    "cdn.pixabay.com",
    "upload.wikimedia.org",
)
_BLOCKED_HOSTS = (
    "freepik",
    "dreamstime",
    "vecteezy",
    "ftcdn",
    "alamy",
    "shutterstock",
    "istock",
    "gettyimages",
    "123rf",
    "pngtree",
    "lovepik",
    "depositphotos",
    "bigstock",
    "plus.unsplash",
    "magnific",
)


def _usable_image_url(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.casefold()
    if any(blocked in host for blocked in _BLOCKED_HOSTS):
        return False
    return url.startswith("http")


def _rank_image_url(url: str) -> int:
    host = urllib.parse.urlparse(url).netloc.casefold()
    for index, clean in enumerate(_CLEAN_HOSTS):
        if clean in host:
            return index
    return len(_CLEAN_HOSTS)


def _ddg_urls(query: str) -> list[str]:
    try:
        from ddgs import DDGS
    except ImportError:
        print("Falta el paquete ddgs. Instálalo con pip install ddgs.", file=sys.stderr)
        return []
    collected: list[str] = []
    for variant in (query, f"{query} pexels"):
        try:
            results = DDGS().images(variant, max_results=8, safesearch="moderate")
        except Exception as exc:
            print(f"DuckDuckGo no respondió para {variant!r}: {exc}", file=sys.stderr)
            continue
        for item in results:
            image = str(item.get("image") or "")
            if _usable_image_url(image):
                collected.append(image)
    unique = list(dict.fromkeys(collected))
    unique.sort(key=_rank_image_url)
    return unique[:8]


def _download_image(urls: list[str], destination: Path) -> bool:
    for url in urls:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": _IMAGE_UA, "Referer": "https://duckduckgo.com/"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = response.read(8_000_000)
            image = Image.open(BytesIO(data)).convert("RGB")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            continue
        if min(image.size) < 280:
            continue
        image.thumbnail((1400, 1600), Image.Resampling.LANCZOS)
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination, "JPEG", quality=88)
        return True
    return False


def _image_file_ok(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 5000:
        return False
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except (OSError, ValueError):
        return False


def _save_broll_image(token: str, query: str) -> tuple[Path, str] | None:
    path = BROLL_DIR / f"{token}.jpg"
    if _image_file_ok(path):
        return path, "caché"
    pexels = _pexels_urls(query)
    if pexels and _download_image(pexels, path) and _image_file_ok(path):
        return path, "Pexels"
    if _download_image(_ddg_urls(query), path) and _image_file_ok(path):
        return path, "DuckDuckGo"
    print(f"Sin foto para {token} ({query}).", file=sys.stderr)
    return None


# Temas con foto clara. El resto solo rellena si la noticia no nombra a ninguno.
_BROLL_PRIMARY = {
    "hackers",
    "php",
    "aws",
    "internet",
    "ai",
    "chip",
    "google",
    "software",
    "phone",
    "social",
}


def extract_broll_keywords(cues: list[CaptionCue]) -> list[tuple[str, str]]:
    """Hasta 3 temas, primero los nombres concretos y en orden de narración."""
    primary: list[tuple[str, str]] = []
    extra: list[tuple[str, str]] = []
    seen: set[str] = set()
    for cue in cues:
        choice = _broll_choice(cue.text)
        if choice is None or choice[0] in seen:
            continue
        seen.add(choice[0])
        if choice[0] in _BROLL_PRIMARY:
            primary.append(choice)
        else:
            extra.append(choice)
    chosen = primary[:BROLL_COUNT]
    if len(chosen) < 2:
        for choice in extra:
            if choice[0] in {token for token, _query in chosen}:
                continue
            chosen.append(choice)
            if len(chosen) >= BROLL_COUNT:
                break
    return chosen


def fetch_broll(cues: list[CaptionCue]) -> list[BrollShot]:
    """Descarga las fotos y las reparte en el tiempo de la narración."""
    chosen = extract_broll_keywords(cues)
    if not chosen or not cues:
        return []
    starts: list[tuple[float, str, str]] = []
    for token, query in chosen:
        for cue in cues:
            choice = _broll_choice(cue.text)
            if choice is not None and choice[0] == token:
                starts.append((cue.start, token, query))
                break
    shots: list[BrollShot] = []
    last_end = cues[-1].end
    for index, (start, token, query) in enumerate(starts):
        saved = _save_broll_image(token, query)
        if saved is None:
            continue
        path, provider = saved
        end = starts[index + 1][0] if index + 1 < len(starts) else max(last_end, start + 1.5)
        if end <= start:
            end = start + 1.2
        print(f"B-roll {token}: {query} ({provider})")
        shots.append(
            BrollShot(
                token=token,
                query=query,
                start=start,
                end=end,
                path=path,
                provider=provider,
            )
        )
    return shots


def list_backgrounds(folder: Path = BACKGROUNDS_DIR) -> list[Path]:
    """Clips .mp4 disponibles para el fondo. Crea la carpeta si todavía no existe."""
    folder.mkdir(parents=True, exist_ok=True)
    return sorted(path for path in folder.glob("*.mp4") if path.is_file())


def pick_background(folder: Path = BACKGROUNDS_DIR) -> Path:
    """Elige un clip al azar. Sin un .mp4 en la carpeta no hay vídeo."""
    clips = list_backgrounds(folder)
    if not clips:
        raise FileNotFoundError(
            f"Hace falta al menos un .mp4 en {folder}. "
            "Copia ahí el vídeo de fondo y vuelve a lanzar el editor."
        )
    return random.choice(clips)


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


def write_captions(cues: list[CaptionCue], source: str, duration: float, destination: Path) -> Path:
    """ASS con borde y sombra. BorderStyle 1 no dibuja una caja opaca."""
    events = _caption_events(cues) if cues else []
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {FRAME_SIZE[0]}",
        f"PlayResY: {FRAME_SIZE[1]}",
        "ScaledBorderAndShadow: yes",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Caption,Arial Black,72,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        "-1,0,0,0,100,100,1,0,1,4,3,5,70,70,0,1",
        "Style: Credit,Arial Black,32,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,3,1,2,40,40,72,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for start, end, text in events:
        lines.append(
            f"Dialogue: 0,{_ass_clock(start)},{_ass_clock(end)},Caption,,0,0,0,,{text}"
        )
    credit = _ass_word(source)
    if credit:
        lines.append(
            f"Dialogue: 1,{_ass_clock(0)},{_ass_clock(duration)},Credit,,0,0,0,,{credit}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return destination


def _ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _media_duration(path: Path) -> float:
    result = subprocess.run(
        [_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if match is None:
        raise RuntimeError(f"No se pudo leer la duración de {path.name}.")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _filter_path(path: Path) -> str:
    text = path.resolve().as_posix().replace("\\", "/")
    return text.replace(":", "\\:").replace("'", "\\'")


def _zoom_scale(amount: str) -> str:
    width, height = FRAME_SIZE
    return (
        f"scale=w='trunc({width}*(1+({amount}))/2)*2':"
        f"h='trunc({height}*(1+({amount}))/2)*2':eval=frame,"
        f"crop={width}:{height}"
    )


def _shot_window(index: int, shots: list[BrollShot], duration: float) -> tuple[float, float, float]:
    shot = shots[index]
    span = max(shot.end - shot.start, 0.24)
    fade = min(FADE_SECONDS, span / 3)
    start = shot.start
    end = min(duration, shot.end)
    if index > 0 and shot.start - shots[index - 1].end <= 0.08:
        start = max(0.0, shot.start - fade / 2)
    if index + 1 < len(shots) and shots[index + 1].start - shot.end <= 0.08:
        end = min(duration, shot.end + fade / 2)
    fade = min(FADE_SECONDS, max(0.12, (end - start) / 3))
    return start, end, fade


def _filter_graph(
    shots: list[BrollShot],
    captions: Path,
    duration: float,
) -> str:
    width, height = FRAME_SIZE
    background_zoom = f"{KEN_BURNS_ZOOM:.4f}*t/{duration:.3f}"
    parts = [
        (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},fps={FPS},setsar=1,"
            f"{_zoom_scale(background_zoom)},"
            f"fade=t=in:st=0:d={FADE_SECONDS:.2f},"
            f"eq=brightness=-0.04:saturation=1.06,format=yuv420p[base0]"
        )
    ]
    previous = "base0"
    for index, shot in enumerate(shots):
        start, end, fade = _shot_window(index, shots, duration)
        span = max(shot.end - shot.start, 0.24)
        amount = f"0.0500*max(t-{shot.start:.3f}\\,0)/{span:.3f}"
        fade_out = max(start, end - fade)
        parts.append(
            f"[{index + 1}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},fps={FPS},setsar=1,"
            f"{_zoom_scale(amount)},format=yuva420p,"
            f"fade=t=in:st={start:.3f}:d={fade:.3f}:alpha=1,"
            f"fade=t=out:st={fade_out:.3f}:d={fade:.3f}:alpha=1[img{index}]"
        )
        parts.append(
            f"[{previous}][img{index}]overlay=0:0:"
            f"enable='between(t,{start:.3f},{end:.3f})'[base{index + 1}]"
        )
        previous = f"base{index + 1}"
    fonts = _filter_path(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts")
    subtitles = _filter_path(captions)
    parts.append(f"[{previous}]ass=filename='{subtitles}':fontsdir='{fonts}'[vout]")
    return ";\n".join(parts)


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


def render_short(
    audio_path: Path,
    destination: Path,
    title: str,
    source: str,
    cues: list[CaptionCue],
    script: str = "",
    background_path: Path | None = None,
    broll: list[BrollShot] | None = None,
) -> Path:
    """FFmpeg: fondo con Ken Burns, fotos en fundido y subtítulos solo con borde."""
    del title
    if background_path is None:
        background_path = pick_background()
    destination.parent.mkdir(parents=True, exist_ok=True)
    duration = _media_duration(audio_path)
    if not cues:
        cues = _even_cues(script, duration)
    cues = sync_cues(cues)
    if broll is None:
        broll = fetch_broll(cues)
    captions = write_captions(cues, source, duration, destination.with_name("captions.ass"))
    filter_path = destination.with_name("render_filter.txt")
    filter_path.write_text(_filter_graph(broll, captions, duration), encoding="utf-8")
    build = destination.with_name(destination.stem + "_build.mp4")
    if build.exists():
        build.unlink()
    command = [_ffmpeg_exe(), "-y", "-stream_loop", "-1", "-i", str(background_path)]
    for shot in broll:
        command.extend(
            ["-loop", "1", "-framerate", str(FPS), "-t", f"{duration:.3f}", "-i", str(shot.path)]
        )
    command.extend(
        [
            "-i",
            str(audio_path),
            "-filter_complex_script",
            str(filter_path),
            "-map",
            "[vout]",
            "-map",
            f"{1 + len(broll)}:a:0",
            "-t",
            f"{duration:.3f}",
            "-r",
            str(FPS),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            str(build),
        ]
    )
    print(f"Montaje FFmpeg: {duration:.1f}s, {len(cues)} palabras, {len(broll)} fotos")
    _run_ffmpeg(command)
    os.replace(build, destination)
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg no escribió el vídeo en {destination}.")
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
    cues = sync_cues(cues)
    title = _as_sentence(localize_to_spanish(str(article.get("title") or ""))).rstrip(".")
    shots = fetch_broll(cues)
    video = render_short(
        audio,
        destination,
        title or "Noticia de tecnología",
        str(article.get("source") or ""),
        cues,
        script,
        background_path,
        shots,
    )
    return {
        "script": script,
        "audio": audio,
        "video": video,
        "cues": len(cues),
        "background": str(background_path) if background_path else "",
        "source": article.get("source", ""),
        "source_file": article.get("source_file", ""),
        "broll": [shot.token for shot in shots],
        "broll_provider": [shot.provider for shot in shots],
    }


def create_test_short() -> dict:
    """Genera la prueba en output/latest_video.mp4."""
    article = load_latest_article(OUTPUT_DIR)
    return render_news_video(article, LATEST_VIDEO, OUTPUT_DIR / "latest_audio.mp3")


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
    print(f"Voz: {DEFAULT_VOICE} a {DEFAULT_RATE}")
    print(f"Formato: {FRAME_SIZE[0]}x{FRAME_SIZE[1]}")
    print("Motor: FFmpeg")
    try:
        result = create_test_short()
    except Exception as exc:
        print(f"No se pudo generar el vídeo: {exc}", file=sys.stderr)
        return 1

    audio = result["audio"]
    video = result["video"]
    names = result.get("broll") or []
    providers = result.get("broll_provider") or []
    broll_line = ", ".join(
        f"{name} ({providers[index]})" if index < len(providers) else name
        for index, name in enumerate(names)
    ) or "ninguno"
    print(f"Fuente: {result['source']} ({result['source_file']})")
    print(f"Guión: {result['script']}")
    print(f"Palabras sincronizadas: {result['cues']}")
    print(f"B-roll: {broll_line}")
    print(f"Fondo: {result['background']}")
    print(f"Audio: {audio} ({audio.stat().st_size} bytes)")
    print(f"Vídeo: {video} ({video.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
