"""Clipazos ES: Short diario con los momentazos de streamers hispanos.

Flujo 100 % local y sin claves:
  1. Twitch (API pública de su web): clips en español de las últimas 24 h en los
     juegos más vistos y en las categorías fijas, más los de una lista fija de
     streamers (recientes e históricos). Kick y YouTube: streamers fijos.
  2. Radar: guarda las vistas por juego y por streamer de cada día para detectar
     qué juego se dispara (Short especial de hype) y quién está subiendo.
  3. yt-dlp localiza cada clip y FFmpeg recorta solo el tramo necesario.
  4. El clip entra al Short por vistas por hora y, en Twitch, por lo denso que
     está el chat del VOD en ese segundo. Dentro del clip, el corte persigue el
     salto de sonido y las palabras de reacción, no el tramo que solo está alto.
  5. FFmpeg monta el vertical: fondo desenfocado, clip centrado, título, crédito
     del streamer y subtítulos con la palabra activa en amarillo.

Selección (narrative_segments.py y clip_judge.py): por defecto un solo momento
completo por Short, cortado en frases enteras con la preparación antes del pico
y el remate después; el juez local descarta los tramos sin arco completo.

Antes de descargar o editar un clip se comprueban los derechos (rights.py); después
del montaje, el QA bloqueante (qa.py); y antes de subir, la guardia de publicación
(publication_guard.py: kill switch, cupos y duplicados). --dry-run hace todo menos
llamar a la API de YouTube.

Al arrancar, entorno.py comprueba FFmpeg (>= 7, los filtros del montaje y un
smoke de -/filter_complex) y la fuente Arial Black. Si falla, sale con
entorno_no_apto y no monta ni sube nada.

La configuración del canal vive en clipazos.json.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import clip_judge
import entorno
import narrative_segments
import publication_guard
import qa
import rights
import tiktok_export
from video_editor import (
    ACTIVE_ASS,
    BASE_DIR,
    FPS,
    FRAME_SIZE,
    OUTPUT_DIR,
    SPLIT_TOP,
    CaptionCue,
    _ass_clock,
    _ass_word,
    _ffmpeg_exe,
    _phrase_text,
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
# Sufijo de los archivos de una ejecución --dry-run, para no pisar los de verdad.
DRY_RUN_SUFFIX = "_dryrun"
_RUN_SUFFIX = ""
# Momentos evaluados por el juez en esta ejecución (se guardan en el reporte).
_SELECTION_LOG: list[dict] = []

SAMPLE_RATE = 16_000
FRAME_STEP = 0.25
PAUSE_SECONDS = 0.35
DEAD_AIR = 0.20
PEAK_BONUS = 2.5
# Palabras que marcan un pico. "no" o "que" sueltos no entran: salen en cualquier frase.
SHOUTS = {
    "hostia", "ostia", "dios", "diosmio", "joder", "mierda", "puta", "puto",
    "vamos", "wtf", "omg", "bro", "tio", "tío", "nano", "calla", "callate",
    "cállate", "para", "buah", "buag", "broo",
}
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
SCAN_SIZE = (1280, 720)
FOLLOW_BOX = (1080, 1440)
SOURCE_KEEP = 0.86
INTRO_SECONDS = 3.0
SILENCE_CUT = 0.65
SILENCE_KEEP = 0.20
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
    seconds: float | None = None
    vod_id: str | None = None
    offset: float | None = None
    chat_rate: float | None = None
    historico: bool = False
    login: str | None = None


@dataclass
class Moment:
    candidate: Candidate
    path: Path
    start: float
    length: float
    cues: list[CaptionCue]
    peak: float = 0.0
    layout: Layout | None = None
    cuts: list[tuple[float, float]] = field(default_factory=list)
    segment: dict | None = None
    judgement: dict | None = None
    source_words: list | None = None


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


def _gql_many(queries: list[str]) -> list[dict]:
    """Varias consultas públicas en una sola petición."""
    if not queries:
        return []
    request = urllib.request.Request(
        TWITCH_GQL,
        data=json.dumps([{"query": query} for query in queries]).encode(),
        headers={"Client-ID": TWITCH_WEB_CLIENT_ID, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = json.load(response)
    if isinstance(payload, dict):
        payload = [payload]
    return payload


def _clip_slug(candidate: Candidate) -> str | None:
    if candidate.platform != "Twitch":
        return None
    slug = candidate.url.rstrip("/").rsplit("/", 1)[-1]
    return slug or None


def _chat_burst(edges: list[dict]) -> float | None:
    """Mensajes por segundo en la página de chat, con un extra si van llenos de emotes."""
    points: list[tuple[float, int]] = []
    for edge in edges:
        node = edge.get("node") or {}
        moment = node.get("contentOffsetSeconds")
        if moment is None:
            continue
        fragments = ((node.get("message") or {}).get("fragments")) or []
        emotes = sum(1 for fragment in fragments if fragment.get("emote"))
        points.append((float(moment), emotes))
    if len(points) < 5:
        return None
    points.sort()
    span = max(1.0, points[-1][0] - points[0][0])
    emote_ratio = sum(emotes for _moment, emotes in points) / len(points)
    return (len(points) / span) * (1 + 0.3 * min(emote_ratio, 3.0))


def attach_chat(candidates: list[Candidate]) -> None:
    """Mide el pico de chat del VOD en los clips de Twitch que aún lo tengan.

    El VOD a veces ya no existe: ese clip se queda solo con las vistas por hora.
    """
    pending = [candidate for candidate in candidates if _clip_slug(candidate) and candidate.chat_rate is None]
    pending.sort(key=clip_weight, reverse=True)
    pending = pending[:16]
    if not pending:
        return
    lookups = [
        "query{clip(slug:%s){videoOffsetSeconds durationSeconds video{id}}}" % json.dumps(_clip_slug(candidate))
        for candidate in pending
    ]
    try:
        found = _gql_many(lookups)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"  Aviso: no se pudo leer el momento del clip en Twitch ({exc})", file=sys.stderr)
        return
    comment_queries: list[str] = []
    comment_targets: list[Candidate] = []
    for candidate, item in zip(pending, found):
        clip = ((item or {}).get("data") or {}).get("clip") or {}
        video = clip.get("video") or {}
        if clip.get("videoOffsetSeconds") is None or not video.get("id"):
            continue
        candidate.offset = float(clip["videoOffsetSeconds"])
        candidate.vod_id = str(video["id"])
        if clip.get("durationSeconds"):
            candidate.seconds = float(clip["durationSeconds"])
        spots = [int(candidate.offset)]
        if candidate.seconds and candidate.seconds > 12:
            spots.append(int(candidate.offset + candidate.seconds / 2))
        for spot in spots:
            comment_queries.append(
                "query{video(id:%s){comments(contentOffsetSeconds:%d, first:50)"
                "{edges{node{contentOffsetSeconds message{fragments{text emote{emoteID}}}}}}}}"
                % (json.dumps(candidate.vod_id), spot)
            )
            comment_targets.append(candidate)
    if not comment_queries:
        return
    try:
        pages = _gql_many(comment_queries)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"  Aviso: el chat del VOD no respondió ({exc})", file=sys.stderr)
        return
    bursts: dict[int, float] = {}
    for candidate, item in zip(comment_targets, pages):
        edges = ((((item or {}).get("data") or {}).get("video") or {}).get("comments") or {}).get("edges") or []
        burst = _chat_burst(edges)
        if burst is None:
            continue
        bursts[id(candidate)] = max(bursts.get(id(candidate), 0.0), burst)
    for candidate in pending:
        if id(candidate) in bursts:
            candidate.chat_rate = bursts[id(candidate)]


def clip_weight(candidate: Candidate, now: datetime | None = None) -> float:
    """Vistas por hora. El chat denso multiplica: 5 mensajes/s ya duplica el peso."""
    now = now or datetime.now(timezone.utc)
    if candidate.created is not None:
        hours = max(0.5, (now - candidate.created).total_seconds() / 3600)
    else:
        hours = 12.0
    velocity = math.log1p(candidate.views / hours)
    return velocity * (1.0 + min(2.0, (candidate.chat_rate or 0.0) / 5.0))


def rank_clips(pool: list[Candidate]) -> list[Candidate]:
    """Primero el momento del directo: chat en pico, vistas rápidas y clips del mismo segundo."""
    attach_chat(pool)
    weights = {id(candidate): clip_weight(candidate) for candidate in pool}
    grouped: dict[str, list[Candidate]] = {}
    for candidate in pool:
        if candidate.vod_id and candidate.offset is not None:
            grouped.setdefault(candidate.vod_id, []).append(candidate)
    for group in grouped.values():
        ordered = sorted(group, key=lambda candidate: candidate.offset or 0.0)
        cluster = [ordered[0]]
        clusters = []
        for candidate in ordered[1:]:
            if (candidate.offset or 0.0) - (cluster[-1].offset or 0.0) <= 180:
                cluster.append(candidate)
            else:
                clusters.append(cluster)
                cluster = [candidate]
        clusters.append(cluster)
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            for candidate in cluster:
                weights[id(candidate)] *= 1.2
            hottest = max(cluster, key=lambda candidate: weights[id(candidate)])
            weights[id(hottest)] *= 1.25
    return sorted(pool, key=lambda candidate: weights[id(candidate)], reverse=True)


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
                seconds=float(node["durationSeconds"]),
                login=broadcaster.get("login"),
            )
    return sorted(found.values(), key=lambda candidate: candidate.views, reverse=True)


# Periodos que admite la GQL pública de clips. Recientes e históricos no se mezclan.
_RECENT_PERIODS = ("LAST_DAY", "LAST_WEEK")
_HISTORICAL_PERIODS = ("LAST_MONTH", "ALL_TIME")
# Dos clips del mismo VOD que arrancan así de cerca son el mismo momento.
_MISMO_MOMENTO_SEGUNDOS = 5.0


def _as_int(value, default: int, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = int(value)
    return number if number >= minimum else default


def _login_list(values) -> list[str]:
    if not isinstance(values, list):
        return []
    logins: list[str] = []
    for name in values:
        login = str(name).strip().lstrip("@").lower()
        if login and login not in logins:
            logins.append(login)
    return logins


def _periodos(values, allowed: tuple[str, ...], default: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    raw = values if isinstance(values, list) else [values]
    for period in raw:
        token = str(period).strip().upper()
        if token in allowed and token not in found:
            found.append(token)
    return found or list(default)


def twitch_streamer_plan(settings: dict | None) -> dict:
    """Logins, periodos y cupos de twitch.streamers_fijos / twitch.historicos."""
    settings = settings or {}
    recent = settings.get("recientes") if isinstance(settings.get("recientes"), dict) else {}
    historical = settings.get("historicos") if isinstance(settings.get("historicos"), dict) else {}
    period = str(historical.get("periodo") or "ALL_TIME").strip().upper()
    if period not in _HISTORICAL_PERIODS:
        period = "ALL_TIME"
    return {
        "logins": _login_list(settings.get("streamers_fijos")),
        "recent_periods": _periodos(recent.get("periodos"), _RECENT_PERIODS, _RECENT_PERIODS),
        "recent_first": _as_int(recent.get("por_streamer"), 8, minimum=1),
        "recent_min_views": _as_int(recent.get("vistas_minimas"), 0),
        "historical_active": historical.get("activo") is True,
        "historical_period": period,
        "historical_first": _as_int(historical.get("por_streamer"), 4, minimum=1),
        "historical_min_views": _as_int(historical.get("vistas_minimas"), 50_000),
        "min_clips": _as_int(settings.get("min_clips"), 2, minimum=1),
        "min_clips_fijos": _as_int(settings.get("min_clips_fijos"), 1, minimum=1),
    }


def helix_activo(settings: dict | None) -> bool:
    helix = (settings or {}).get("helix")
    return isinstance(helix, dict) and helix.get("activa") is True


def scout_twitch_helix(
    settings: dict | None, login: str, period: str, first: int
) -> list[Candidate] | None:
    """Hueco para Twitch Helix. No llama a la API ni pide credenciales.

    None: no hay segundo camino y el llamador se queda con lo que trajo la GQL.
    """
    del login, period, first
    if not helix_activo(settings):
        return None
    print(
        "  Aviso: twitch.helix.activa está encendido, pero Helix todavía no está implementado. Se sigue con la GQL.",
        file=sys.stderr,
    )
    return None


def _user_clips_query(login: str, period: str, first: int) -> str:
    return (
        "query{user(login:%s){login clips(criteria:{period:%s}, first:%d)"
        "{edges{node{slug title viewCount durationSeconds createdAt language "
        "videoOffsetSeconds video{id} broadcaster{login displayName} game{name}}}}}}"
    ) % (json.dumps(login), period, int(first))


def _clip_en_espanol(node: dict, language: str) -> bool:
    """True si el idioma falta o es el del canal. Un EN explícito se descarta."""
    raw = node.get("language")
    if raw is None or not str(raw).strip():
        return True
    code = str(raw).strip().lower().replace("_", "-")
    wanted = (language or "es").strip().lower().replace("_", "-") or "es"
    return code == wanted or code.startswith(wanted + "-")


def _vod_de_clip(node: dict) -> tuple[str | None, float | None]:
    video = node.get("video") or {}
    vod_id = str(video["id"]) if video.get("id") else None
    offset = node.get("videoOffsetSeconds")
    if vod_id is None or offset is None:
        return vod_id, None
    try:
        return vod_id, float(offset)
    except (TypeError, ValueError):
        return vod_id, None


def _candidate_from_twitch_clip(
    node: dict, *, historico: bool, login: str, language: str, min_views: int
) -> Candidate | None:
    if not isinstance(node, dict) or not _clip_en_espanol(node, language):
        return None
    slug = str(node.get("slug") or "").strip()
    if not slug:
        return None
    try:
        duration = float(node.get("durationSeconds") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration < 10:
        return None
    try:
        views = int(node.get("viewCount") or 0)
    except (TypeError, ValueError):
        views = 0
    if views < min_views:
        return None
    broadcaster = node.get("broadcaster") or {}
    name = broadcaster.get("displayName") or broadcaster.get("login") or login
    vod_id, offset = _vod_de_clip(node)
    return Candidate(
        platform="Twitch",
        streamer=name,
        title=str(node.get("title") or ""),
        views=views,
        url=f"https://clips.twitch.tv/{slug}",
        key=f"twitch-{slug}",
        game=(node.get("game") or {}).get("name"),
        created=_parse_time(node.get("createdAt")),
        seconds=duration,
        vod_id=vod_id,
        offset=offset,
        historico=historico,
        login=broadcaster.get("login") or login,
    )


def _prefer_clip(current: Candidate, new: Candidate) -> Candidate:
    """El reciente gana al histórico del mismo momento. Si no, mandan las vistas."""
    if current.historico != new.historico:
        chosen = current if not current.historico else new
        other = new if chosen is current else current
    elif new.views > current.views:
        chosen, other = new, current
    else:
        chosen, other = current, new
    if chosen.vod_id is None and other.vod_id:
        chosen.vod_id = other.vod_id
        chosen.offset = other.offset
    return chosen


def _mismo_momento(left: Candidate, right: Candidate) -> bool:
    if not left.vod_id or not right.vod_id or left.vod_id != right.vod_id:
        return False
    if left.offset is None or right.offset is None:
        return False
    return abs(left.offset - right.offset) <= _MISMO_MOMENTO_SEGUNDOS


def dedupe_twitch_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Un slug, una ficha. Con VOD, también un arranque (vod_id + offset).

    attach_chat solo rellena el offset de unos 16 clips, así que los de
    streamers fijos traen video.id y videoOffsetSeconds en la propia consulta.
    """
    by_slug: dict[str, Candidate] = {}
    order: list[str] = []
    for candidate in candidates:
        slug = (_clip_slug(candidate) or candidate.key).casefold()
        if slug not in by_slug:
            by_slug[slug] = candidate
            order.append(slug)
        else:
            by_slug[slug] = _prefer_clip(by_slug[slug], candidate)
    kept: list[Candidate] = []
    for slug in order:
        candidate = by_slug[slug]
        for index, previous in enumerate(kept):
            if _mismo_momento(previous, candidate):
                kept[index] = _prefer_clip(previous, candidate)
                break
        else:
            kept.append(candidate)
    return kept


def merge_twitch_candidates(*groups: list[Candidate]) -> list[Candidate]:
    """Une la búsqueda por categoría con la de streamers fijos."""
    merged = dedupe_twitch_candidates([candidate for group in groups for candidate in group])
    return sorted(merged, key=lambda candidate: candidate.views, reverse=True)


def _avisar_tope_gql(settings: dict) -> None:
    print("  Aviso: Twitch respondió 429. Se guardan los clips ya reunidos.", file=sys.stderr)
    if helix_activo(settings):
        scout_twitch_helix(settings, "", "", 0)
    else:
        print("  Helix está apagado (twitch.helix.activa = false); no hay otro camino.", file=sys.stderr)


def _consultar_usuario(query: str, login: str) -> tuple[str, dict]:
    """('ok', data), ('rate', {}) si hay 429, o ('error', {}) si la GQL falla."""
    try:
        return "ok", _gql(query)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            return "rate", {}
        print(f"  Aviso: Twitch respondió HTTP {exc.code} para @{login}.", file=sys.stderr)
        return "error", {}
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"  Aviso: la GQL de Twitch falló para @{login} ({exc}).", file=sys.stderr)
        return "error", {}


def scout_twitch_streamers(settings: dict, language: str = "es") -> list[Candidate]:
    """Clips de twitch.streamers_fijos: recientes (LAST_DAY/LAST_WEEK) e históricos.

    La GQL es el camino por defecto. Un login que no existe se avisa y se salta.
    Un 429 o un error de esa consulta no tiran la ejecución: se conserva lo ya
    reunido. Helix queda apagado (twitch.helix.activa) y no se pide ninguna clave.
    El idioma se filtra solo cuando el clip lo trae; si falta, el clip se queda.
    No va en criteria.languages para no perder los que vienen sin etiqueta.
    """
    plan = twitch_streamer_plan(settings)
    if not plan["logins"]:
        return []
    jobs: list[tuple[str, int, int, bool]] = [
        (period, plan["recent_first"], plan["recent_min_views"], False) for period in plan["recent_periods"]
    ]
    if plan["historical_active"]:
        jobs.append((plan["historical_period"], plan["historical_first"], plan["historical_min_views"], True))
    found: list[Candidate] = []
    missing: set[str] = set()
    helix_avisado = False
    for login in plan["logins"]:
        for period, first, minimum, historico in jobs:
            if login in missing:
                break
            status, data = _consultar_usuario(_user_clips_query(login, period, first), login)
            if status == "rate":
                _avisar_tope_gql(settings)
                return dedupe_twitch_candidates(found)
            if status != "ok":
                if helix_activo(settings) and not helix_avisado:
                    scout_twitch_helix(settings, login, period, first)
                    helix_avisado = True
                continue
            user = data.get("user") if isinstance(data, dict) else None
            if not user:
                print(f"  Aviso: @{login} no existe en Twitch; se salta.", file=sys.stderr)
                missing.add(login)
                break
            edges = ((user.get("clips") or {}).get("edges")) or []
            for edge in edges:
                candidate = _candidate_from_twitch_clip(
                    (edge or {}).get("node") or {},
                    historico=historico,
                    login=login,
                    language=language,
                    min_views=minimum,
                )
                if candidate is not None:
                    found.append(candidate)
    return dedupe_twitch_candidates(found)


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


def clean_cache(days: int = CACHE_DAYS, keep_videos: int = 0) -> None:
    """Borra clips de la caché viejos y, si keep_videos > 0, Shorts de hace más de esos días.

    Los reportes .json se conservan: el radar y el «no repetir» los necesitan.
    """
    limit = time.time() - days * 86400
    if CACHE_DIR.is_dir():
        for path in CACHE_DIR.glob("*.mp4"):
            if path.stat().st_mtime < limit:
                path.unlink(missing_ok=True)
    if keep_videos <= 0 or not CLIPAZOS_DIR.is_dir():
        return
    limit = time.time() - keep_videos * 86400
    for pattern in ("streamer_*.mp4", "streamer_*.ass", "streamer_*_filter.txt", "preview_*.png"):
        for path in CLIPAZOS_DIR.glob(pattern):
            if path.stat().st_mtime < limit:
                path.unlink(missing_ok=True)


def fetch_media(candidate: Candidate, cache_dir: Path | None = None) -> Path:
    """Localiza el clip. Sin ``cache_dir`` descarga si falta; con él no sale de esa carpeta."""
    directory = cache_dir if cache_dir is not None else CACHE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{_safe_name(candidate.key)}.mp4"
    if destination.is_file() and destination.stat().st_size > 0:
        return destination
    if cache_dir is not None:
        raise FileNotFoundError(f"no está en la caché pedida ({destination.name}); no se descarga")
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


# faster-whisper 1.2.1 acepta hotwords (str). En get_prompt los ignora si
# también llega prefix («Has no effect if prefix is not None»), así que aquí
# no se mandan los dos a la vez.
_HOTWORDS_MAX = 200


def asr_hotwords(config: dict | None, streamer: str | None = None) -> str | None:
    """Pista para el ASR: lista de clipazos.json más el nombre del streamer.

    None si asr.hotwords_activa no está encendido. Como mucho 200 caracteres.
    El nombre del streamer se conserva aunque haya que recortar la lista base.
    """
    raw = (config or {}).get("asr")
    raw = raw if isinstance(raw, dict) else {}
    if raw.get("hotwords_activa") is not True:
        return None
    seen: set[str] = set()
    base_words: list[str] = []
    base = raw.get("hotwords") or []
    if isinstance(base, str):
        base = [part.strip() for part in base.split(",")]
    if not isinstance(base, list):
        base = []
    for item in base:
        word = " ".join(str(item).split())
        if not word or word.casefold() in seen:
            continue
        seen.add(word.casefold())
        base_words.append(word)
    name = " ".join(str(streamer or "").split()).lstrip("@")
    tail = ""
    if name:
        base_words = [word for word in base_words if word.casefold() != name.casefold()]
        tail = name[:_HOTWORDS_MAX].strip()
    chosen: list[str] = []
    for word in base_words:
        parts = [*chosen, word, tail] if tail else [*chosen, word]
        if len(" ".join(parts)) > _HOTWORDS_MAX:
            break
        chosen.append(word)
    text = " ".join([*chosen, tail] if tail else chosen).strip()
    return text or None


def _opciones_transcripcion(
    language: str, config: dict | None, streamer: str | None, prefix: str | None = None
) -> dict:
    """Argumentos de WhisperModel.transcribe. hotwords y prefix no viajan juntos."""
    kwargs: dict = {"language": language, "word_timestamps": True, "vad_filter": True}
    if prefix:
        kwargs["prefix"] = prefix
    hotwords = asr_hotwords(config, streamer)
    if hotwords and "prefix" not in kwargs:
        kwargs["hotwords"] = hotwords
    return kwargs


def transcribe(
    audio: np.ndarray, language: str, config: dict | None = None, streamer: str | None = None
) -> list[CaptionCue]:
    """Transcribe con faster-whisper. Con el flag apagado la llamada no cambia."""
    kwargs = _opciones_transcripcion(language, config, streamer)
    segments, _info = _whisper().transcribe(audio, **kwargs)
    return sync_cues(
        [
            CaptionCue(word.word.strip(), float(word.start), float(word.end))
            for segment in segments
            for word in segment.words
            if word.word.strip()
        ]
    )


def _reaction(word: str) -> float:
    """Cuánto parece un grito o una risa. Una frase normal se queda en cero."""
    plain = re.sub(r"[^\wáéíóúñü]", "", word, flags=re.IGNORECASE).casefold()
    if not plain:
        return 0.0
    score = 0.0
    if re.search(r"(.)\1{2,}", plain) or re.search(r"(?:ja|je|ji){2,}|lol+|xd+", plain):
        score += 1.0
    if plain in SHOUTS or word.endswith(("!", "?")):
        score += 0.8
    return min(score, 1.8)


def _frame_scores(audio: np.ndarray, cues: list[CaptionCue]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Energía, salto de sonido y reacción, por fotograma de FRAME_STEP segundos.

    El salto es lo que cambia de golpe (un grito), no el volumen medio del juego.
    """
    step = int(SAMPLE_RATE * FRAME_STEP)
    frames = max(1, len(audio) // step)
    chunk = audio[: frames * step].reshape(frames, step)
    energy = np.sqrt(np.mean(chunk ** 2, axis=1))
    energy = np.convolve(energy, np.ones(3) / 3, mode="same")
    baseline = np.convolve(energy, np.ones(9) / 9, mode="same")
    jump = np.maximum(0.0, energy - baseline)
    spectrum = np.log1p(np.abs(np.fft.rfft(chunk * np.hanning(step), axis=1)))
    flux = np.zeros(frames, dtype=np.float64)
    if frames > 1:
        flux[1:] = np.maximum(np.diff(spectrum, axis=0), 0.0).sum(axis=1)
    flux = np.convolve(flux, np.ones(3) / 3, mode="same")
    spike = 0.7 * flux / max(float(flux.max()), 1e-6) + 0.3 * jump / max(float(jump.max()), 1e-6)
    heat = np.zeros(frames)
    for cue in cues:
        weight = 0.35 + _reaction(cue.text)
        first = max(0, int(cue.start / FRAME_STEP))
        last = min(frames - 1, int(cue.end / FRAME_STEP))
        if last >= first:
            heat[first : last + 1] = np.maximum(heat[first : last + 1], weight)
    return energy, spike, heat


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


def find_peak(audio: np.ndarray, cues: list[CaptionCue]) -> float:
    """Segundo del clip con el salto de sonido más fuerte, pesado por la reacción."""
    _energy, spike, heat = _frame_scores(audio, cues)
    return int(np.argmax(spike * (0.35 + heat))) * FRAME_STEP


def choose_window(
    audio: np.ndarray, cues: list[CaptionCue], shortest: float, longest: float
) -> tuple[float, float, float]:
    """El momento completo: empieza en una frase, contiene el pico y acaba tras la reacción.

    Devuelve (inicio, duración, pico relativo al inicio). Gana la ventana donde
    el salto de sonido y la reacción están concentrados, no la más larga.
    """
    total = len(audio) / SAMPLE_RATE
    _energy, spike, heat = _frame_scores(audio, cues)
    value = 0.7 * spike + 0.3 * heat - DEAD_AIR * (spike < 0.2)
    prefix = np.concatenate([[0.0], np.cumsum(value)])
    peak_frame = int(np.argmax(spike * (0.35 + heat)))
    peak = peak_frame * FRAME_STEP
    starts, ends = _cut_points(cues, total)
    best: tuple[float, float, float] | None = None
    for start in starts:
        for end in ends:
            span = end - start
            if not shortest <= span <= longest:
                continue
            first, last = int(start / FRAME_STEP), int(end / FRAME_STEP)
            score = (prefix[last] - prefix[first]) / max(1, last - first)
            if start <= peak <= end:
                score += PEAK_BONUS
            if best is None or score > best[0]:
                best = (score, start, span)
    if best is None:
        span = min(total, max(shortest, min(longest, 12.0)))
        start = min(max(0.0, peak - span * 0.45), max(0.0, total - span))
    else:
        _score, start, span = best
    first = int(start / FRAME_STEP)
    last = max(first + 1, int((start + span) / FRAME_STEP))
    local_peak = (first + int(np.argmax(spike[first:last]))) * FRAME_STEP - start
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


def _keep_ranges(
    span: float,
    cues: list[CaptionCue],
    cut: float = SILENCE_CUT,
    keep: float = SILENCE_KEEP,
    sentence_only: bool = False,
) -> list[tuple[float, float]]:
    """Quita los silencios largos y deja un respiro corto, como un jump cut.

    Con sentence_only=True solo corta entre frases: tras una palabra con
    puntuación final, o si el silencio es el doble de ``cut``. Una pausa dentro
    de una frase se respeta.
    """
    if not cues:
        return [(0.0, span)]
    ranges: list[tuple[float, float]] = []
    cursor = 0.0
    previous_text = ""
    for cue in cues:
        gap = cue.start - cursor
        word_end = min(span, cue.end)
        boundary = (
            not sentence_only
            or not ranges
            or previous_text.rstrip().endswith(narrative_segments.SENTENCE_END)
            or gap > 2 * cut
        )
        previous_text = cue.text
        if gap > cut and boundary:
            if ranges:
                left, right = ranges[-1]
                ranges[-1] = (left, min(span, right + keep))
            ranges.append((max(0.0, cue.start - keep), word_end))
        elif not ranges:
            ranges.append((0.0, word_end))
        else:
            left, _right = ranges[-1]
            ranges[-1] = (left, word_end)
        cursor = word_end
    if ranges and span - cursor > cut:
        left, right = ranges[-1]
        ranges[-1] = (left, min(span, right + keep))
    elif ranges:
        left, _right = ranges[-1]
        ranges[-1] = (left, span)
    return [(left, right) for left, right in ranges if right - left > 0.05]


def _place(moment: float, ranges: list[tuple[float, float]]) -> float:
    done = 0.0
    for left, right in ranges:
        if moment <= left:
            return done
        if moment <= right:
            return done + moment - left
        done += right - left
    return done


def _tighten(
    start: float,
    span: float,
    cues: list[CaptionCue],
    peak: float,
    cut: float = SILENCE_CUT,
    keep: float = SILENCE_KEEP,
    sentence_only: bool = False,
) -> tuple[float, list[CaptionCue], float, list[tuple[float, float]]]:
    """Corta el silencio como auto-editor: se queda la voz y un margen corto.

    build_moment lo llama con sentence_only=True y los valores de "seleccion"
    (silencio_corte, silencio_margen) para no cortar a mitad de frase.
    """
    ranges = _keep_ranges(span, cues, cut, keep, sentence_only)
    edited = [
        CaptionCue(cue.text, _place(cue.start, ranges), max(_place(cue.end, ranges), _place(cue.start, ranges) + 0.08))
        for cue in cues
    ]
    length = sum(right - left for left, right in ranges)
    cuts = [(start + left, start + right) for left, right in ranges]
    return length, edited, min(_place(peak, ranges), max(0.0, length - 0.05)), cuts


def select_segment(
    audio: np.ndarray, words: list[CaptionCue], config: dict
) -> tuple[narrative_segments.Segment, float, list[CaptionCue], float, list[tuple[float, float]], clip_judge.Judgement]:
    """Tramo narrativo alrededor del pico, silencios entre frases quitados y nota del juez.

    Devuelve (segmento, duración final, subtítulos, pico relativo, cortes, juicio).
    """
    selection = narrative_segments.settings_from(config)
    total = len(audio) / SAMPLE_RATE
    peak = find_peak(audio, words)
    if words:
        segment = narrative_segments.segment_for_peak(words, peak, selection, total)
    else:
        # Sin voz: la ventana de siempre; el juez la descartará por no tener frases.
        start, span, local_peak = choose_window(audio, words, selection["segundos_min"], selection["segundos_max"])
        segment = narrative_segments.Segment(start, start + span, start + local_peak, None, None, notes=["sin transcripción"])
    start, span = segment.start, segment.length
    cues = [
        CaptionCue(cue.text, cue.start - start, min(cue.end, start + span) - start)
        for cue in words
        if start <= cue.start < start + span
    ]
    length, cues, rel_peak, cuts = _tighten(
        start, span, cues, max(0.0, segment.peak - start),
        selection["silencio_corte"], selection["silencio_margen"], sentence_only=True,
    )
    judgement = clip_judge.judge(words, segment, length, config)
    return segment, length, cues, rel_peak, cuts, judgement


def build_moment(
    candidate: Candidate, shortest: float, longest: float, language: str, config: dict | None = None,
    cache_dir: Path | None = None,
) -> Moment:
    path = fetch_media(candidate, cache_dir)
    audio = load_audio(path)
    if audio.size == 0:
        raise RuntimeError("el clip no tiene audio")
    words = transcribe(audio, language, config, candidate.streamer)
    if config is None:
        # Camino antiguo (sin config): ventana por picos.
        start, span, peak = choose_window(audio, words, shortest, longest)
        cues = [
            CaptionCue(cue.text, cue.start - start, min(cue.end, start + span) - start)
            for cue in words
            if start <= cue.start < start + span
        ]
        length, cues, peak, cuts = _tighten(start, span, cues, peak)
        return Moment(
            candidate, path, start, length, cues, peak, detect_layout(path, start, span), cuts,
            source_words=list(words),
        )
    segment, length, cues, peak, cuts, judgement = select_segment(audio, words, config)
    moment = Moment(candidate, path, segment.start, length, cues, peak, None, cuts, source_words=list(words))
    moment.segment = segment.to_dict()
    moment.judgement = judgement.to_dict()
    if judgement.accepted:
        moment.layout = detect_layout(path, segment.start, segment.length)
    return moment


def same_story(first: Moment, second: Moment, config: dict) -> tuple[bool, str]:
    """Dos momentos solo se unen si son la misma historia.

    Mismo clip, o mismo streamer y mismo VOD con el segundo justo a continuación
    del primero (hueco entre la última palabra de uno y la primera del otro de
    como mucho union_max_hueco_segundos).
    """
    a, b = first.candidate, second.candidate
    if a.key == b.key:
        return True, "mismo clip"
    if a.streamer.casefold() != b.streamer.casefold():
        return False, "otro streamer"
    if not (a.vod_id and b.vod_id and a.vod_id == b.vod_id and a.offset is not None and b.offset is not None):
        return False, "no se puede comprobar que sean del mismo directo"
    seg_a, seg_b = first.segment or {}, second.segment or {}
    end_a = a.offset + float(seg_a.get("last_word_end") or (first.start + first.length))
    start_b = b.offset + float(seg_b.get("first_word_start") or second.start)
    gap = start_b - end_a
    limit = narrative_segments.settings_from(config)["union_max_hueco_segundos"]
    if -0.5 <= gap <= limit:
        return True, f"mismo directo, seguido ({gap:.1f} s de hueco)"
    return False, f"mismo directo pero no seguido ({gap:.1f} s de hueco)"


def _log_moment(candidate: Candidate, moment: Moment | None, status: str, reason: str = "") -> dict:
    judgement = (moment.judgement if moment else None) or {}
    entry = {
        "clip": candidate.key,
        "streamer": candidate.streamer,
        "plataforma": candidate.platform,
        "titulo": candidate.title,
        "estado": status,
        "aceptado": status == "aceptado",
        "puntuacion": judgement.get("puntuacion"),
        "motivos": judgement.get("motivos", []) + ([reason] if reason else []),
        "criterios": judgement.get("criterios", {}),
        "historico": bool(candidate.historico),
    }
    if moment is not None and moment.segment:
        entry["tramo"] = {
            "inicio": round(moment.start, 2),
            "fin": round(moment.segment["end"], 2),
            "duracion_final": round(moment.length, 2),
            "notas": moment.segment.get("notes", []),
        }
    _SELECTION_LOG.append(entry)
    return entry


def collect_moments(
    candidates: list[Candidate], config: dict, language: str
) -> list[Moment]:
    """Por defecto, el primer momento completo que apruebe el juez.

    Se evalúan como mucho seleccion.candidatos_a_evaluar clips (cada uno se
    descarga y se transcribe). Si seleccion.max_momentos > 1, solo se añaden
    momentos de la misma historia (same_story) hasta segundos_objetivo.
    """
    selection = narrative_segments.settings_from(config)
    limit = selection["max_momentos"]
    budget = selection["candidatos_a_evaluar"]
    target = float(config.get("segundos_objetivo", 40))
    shortest = selection["segundos_min"]
    longest = selection["segundos_max"]
    moments: list[Moment] = []
    total = 0.0
    tried = 0
    for candidate in candidates:
        if len(moments) >= limit or tried >= budget:
            break
        if moments and total >= target - 3:
            break
        decision = rights.check_candidate(candidate, config)
        if not decision.allowed:
            print(f"  Salto @{candidate.streamer} ({candidate.platform}) por derechos: {decision.reason}", file=sys.stderr)
            continue
        tried += 1
        try:
            moment = build_moment(candidate, shortest, longest, language, config)
        except Exception as exc:  # noqa: BLE001 - un clip roto no debe parar el vídeo
            print(f"  Salto @{candidate.streamer} ({candidate.platform}): {exc}", file=sys.stderr)
            _log_moment(candidate, None, "error", str(exc))
            continue
        verdict = moment.judgement or {}
        if not verdict.get("aceptado", True):
            print(
                f"  Descartado @{candidate.streamer} ({verdict.get('puntuacion')}): "
                f"{'; '.join(verdict.get('motivos', []))}",
                file=sys.stderr,
            )
            _log_moment(candidate, moment, "descartado")
            continue
        if moments:
            joined, why = same_story(moments[-1], moment, config)
            if not joined:
                print(f"  No se une @{candidate.streamer}: {why} (otra historia)", file=sys.stderr)
                _log_moment(candidate, moment, "no_unido", f"otra historia: {why}")
                continue
        _log_moment(candidate, moment, "aceptado")
        moments.append(moment)
        total += moment.length
        chat = f", chat {candidate.chat_rate:.0f}/s" if candidate.chat_rate else ""
        print(
            f"  #{len(moments)} @{candidate.streamer} ({candidate.platform}, {candidate.views} vistas{chat}): "
            f"«{candidate.title}» -> desde {moment.start:.1f}s, queda en {moment.length:.1f}s "
            f"({moment.length:.1f}s, {len(moment.cuts)} cortes, pico en {moment.peak:.1f}s, {len(moment.cues)} palabras, "
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


def split_caption_margin_v() -> int:
    """MarginV de CaptionSplit (alineación 8, desde el borde superior).

    El valor antiguo, SPLIT_TOP - 55, dejaba el renglón dentro de la webcam.
    Medido con Arial Black, borde 5, sombra 3, \\fs 78-124 y el golpe al 130 %:
    la tinta queda por debajo de MarginV y, con ese margen, invadía la banda
    (la más alta, una palabra con tildes, empezaba en y=797; la cámara acaba
    en y=840). El renglón arranca en SPLIT_TOP y el texto cae en la zona de juego.
    No hay clave de configuración: follow y blur siguen con sus márgenes.
    """
    return SPLIT_TOP


def _pop(text: str) -> str:
    """La palabra entra un 30 % más grande y vuelve a su tamaño en 0,1 s."""
    active = YELLOW[:-1] + "\\fscx130\\fscy130\\t(0,100,\\fscx100\\fscy100)}"
    reset = WHITE[:-1] + "\\fscx100\\fscy100}"
    return text.replace(YELLOW, active).replace(WHITE, reset)


def _live_caption_events(cues: list[CaptionCue]) -> list[tuple[float, float, str]]:
    """Una palabra cada vez, solo mientras se está diciendo.

    No se adelanta la siguiente ni se deja la frase quieta en los silencios.
    """
    events: list[tuple[float, float, str]] = []
    for index, cue in enumerate(cues):
        end = max(cue.end, cue.start + 0.16)
        if index + 1 < len(cues):
            end = min(end, cues[index + 1].start)
        if end <= cue.start:
            end = cue.start + 0.08
        label = _ass_word(cue.text)
        if not label:
            continue
        size = 124 if len(label) <= 7 else 100 if len(label) <= 11 else 78
        events.append((cue.start, end, "{\\fs%d\\fad(20,40)}" % size + _phrase_text([cue.text], 0)))
    return events


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
        f"Style: CaptionSplit,Arial Black,80,{style},1,5,3,8,60,60,{split_caption_margin_v()},1",
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
        for word_start, word_end, text in _live_caption_events(shifted):
            lines.append(
                f"Dialogue: 0,{_ass_clock(word_start)},{_ass_clock(word_end)},{style_name},,0,0,0,,"
                f"{_pop(text)}"
            )
        offset += moment.length
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return destination


def _region(center_x: float, center_y: float, rel_w: float, rel_h: float) -> str:
    """Recorte relativo (0-1) centrado en un punto, sin salirse del fotograma."""
    rel_w, rel_h = min(rel_w, 1.0), min(rel_h, 1.0)
    x = min(max(center_x - rel_w / 2, 0.0), 1.0 - rel_w)
    y = min(max(center_y - rel_h / 2, 0.0), 1.0 - rel_h)
    return f"crop=w='iw*{rel_w:.4f}':h='ih*{rel_h:.4f}':x='iw*{x:.4f}':y='ih*{y:.4f}'"


def _layout_filters(index: int, moment: Moment, source: str, entrance: str) -> list[str]:
    width, height = FRAME_SIZE
    layout = moment.layout or Layout("blur", 16 / 9)
    grade = "eq=contrast=1.08:saturation=1.16:gamma=0.96,unsharp=5:5:0.5:5:5:0.0"
    finish = f"setsar=1,{grade},format=yuv420p,{entrance}[v{index}]"
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
            f"{source}fps={FPS},split[cam{index}][game{index}]",
            f"[cam{index}]{cam},scale={width}:{SPLIT_TOP},setsar=1[c{index}]",
            f"[game{index}]{game},scale={width}:{height - SPLIT_TOP},setsar=1[g{index}]",
            f"[c{index}][g{index}]vstack,{finish}",
        ]
    if layout.kind == "follow" and layout.face:
        x, _y, w, _h = layout.face
        box_w, box_h = FOLLOW_BOX
        region = _region(x + w / 2, SOURCE_KEEP / 2, SOURCE_KEEP * (box_w / box_h) / layout.aspect, SOURCE_KEEP)
        return [
            f"{source}fps={FPS},split[raw{index}][top{index}]",
            blurred,
            f"[top{index}]{region},scale={box_w}:{box_h}[fg{index}]",
            f"[bg{index}][fg{index}]overlay=(W-w)/2:(H-h)/2,{finish}",
        ]
    box = min(height - 2 * VIDEO_TOP, int(width * SOURCE_KEEP / layout.aspect) // 2 * 2)
    return [
        f"{source}fps={FPS},split[raw{index}][top{index}]",
        blurred,
        f"[top{index}]crop=iw:ih*{SOURCE_KEEP}:0:0,scale={width}:{box}:force_original_aspect_ratio=increase,"
        f"crop={width}:{box}[fg{index}]",
        f"[bg{index}][fg{index}]overlay=(W-w)/2:(H-h)/2,{finish}",
    ]


def _assemble_cuts(index: int, moment: Moment) -> tuple[str, list[str]]:
    """Junta los trozos ya sin silencios y deja la voz más presente."""
    ranges = moment.cuts or [(moment.start, moment.start + moment.length)]
    lines: list[str] = []
    video_labels: list[str] = []
    audio_labels: list[str] = []
    for piece, (left, right) in enumerate(ranges):
        lines.append(
            f"[{index}:v]trim=start={left:.3f}:end={right:.3f},setpts=PTS-STARTPTS[v{index}_{piece}]"
        )
        lines.append(
            f"[{index}:a]atrim=start={left:.3f}:end={right:.3f},asetpts=PTS-STARTPTS[a{index}_{piece}]"
        )
        video_labels.append(f"[v{index}_{piece}]")
        audio_labels.append(f"[a{index}_{piece}]")
    lines.append(f"{''.join(video_labels)}concat=n={len(ranges)}:v=1:a=0[vcut{index}]")
    lines.append(
        f"{''.join(audio_labels)}concat=n={len(ranges)}:v=0:a=1,highpass=f=80,"
        f"acompressor=threshold=-18dB:ratio=3:attack=8:release=90:makeup=2,"
        f"loudnorm=I=-14:TP=-1.5:LRA=11,aresample=44100[a{index}]"
    )
    return f"[vcut{index}]", lines


def render_short(
    moments: list[Moment], title: list[list[tuple[str, bool]]], channel: str, destination: Path
) -> Path:
    width, height = FRAME_SIZE
    work = destination.with_suffix("")
    captions = write_short_captions(moments, title, channel, work.with_name(work.name + ".ass"))
    parts: list[str] = []
    for index, moment in enumerate(moments):
        source, cut_filters = _assemble_cuts(index, moment)
        entrance = "fade=t=in:st=0:d=0.25" if index == 0 else "fade=t=in:st=0:d=0.12:color=white"
        parts.extend(cut_filters)
        parts.extend(_layout_filters(index, moment, source, entrance))
    joined = "".join(f"[v{index}][a{index}]" for index in range(len(moments)))
    parts.append(f"{joined}concat=n={len(moments)}:v=1:a=1[vcat][aout]")
    fonts = _filter_path(Path(r"C:\Windows\Fonts"))
    parts.append(f"[vcat]ass=filename='{_filter_path(captions)}':fontsdir='{fonts}'[vout]")
    script = work.with_name(work.name + "_filter.txt")
    script.write_text(";\n".join(parts), encoding="utf-8")

    command = [_ffmpeg_exe(), "-y"]
    for moment in moments:
        command.extend(["-i", str(moment.path)])
    # -/filter_complex lee el archivo. FFmpeg 7 lo documenta (y marca
    # -filter_complex_script como obsoleto); FFmpeg 9 ya no reconoce la opción vieja.
    command.extend(
        [*entorno.opcion_filtro_complejo(script), "-map", "[vout]", "-map", "[aout]",
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
    first_log = len(_SELECTION_LOG)
    moments = collect_moments(candidates, config, config.get("idioma", "es"))
    if not moments:
        print("  Ningún momento completo para este Short.", file=sys.stderr)
        return None
    if all(moment.candidate.historico for moment in moments):
        name = moments[0].candidate.streamer
        title = [[("UN CLÁSICO DE", False)], [(f"@{name.upper()}", True)]]
        headline = f"Un clásico de {name}"
    render_short(moments, title, config.get("canal", "Clipazos ES"), destination)
    post = post_text(moments, headline, extra_tags)
    post["video"] = str(destination)
    post["duracion"] = round(sum(moment.length for moment in moments), 1)
    post["historico"] = any(moment.candidate.historico for moment in moments)
    unique = list({moment.candidate.key: moment.candidate for moment in moments}.values())
    post["clips"] = [candidate.key for candidate in unique]
    post["fuentes"] = [_fuente_reporte(candidate) for candidate in unique]
    post["seleccion"] = _SELECTION_LOG[first_log:]
    post["cortes"], post["picos_short"] = _cut_info(moments)
    print(f"  Vídeo: {destination} ({post['duracion']}s, {destination.stat().st_size} bytes)")
    result = qa.run_qa(destination, config, destination.with_suffix(".ass"))
    post["qa"] = result.to_dict()
    if result.passed:
        print("  QA: ok" + (" (desactivado)" if result.skipped else ""))
        _export_tiktok(post, config)
    else:
        post["no_subir"] = True
        post["bloqueado_por"] = "qa"
        print(f"  QA bloquea la subida: {result.reason}", file=sys.stderr)
    return post


def _cut_info(moments: list[Moment]) -> tuple[list[dict], list[float]]:
    """Tramos usados de cada clip (segundos del clip) y el pico en segundos del Short."""
    cuts: list[dict] = []
    peaks: list[float] = []
    offset = 0.0
    for moment in moments:
        peak = round(offset + moment.peak, 2)
        cuts.append({
            "clip": moment.candidate.key,
            "inicio_clip": round(moment.start, 2),
            "fin_clip": round((moment.segment or {}).get("end", moment.start + moment.length), 2),
            "trozos_clip": [[round(left, 2), round(right, 2)] for left, right in moment.cuts],
            "inicio_short": round(offset, 2),
            "duracion_final": round(moment.length, 2),
            "pico_short": peak,
        })
        peaks.append(peak)
        offset += moment.length
    return cuts, peaks


def _export_tiktok(post: dict, config: dict) -> None:
    """Carpeta lista para TikTok. Nunca bloquea YouTube: un fallo se apunta y se sigue."""
    if not tiktok_export.settings_from(config)["enabled"]:
        return
    try:
        manifest = tiktok_export.export_short(post, config, dry_run=_RUN_SUFFIX == DRY_RUN_SUFFIX)
    except Exception as exc:  # noqa: BLE001 - la exportación es secundaria
        post["tiktok_export"] = {"estado": "error", "motivo": str(exc)}
        print(f"  Aviso: no se pudo exportar para TikTok ({exc})", file=sys.stderr)
        return
    folder = str(Path(manifest["archivos"]["manifest.json"]).parent)
    post["tiktok_export"] = {"estado": "ok", "carpeta": folder, "manifest": manifest["archivos"]["manifest.json"]}
    print(f"  TikTok: listo en {folder} (portada: {manifest['portada']['metodo']})")


def _refresh_tiktok(config: dict, report_path: Path, dry_run: bool) -> None:
    try:
        tiktok_export.refresh_youtube(report_path, config, dry_run)
    except Exception as exc:  # noqa: BLE001
        print(f"  Aviso: no se pudo actualizar el estado de YouTube en export_tiktok ({exc})", file=sys.stderr)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clipazos ES: Short diario de streamers hispanos.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--solo-radar", action="store_true", help="Busca y actualiza el radar sin montar vídeos.")
    parser.add_argument("--sin-hype", action="store_true", help="No monta el Short especial de juego en hype.")
    parser.add_argument("--streamer", help="Short con los mejores clips de un solo streamer.")
    parser.add_argument(
        "--clips", type=int,
        help="Máximo de momentos por vídeo (seleccion.max_momentos); solo se unen si son la misma historia.",
    )
    parser.add_argument("--segundos", type=float, help="Duración objetivo del Short (segundos_objetivo).")
    parser.add_argument("--forzar", action="store_true", help="Rehace los Shorts aunque ya estén hechos hoy.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Hace todo (buscar, descargar, montar, derechos, QA y cupos) menos llamar a la API de YouTube.",
    )
    return parser.parse_args(argv)


def streamer_pool(candidates: list[Candidate], name: str) -> list[Candidate]:
    wanted = name.casefold().lstrip("@")
    pool = [candidate for candidate in candidates if candidate.streamer.casefold() == wanted]
    return sorted(pool, key=lambda candidate: candidate.views, reverse=True)


def _puntos_streamer(pool: list[Candidate], best_of: int) -> tuple[int, int]:
    """Vistas recientes primero. Las históricas solo desempatan, no roban el día."""
    recent = sorted((candidate.views for candidate in pool if not candidate.historico), reverse=True)
    old = sorted((candidate.views for candidate in pool if candidate.historico), reverse=True)
    return (sum(recent[:best_of]), sum(old[:best_of]))


def _minimo_de_pool(name: str, pool: list[Candidate], floors: dict[str, int], default: int) -> int:
    keys = {name.casefold().lstrip("@")}
    for candidate in pool:
        keys.add(candidate.streamer.casefold().lstrip("@"))
        if candidate.login:
            keys.add(candidate.login.casefold().lstrip("@"))
    found = [floors[key] for key in keys if key in floors]
    return min(found) if found else default


def top_streamers(
    candidates: list[Candidate],
    count: int,
    skip: set[str],
    min_clips: int = 2,
    best_of: int = 4,
    min_clips_por_streamer: dict[str, int] | None = None,
) -> list[tuple[str, list[Candidate]]]:
    """Streamers con más vistas en sus mejores clips, cada uno con sus clips.

    El mínimo general sigue en 2. ``min_clips_por_streamer`` baja ese suelo para
    los streamers fijos (por defecto 1): un solo clip bueno ya puede entrar y
    el juez decide si el momento vale.
    """
    floors = {
        str(key).casefold().lstrip("@"): value
        for key, value in (min_clips_por_streamer or {}).items()
    }
    pools: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        if candidate.streamer.casefold() in skip:
            continue
        pools.setdefault(candidate.streamer, []).append(candidate)
    ranked = [
        (name, sorted(pool, key=lambda candidate: candidate.views, reverse=True))
        for name, pool in pools.items()
        if len(pool) >= _minimo_de_pool(name, pool, floors, min_clips)
    ]
    ranked.sort(key=lambda item: _puntos_streamer(item[1], best_of), reverse=True)
    return ranked[:count]


def recently_posted(today: str, days: int) -> set[str]:
    """Streamers con Short en los últimos días, para no repetir siempre al mismo."""
    if days <= 0:
        return set()
    limit = (datetime.strptime(today, "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")
    names: set[str] = set()
    for path in CLIPAZOS_DIR.glob("reporte_*.json"):
        if path.stem.endswith(DRY_RUN_SUFFIX):
            continue
        day = path.stem.split("_")[1]
        if not (limit <= day < today):
            continue
        try:
            shorts = json.loads(path.read_text(encoding="utf-8")).get("shorts", [])
        except (OSError, ValueError):
            continue
        names.update(short["streamer"].casefold() for short in shorts if short.get("streamer"))
    return names


def _dia_reporte(path: Path) -> str | None:
    if path.stem.endswith(DRY_RUN_SUFFIX):
        return None
    parts = path.stem.split("_")
    if len(parts) < 2 or len(parts[1]) != 8 or not parts[1].isdigit():
        return None
    return parts[1]


def _claves_de_candidato(candidate: Candidate) -> set[str]:
    keys: set[str] = set()
    if candidate.key and str(candidate.key).strip():
        keys.add(str(candidate.key).strip().casefold())
    slug = (_clip_slug(candidate) or "").casefold()
    if slug:
        keys.add(slug)
        keys.add(f"twitch-{slug}")
    return keys


def clips_ya_publicados(today: str, days: int, config: dict | None = None) -> set[str]:
    """Clips que no deben volver a salir.

    En la ventana de ``no_repetir_dias`` cuenta cualquier Short de un reporte
    real. El historial de publicaciones (base y shorts con youtube_id) cuenta
    siempre, aunque el clip sea antiguo. Los reportes ``_dryrun`` no cuentan.
    """
    keys: set[str] = set()
    limit = ""
    if days > 0:
        try:
            limit = (datetime.strptime(today, "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")
        except ValueError:
            limit = ""
    if CLIPAZOS_DIR.is_dir():
        for path in CLIPAZOS_DIR.glob("reporte_*.json"):
            day = _dia_reporte(path)
            if day is None:
                continue
            in_window = bool(limit) and limit <= day < today
            try:
                shorts = json.loads(path.read_text(encoding="utf-8")).get("shorts") or []
            except (OSError, ValueError):
                continue
            for short in shorts:
                if not isinstance(short, dict):
                    continue
                if not in_window and not short.get("youtube_id"):
                    continue
                for key in short.get("clips") or []:
                    text = str(key).strip().casefold()
                    if text:
                        keys.add(text)
    try:
        keys.update(publication_guard.PublicationGuard(config or {}).published_clip_keys())
    except (OSError, sqlite3.Error):
        print("  Aviso: no se pudo leer el historial de publicaciones.", file=sys.stderr)
    return keys


def sin_clips_publicados(candidates: list[Candidate], used: set[str]) -> list[Candidate]:
    if not used:
        return list(candidates)
    return [candidate for candidate in candidates if not (_claves_de_candidato(candidate) & used)]


def _fuente_reporte(candidate: Candidate) -> dict:
    info = rights.source_info(candidate)
    info["historico"] = bool(candidate.historico)
    return info


def ordenar_para_short(pool: list[Candidate], config: dict | None = None) -> list[Candidate]:
    """Picos de hoy primero. El mejor histórico ocupa el último hueco que mira el juez."""
    recent = rank_clips([candidate for candidate in pool if not candidate.historico])
    historical = sorted(
        (candidate for candidate in pool if candidate.historico),
        key=lambda candidate: candidate.views,
        reverse=True,
    )
    if not historical:
        return recent
    if not recent:
        return historical
    budget = narrative_segments.settings_from(config)["candidatos_a_evaluar"]
    if budget <= 1:
        return recent + historical
    keep = budget - 1
    return recent[:keep] + historical[:1] + recent[keep:] + historical[1:]


def streamer_short(
    pool: list[Candidate], name: str, config: dict, today: str, game: str | None = None
) -> dict | None:
    """Short de un solo streamer: primero el pico de chat y de sonido, no el clip más largo."""
    name = pool[0].streamer if pool else name
    pool = ordenar_para_short(pool, config)
    recent_count = sum(not candidate.historico for candidate in pool)
    old_count = len(pool) - recent_count
    extra = f" y {old_count} histórico{'s' if old_count != 1 else ''}" if old_count else ""
    print(f"\nShort de @{name}: {recent_count} clips recientes{extra}, ordenados por el pico")
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
        CLIPAZOS_DIR / f"streamer_{_safe_name(name).lower()}_{today}{_RUN_SUFFIX}.mp4", tags,
    )
    if short:
        short["streamer"] = name
    return short


def apply_rights(candidates: list[Candidate], config: dict, label: str) -> list[Candidate]:
    """Quita los clips sin permiso antes de descargar nada."""
    allowed, blocked = rights.filter_candidates(candidates, config)
    if blocked:
        names = sorted({candidate.streamer for candidate, _decision in blocked}, key=str.casefold)
        shown = ", ".join("@" + name for name in names[:8]) + (" ..." if len(names) > 8 else "")
        print(f"Derechos {label}: {len(blocked)} de {len(candidates)} clips bloqueados ({shown})")
        print(f"  Primer motivo: {blocked[0][1].reason}")
    return allowed


def _upload(
    config: dict, report_path: Path, config_path: Path | None = None, dry_run: bool = False
) -> None:
    """Sube a YouTube los Shorts del reporte que aún no estén subidos."""
    upload = config.get("subida_youtube", {})
    if not upload.get("activa"):
        return
    import youtube_upload

    privacy = publication_guard.resolve_privacy(config)
    if dry_run:
        print("\n--dry-run: se comprueba la subida a YouTube sin llamar a la API...")
        youtube_upload.upload_report(
            report_path, privacy, config=config, config_path=config_path or CONFIG_PATH, dry_run=True
        )
        return
    if youtube_upload.is_ready():
        print("\nSubiendo a YouTube...")
        youtube_upload.upload_report(report_path, privacy, config=config, config_path=config_path or CONFIG_PATH)
    else:
        print("\nSubida a YouTube activa pero sin autorizar: ejecuta autorizar_youtube.bat")


def main(argv: list[str] | None = None) -> int:
    global _RUN_SUFFIX
    _utf8_stdio()
    _SELECTION_LOG.clear()
    args = parse_args(argv)
    config = load_config(args.config)
    _RUN_SUFFIX = DRY_RUN_SUFFIX if args.dry_run else ""
    if args.dry_run:
        print("Modo --dry-run: no se llamará a la API de YouTube.")
    if args.clips:
        config["clips_maximos"] = args.clips
        selection = config.get("seleccion") if isinstance(config.get("seleccion"), dict) else {}
        config["seleccion"] = {**selection, "max_momentos": args.clips}
    if args.segundos:
        config["segundos_objetivo"] = args.segundos
    # Antes de buscar, descargar, montar o subir: si el PC no puede montar, el lote se para.
    informe_entorno = entorno.comprobar()
    if not informe_entorno.apto:
        print(informe_entorno.linea_log(), file=sys.stderr)
        return entorno.EXIT_ENTORNO_NO_APTO
    print(informe_entorno.linea_log())
    channel = config.get("canal", "Clipazos ES")
    language = config.get("idioma", "es")
    today = datetime.now().strftime("%Y%m%d")
    excluded = {name.casefold() for name in config.get("excluir_streamers", [])}
    daily_report = CLIPAZOS_DIR / f"reporte_{today}.json"
    if not (args.streamer or args.solo_radar or args.forzar) and daily_report.is_file():
        try:
            done = json.loads(daily_report.read_text(encoding="utf-8")).get("shorts", [])
        except (OSError, ValueError):
            done = []
        if done:
            print(f"{channel}: los Shorts de hoy ya están hechos ({len(done)}). Usa --forzar para rehacerlos.")
            _upload(config, daily_report, args.config, args.dry_run)
            _refresh_tiktok(config, daily_report, args.dry_run)
            return 0
    print(f"{channel}: buscando momentazos de las últimas 24 h y de los streamers fijos...")
    clean_cache(keep_videos=int(config.get("dias_guardar_videos", 14)))

    twitch_settings = config.get("twitch", {})
    plan = twitch_streamer_plan(twitch_settings)
    twitch = scout_twitch(twitch_settings, language)
    de_streamers = scout_twitch_streamers(twitch_settings, language)
    kick = scout_kick(config.get("kick", {}))
    youtube = scout_youtube(config.get("youtube", {}))
    repeat_days = int(config.get("no_repetir_dias", 2))
    usados = clips_ya_publicados(today, repeat_days, config)
    twitch = sin_clips_publicados(twitch, usados)
    de_streamers = sin_clips_publicados(de_streamers, usados)
    recientes_fijos = sum(not candidate.historico for candidate in de_streamers)
    historicos_fijos = sum(candidate.historico for candidate in de_streamers)
    print(
        f"Clips encontrados: Twitch {len(twitch)} por categoría, "
        f"{recientes_fijos} recientes de streamers fijos, {historicos_fijos} históricos, "
        f"Kick {len(kick)}, YouTube {len(youtube)}"
    )

    radar_settings = config.get("radar", {})
    # El radar compara el día. Los virales históricos no entran en esa cuenta.
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

    twitch = merge_twitch_candidates(twitch, de_streamers)
    historicos_encontrados = [candidate for candidate in twitch if candidate.historico]

    # Derechos antes de descargar o editar: el radar usa todos los clips, el resto solo los permitidos.
    twitch = apply_rights(twitch, config, "Twitch")
    kick = apply_rights(kick, config, "Kick")
    youtube = apply_rights(youtube, config, "YouTube")

    report = {
        "canal": channel,
        "fecha": today,
        "hype": [{"juego": game, "vistas": views, "media": round(baseline)} for game, views, baseline in hot],
        "subiendo": [{"streamer": name, "vistas": views, "media": round(baseline)} for name, views, baseline in rising],
        "historicos": [
            {
                "key": candidate.key,
                "streamer": candidate.streamer,
                "titulo": candidate.title,
                "vistas": candidate.views,
                "url": candidate.url,
                "juego": candidate.game or "",
                "historico": True,
            }
            for candidate in historicos_encontrados
        ],
        "shorts": [],
        "seleccion": _SELECTION_LOG,
    }
    if args.streamer:
        pool = streamer_pool(twitch + kick + youtube, args.streamer)
        short = streamer_short(pool, args.streamer.lstrip("@"), config, today)
        if short:
            report["shorts"].append(short)
    elif not args.solo_radar:
        wanted = int(config.get("shorts_por_dia", 1))
        recent = recently_posted(today, repeat_days)
        floors = {login: plan["min_clips_fijos"] for login in plan["logins"]}
        picked = top_streamers(
            twitch, wanted, excluded | recent,
            min_clips=plan["min_clips"], min_clips_por_streamer=floors,
        )
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
            for name, pool in top_streamers(
                in_game, 1, excluded | used,
                min_clips=plan["min_clips"], min_clips_por_streamer=floors,
            ):
                print(f"\nJuego en hype: {game}")
                short = streamer_short(pool, name, config, today, game=game)
                if short:
                    report["shorts"].append(short)

    CLIPAZOS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"_{_safe_name(args.streamer.lstrip('@')).lower()}" if args.streamer else ""
    report_path = CLIPAZOS_DIR / f"reporte_{today}{suffix}{_RUN_SUFFIX}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for short in report["shorts"]:
        print(f"\n{short['titulo']}\n{short['descripcion']}\n{' '.join(short['hashtags'])}")
    print(f"\nReporte: {report_path}")
    if report["shorts"]:
        _upload(config, report_path, args.config, args.dry_run)
        _refresh_tiktok(config, report_path, args.dry_run)
    if not args.solo_radar and not report["shorts"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
