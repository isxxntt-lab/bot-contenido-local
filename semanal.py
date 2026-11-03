"""Clipazos ES semanal: el mejor directo de la semana de cada streamer, recortado a lo bueno.

No es un mosaico de clips sueltos: elige UN directo (el VOD cuyos clips suman más
vistas esta semana), localiza dónde se concentran los clips de la comunidad y corta
tramos largos y continuos de ese directo, con contexto antes y después. Dentro de
cada tramo quita los ratos muertos (silencios largos sin hablar) y monta un vídeo
horizontal 1920x1080 para YouTube con intro, tarjeta por parte, subtítulos locales,
miniatura y descripción con capítulos.

Uso:
    python semanal.py                                  # streamers de clipazos.json
    python semanal.py --streamer auronplay             # solo uno
    python semanal.py --streamer auronplay --minutos 3 # prueba corta
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from clipazos import (
    CLIPAZOS_DIR,
    SAMPLE_RATE,
    WHITE,
    YELLOW,
    Candidate,
    _gql,
    _hashtag,
    _parse_time,
    _safe_name,
    _ytdlp,
    fetch_media,
    load_audio,
    load_config,
    transcribe,
)
from video_editor import (
    CaptionCue,
    _ass_clock,
    _caption_events,
    _ffmpeg_exe,
    _filter_path,
    _run_ffmpeg,
    _utf8_stdio,
)

WEEKLY_DIR = CLIPAZOS_DIR / "semanal"
WIDE = (1920, 1080)
WIDE_FPS = 30
INTRO_SECONDS = 3.8
CHAPTER_SECONDS = 4.5
FADE_SECONDS = 0.5
CAPTION_GAP = 1.5
FONT_PATH = r"C:\Windows\Fonts\ariblk.ttf"
DAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre")

DEFAULTS = {
    "minutos": 20,
    "contexto_antes": 50,
    "contexto_despues": 30,
    "union_segundos": 60,
    "tramo_min_segundos": 90,
    "tramo_max_segundos": 360,
    "silencio_max_segundos": 10,
    "vistas_minimas_vod": 300,
}


@dataclass
class VodClip:
    slug: str
    title: str
    views: int
    offset: float
    seconds: float


@dataclass
class Vod:
    id: str
    title: str
    length: float
    created: datetime | None
    game: str | None
    clips: list[VodClip]

    @property
    def views(self) -> int:
        return sum(clip.views for clip in self.clips)

    @property
    def url(self) -> str:
        return f"https://www.twitch.tv/videos/{self.id}"


@dataclass
class Stretch:
    """Tramo continuo del directo [start, end] en segundos del VOD."""

    start: float
    end: float
    clips: list[VodClip] = field(default_factory=list)

    @property
    def length(self) -> float:
        return self.end - self.start

    @property
    def weight(self) -> int:
        return sum(clip.views for clip in self.clips)

    @property
    def top(self) -> VodClip:
        return max(self.clips, key=lambda clip: clip.views)


@dataclass
class Part:
    """Tramo ya descargado: qué trozos se conservan y sus subtítulos en tiempo de salida."""

    stretch: Stretch
    path: Path
    keeps: list[tuple[float, float]]
    cues: list[CaptionCue]

    @property
    def length(self) -> float:
        return sum(end - start for start, end in self.keeps)


def settings_for(config: dict) -> dict:
    merged = dict(DEFAULTS)
    merged.update(config.get("semanal", {}))
    return merged


# --------------------------------------------------------------------------- Twitch


def weekly_vods(login: str) -> tuple[str, list[Vod]]:
    """Clips de los últimos 7 días agrupados por el directo (VOD) del que salen."""
    data = _gql(
        "query{user(login:%s){displayName clips(criteria:{period:LAST_WEEK}, first:100)"
        "{edges{node{slug title viewCount durationSeconds videoOffsetSeconds video{id}}}}}}"
        % json.dumps(login)
    )
    user = data.get("user")
    if not user:
        raise RuntimeError(f"Twitch no conoce al canal {login}")
    name = user.get("displayName") or login
    grouped: dict[str, list[VodClip]] = {}
    for edge in user["clips"]["edges"]:
        node = edge["node"]
        vod = (node.get("video") or {}).get("id")
        if not vod or node.get("videoOffsetSeconds") is None:
            continue
        grouped.setdefault(vod, []).append(
            VodClip(node["slug"], node["title"].strip(), int(node["viewCount"]),
                    float(node["videoOffsetSeconds"]), float(node["durationSeconds"]))
        )
    vods = [Vod(vod, "", 0.0, None, None, clips) for vod, clips in grouped.items()]
    return name, sorted(vods, key=lambda vod: vod.views, reverse=True)


def vod_details(vod: Vod) -> bool:
    """Rellena título, duración y fecha; False si el VOD ya no existe o no se puede ver."""
    data = _gql(
        'query{video(id:%s){title lengthSeconds createdAt game{name}}}' % json.dumps(vod.id)
    )
    video = data.get("video")
    if not video or not video.get("lengthSeconds"):
        return False
    vod.title = (video.get("title") or "").strip()
    vod.length = float(video["lengthSeconds"])
    vod.created = _parse_time(video.get("createdAt"))
    vod.game = (video.get("game") or {}).get("name")
    try:
        with _ytdlp() as downloader:
            downloader.extract_info(vod.url, download=False)
    except Exception as exc:  # noqa: BLE001 - expirado, solo suscriptores, borrado...
        print(f"  VOD {vod.id} no disponible: {str(exc).splitlines()[0][:120]}", file=sys.stderr)
        return False
    return True


def pick_vod(vods: list[Vod], minimum: int) -> Vod | None:
    for vod in vods:
        if vod.views < minimum:
            break
        print(f"  Probando VOD {vod.id}: {len(vod.clips)} clips, {vod.views} vistas de clips")
        if vod_details(vod):
            return vod
    return None


# --------------------------------------------------------------------------- Tramos


def hot_stretches(vod: Vod, settings: dict, max_len: float) -> list[Stretch]:
    """Agrupa los clips cercanos en tramos continuos con contexto antes y después."""
    before = float(settings["contexto_antes"])
    after = float(settings["contexto_despues"])
    join = float(settings["union_segundos"])
    min_len = min(float(settings["tramo_min_segundos"]), max_len)
    stretches: list[Stretch] = []
    for clip in sorted(vod.clips, key=lambda item: item.offset):
        start = max(0.0, clip.offset - before)
        end = min(vod.length, clip.offset + clip.seconds + after)
        if end <= start:
            continue
        last = stretches[-1] if stretches else None
        if last and start - last.end <= join and end - last.start <= max_len:
            last.end = max(last.end, end)
            last.clips.append(clip)
            continue
        if last and start < last.end:
            start = last.end
            if end - start < clip.seconds / 2:
                last.clips.append(clip)
                continue
        stretches.append(Stretch(start, end, [clip]))
    for stretch in stretches:
        if stretch.length < min_len:
            pad = (min_len - stretch.length) / 2
            stretch.start = max(0.0, stretch.start - pad)
            stretch.end = min(vod.length, stretch.start + min_len)
    return _merge_overlaps(stretches, 0.0)


def _merge_overlaps(stretches: list[Stretch], gap: float) -> list[Stretch]:
    merged: list[Stretch] = []
    for stretch in sorted(stretches, key=lambda item: item.start):
        if merged and stretch.start - merged[-1].end <= gap:
            merged[-1].end = max(merged[-1].end, stretch.end)
            merged[-1].clips.extend(stretch.clips)
        else:
            merged.append(Stretch(stretch.start, stretch.end, list(stretch.clips)))
    return merged


def _shrink(stretch: Stretch, length: float) -> Stretch:
    """La ventana de `length` segundos dentro del tramo con más vistas de clips."""
    best: Stretch | None = None
    for clip in stretch.clips:
        start = min(max(stretch.start, clip.offset + clip.seconds / 2 - length / 2), stretch.end - length)
        start = max(0.0, start)
        inside = [item for item in stretch.clips
                  if item.offset >= start - 5 and item.offset + item.seconds <= start + length + 5]
        window = Stretch(start, start + length, inside)
        if inside and (best is None or window.weight > best.weight):
            best = window
    return best or Stretch(stretch.start, stretch.start + length, list(stretch.clips))


def choose_stretches(stretches: list[Stretch], target: float, min_len: float) -> list[Stretch]:
    """Los tramos con más peso hasta llenar el objetivo, en orden cronológico."""
    chosen: list[Stretch] = []
    total = 0.0
    for stretch in sorted(stretches, key=lambda item: item.weight, reverse=True):
        remaining = target - total
        if remaining < min_len * 0.75:
            break
        if stretch.length > remaining * 1.1:
            stretch = _shrink(stretch, max(remaining, min_len))
        chosen.append(stretch)
        total += stretch.length
    return _merge_overlaps(chosen, 1.0)


# --------------------------------------------------------------------------- Partes


def dead_time_cuts(audio: np.ndarray, cues: list[CaptionCue], limit: float) -> list[tuple[float, float]]:
    """Trozos que se conservan: fuera los silencios sin palabras de más de `limit` s."""
    total = len(audio) / SAMPLE_RATE
    step = SAMPLE_RATE // 2
    frames = len(audio) // step
    if frames == 0:
        return [(0.0, total)]
    energy = np.sqrt(np.mean(audio[: frames * step].reshape(frames, step) ** 2, axis=1))
    quiet = energy < 0.5 * max(float(np.median(energy)), 1e-4)
    talking = np.zeros(frames, dtype=bool)
    for cue in cues:
        talking[int(cue.start * 2): min(frames, int(cue.end * 2) + 1)] = True
    dead = quiet & ~talking
    keeps: list[tuple[float, float]] = []
    cursor = 0.0
    index = 0
    while index < frames:
        if not dead[index]:
            index += 1
            continue
        run_end = index
        while run_end < frames and dead[run_end]:
            run_end += 1
        start, end = index / 2, run_end / 2
        if end - start >= limit:
            keeps.append((cursor, start + 1.0))
            cursor = end - 1.0
        index = run_end
    keeps.append((cursor, total))
    return [(start, end) for start, end in keeps if end - start > 0.5]


def _remap(cues: list[CaptionCue], keeps: list[tuple[float, float]]) -> list[CaptionCue]:
    """Pasa los tiempos de las palabras del tramo original al tramo ya recortado."""
    mapped: list[CaptionCue] = []
    offset = 0.0
    for start, end in keeps:
        for cue in cues:
            if start <= cue.start < end:
                mapped.append(CaptionCue(cue.text, cue.start - start + offset,
                                         min(cue.end, end) - start + offset))
        offset += end - start
    return mapped


def build_parts(vod: Vod, name: str, stretches: list[Stretch], settings: dict, language: str) -> list[Part]:
    parts: list[Part] = []
    for number, stretch in enumerate(stretches, start=1):
        candidate = Candidate(
            platform="Twitch",
            streamer=name,
            title=vod.title,
            views=stretch.weight,
            url=vod.url,
            key=f"vod-{vod.id}-{int(stretch.start)}-{int(stretch.length)}",
            section=(stretch.start, stretch.length),
        )
        try:
            path = fetch_media(candidate)
            audio = load_audio(path)
            if audio.size == 0:
                raise RuntimeError("el tramo no tiene audio")
            cues = transcribe(audio, language)
        except Exception as exc:  # noqa: BLE001 - un tramo roto no debe parar el vídeo
            print(f"  Salto la parte {number}: {exc}", file=sys.stderr)
            continue
        keeps = dead_time_cuts(audio, cues, float(settings["silencio_max_segundos"]))
        part = Part(stretch, path, keeps, _remap(cues, keeps))
        parts.append(part)
        cut = len(audio) / SAMPLE_RATE - part.length
        print(f"  Parte {len(parts)}: {_clock(stretch.start)}-{_clock(stretch.end)} del directo, "
              f"peso {stretch.weight} vistas ({len(stretch.clips)} clips), {part.length:.0f}s"
              f"{f' (-{cut:.0f}s de silencios)' if cut > 1 else ''}, {len(part.cues)} palabras")
    return parts


# --------------------------------------------------------------------------- Textos


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _date_label(created: datetime | None) -> str:
    if created is None:
        return ""
    local = created.astimezone()
    return f"{DAYS[local.weekday()]} {local.day} de {MONTHS[local.month - 1]}"


def _plain(text: str) -> str:
    """Sin emojis ni símbolos que Arial Black no tiene (saldrían como cuadrados)."""
    text = re.sub(r"[^\w\s¿?¡!.,:;'\"()\-·&%#@]", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _usable_title(title: str) -> str | None:
    plain = _plain(title)
    letters = re.findall(r"[a-záéíóúüñ]", plain, flags=re.IGNORECASE)
    if len(letters) < 4 or re.fullmatch(r"(clip|r|auron)\s*\d*", plain, flags=re.IGNORECASE):
        return None
    return plain


def _ass_clean(text: str) -> str:
    return text.replace("{", "(").replace("}", ")").replace("\\", "/").strip()


def _part_title(part: Part) -> str | None:
    for clip in sorted(part.stretch.clips, key=lambda item: item.views, reverse=True):
        title = _usable_title(clip.title)
        if title:
            return title
    return None


def _caption_chunks(cues: list[CaptionCue]) -> list[list[CaptionCue]]:
    """Corta en los silencios para que no se quede una frase vieja en pantalla."""
    chunks: list[list[CaptionCue]] = []
    for cue in cues:
        if chunks and cue.start - chunks[-1][-1].end <= CAPTION_GAP:
            chunks[-1].append(cue)
        else:
            chunks.append([cue])
    return chunks


def write_captions(parts: list[Part], vod: Vod, name: str, channel: str, destination: Path) -> Path:
    width, height = WIDE
    total = sum(part.length for part in parts)
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
        f"Style: Intro,Arial Black,96,{style},1,7,4,5,80,80,0,1",
        f"Style: Chapter,Arial Black,44,{style},1,4,2,7,60,60,50,1",
        f"Style: Caption,Arial Black,64,{style},1,4,3,2,120,120,70,1",
        "Style: Brand,Arial Black,30,&H40FFFFFF,&H000000FF,&H40000000,&H00000000,"
        "-1,0,0,0,100,100,2,0,1,3,0,9,50,50,45,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        f"Dialogue: 3,{_ass_clock(0)},{_ass_clock(INTRO_SECONDS)},Intro,,0,0,0,,"
        f"{{\\fad(300,400)}}EL MEJOR DIRECTO\\NDE LA SEMANA\\N{YELLOW}{{\\fs130}}@{name.upper()}{WHITE}"
        f"\\N{{\\fs52}}{_date_label(vod.created).upper()}",
        f"Dialogue: 2,{_ass_clock(0)},{_ass_clock(total)},Brand,,0,0,0,,{channel.upper()}",
    ]
    offset = 0.0
    for number, part in enumerate(parts, start=1):
        start = offset + (INTRO_SECONDS + 0.2 if number == 1 else FADE_SECONDS)
        end = min(offset + part.length - FADE_SECONDS, start + CHAPTER_SECONDS)
        title = _part_title(part)
        card = f"{YELLOW}PARTE {number}{WHITE} · {_clock(part.stretch.start)} DEL DIRECTO"
        if title:
            card += "\\N{\\fs56}" + _ass_clean(title).upper()[:44]
        if end > start:
            lines.append(f"Dialogue: 1,{_ass_clock(start)},{_ass_clock(end)},Chapter,,0,0,0,,"
                         f"{{\\fad(250,350)}}{card}")
        shifted = [CaptionCue(cue.text, cue.start + offset, cue.end + offset) for cue in part.cues]
        for chunk in _caption_chunks(shifted):
            for cue_start, cue_end, text in _caption_events(chunk):
                lines.append(f"Dialogue: 0,{_ass_clock(cue_start)},{_ass_clock(cue_end)},Caption,,0,0,0,,{text}")
        offset += part.length
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return destination


# --------------------------------------------------------------------------- Render


def _between(keeps: list[tuple[float, float]]) -> str:
    return "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in keeps)


def render_weekly(parts: list[Part], vod: Vod, name: str, channel: str, destination: Path) -> Path:
    width, height = WIDE
    work = destination.with_suffix("")
    captions = write_captions(parts, vod, name, channel, work.with_name(work.name + ".ass"))
    graph: list[str] = []
    for index, part in enumerate(parts):
        length = part.length
        fade_out = max(0.0, length - FADE_SECONDS)
        graph.append(
            f"[{index}:v]fps={WIDE_FPS},select='{_between(part.keeps)}',setpts=N/{WIDE_FPS}/TB,"
            f"trim=duration={length:.3f},"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,"
            f"fade=t=in:st=0:d={FADE_SECONDS},fade=t=out:st={fade_out:.3f}:d={FADE_SECONDS}[v{index}]"
        )
        graph.append(
            f"[{index}:a]aselect='{_between(part.keeps)}',asetpts=N/SR/TB,aresample=48000,"
            f"atrim=duration={length:.3f},loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,"
            f"afade=t=in:st=0:d={FADE_SECONDS},afade=t=out:st={fade_out:.3f}:d={FADE_SECONDS}[a{index}]"
        )
    joined = "".join(f"[v{index}][a{index}]" for index in range(len(parts)))
    graph.append(f"{joined}concat=n={len(parts)}:v=1:a=1[vcat][aout]")
    fonts = _filter_path(Path(r"C:\Windows\Fonts"))
    graph.append(
        f"[vcat]drawbox=x=0:y=0:w=iw:h=ih:color=black@0.55:t=fill:enable='lt(t,{INTRO_SECONDS})',"
        f"ass=filename='{_filter_path(captions)}':fontsdir='{fonts}'[vout]"
    )
    script = work.with_name(work.name + "_filter.txt")
    script.write_text(";\n".join(graph), encoding="utf-8")
    command = [_ffmpeg_exe(), "-y", "-v", "error"]
    for part in parts:
        command.extend(["-i", str(part.path)])
    command.extend(
        ["-filter_complex_script", str(script), "-map", "[vout]", "-map", "[aout]",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
         "-r", str(WIDE_FPS), "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
         "-movflags", "+faststart", str(destination)]
    )
    _run_ffmpeg(command)
    captions.unlink(missing_ok=True)
    script.unlink(missing_ok=True)
    return destination


def _outlined(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font, fill: str) -> None:
    draw.text(xy, text, font=font, fill=fill, stroke_width=10, stroke_fill="black")


def _thumbnail_moment(parts: list[Part]) -> tuple[Part, float]:
    """El segundo (dentro del archivo del tramo) del clip más visto del vídeo."""
    best = max(parts, key=lambda part: part.stretch.top.views)
    clip = best.stretch.top
    moment = clip.offset + clip.seconds / 2 - best.stretch.start
    if any(start <= moment < end for start, end in best.keeps):
        return best, moment
    start, end = best.keeps[0]
    return best, (start + end) / 2


def render_thumbnail(parts: list[Part], name: str, destination: Path) -> Path:
    """Miniatura 1280x720: fotograma del clip más visto con el texto en grande."""
    part, source = _thumbnail_moment(parts)
    frame = destination.with_name(destination.stem + "_frame.png")
    _run_ffmpeg([_ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{source:.2f}", "-i", str(part.path),
                 "-frames:v", "1", str(frame)])
    image = Image.open(frame).convert("RGB").resize((1280, 720), Image.LANCZOS)
    frame.unlink(missing_ok=True)
    image = ImageEnhance.Contrast(ImageEnhance.Color(image).enhance(1.3)).enhance(1.1)
    shade = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shade_draw = ImageDraw.Draw(shade)
    for y in range(300, 720):
        shade_draw.line([(0, y), (1280, y)], fill=(0, 0, 0, int(200 * (y - 300) / 420)))
    image = Image.alpha_composite(image.convert("RGBA"), shade).convert("RGB")
    draw = ImageDraw.Draw(image)
    small = ImageFont.truetype(FONT_PATH, 70)
    label = "@" + name.upper()
    size = 140
    big = ImageFont.truetype(FONT_PATH, size)
    while draw.textlength(label, font=big) > 1180 and size > 60:
        size -= 6
        big = ImageFont.truetype(FONT_PATH, size)
    _outlined(draw, (50, 390), "EL MEJOR DIRECTO", small, "white")
    _outlined(draw, (50, 470), label, big, "#FFFF00")
    _outlined(draw, (50, 480 + size), "DE LA SEMANA", small, "white")
    image.save(destination, quality=92)
    return destination


def weekly_post(parts: list[Part], vod: Vod, name: str, login: str) -> dict:
    chapters: list[str] = []
    offset = 0.0
    for number, part in enumerate(parts, start=1):
        title = _part_title(part)
        label = f"Parte {number}" + (f" · {title}" if title else "")
        chapters.append(f"{_clock(offset)} {label} ({_clock(part.stretch.start)} del directo)")
        offset += part.length
    day = _date_label(vod.created)
    vod_title = vod.title or "directo"
    description = [
        f"El mejor directo de {name} de la semana ({day}): «{vod_title}», recortado a lo mejor.",
        "Los tramos salen de donde más clips hizo su comunidad, con contexto y sin ratos muertos.",
        "",
        f"Canal original: https://www.twitch.tv/{login}",
        f"Directo completo: {vod.url}",
    ]
    if len(chapters) >= 3:
        description += ["", "Capítulos:", *chapters]
    else:
        description += ["", "Partes:", *chapters]
    tags = ["#clipazos", _hashtag(name).lower(), "#twitch", "#directo", "#resumen"]
    if vod.game and vod.game not in ("Just Chatting",):
        tags.append(_hashtag(vod.game).lower())
    title = f"EL MEJOR DIRECTO DE {name.upper()} de la semana: {_plain(vod_title) or 'directo'}"
    return {
        "titulo": title[:100],
        "descripcion": "\n".join(description),
        "hashtags": list(dict.fromkeys(tags)),
        "duracion": round(offset, 1),
        "vod": {"id": vod.id, "url": vod.url, "titulo": vod.title, "duracion": vod.length,
                "fecha": vod.created.isoformat() if vod.created else None,
                "vistas_clips": vod.views, "clips": len(vod.clips)},
        "tramos": [
            {"inicio": round(part.stretch.start, 1), "fin": round(part.stretch.end, 1),
             "inicio_hms": _clock(part.stretch.start), "fin_hms": _clock(part.stretch.end),
             "peso": part.stretch.weight, "clips": len(part.stretch.clips),
             "duracion_final": round(part.length, 1),
             "clip_top": part.stretch.top.title}
            for part in parts
        ],
    }


# --------------------------------------------------------------------------- Flujo


def run_streamer(login: str, config: dict, minutes: float) -> dict | None:
    settings = settings_for(config)
    target = minutes * 60
    max_len = min(float(settings["tramo_max_segundos"]), max(60.0, target / 2))
    min_len = min(float(settings["tramo_min_segundos"]), max_len)
    name, vods = weekly_vods(login)
    print(f"\n@{name}: {sum(len(v.clips) for v in vods)} clips en {len(vods)} directos esta semana")
    vod = pick_vod(vods, int(settings["vistas_minimas_vod"]))
    if vod is None:
        print("  Ningún directo de la semana tiene clips suficientes y está disponible.", file=sys.stderr)
        return None
    print(f"  Elegido: «{vod.title}» ({_date_label(vod.created)}, {_clock(vod.length)}), "
          f"{vod.views} vistas en {len(vod.clips)} clips")
    stretches = choose_stretches(hot_stretches(vod, settings, max_len), target, min_len)
    if not stretches:
        print("  No hay tramos calientes en el directo.", file=sys.stderr)
        return None
    parts = build_parts(vod, name, stretches, settings, config.get("idioma", "es"))
    if not parts:
        print("  No se pudo preparar ningún tramo.", file=sys.stderr)
        return None
    WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    video = WEEKLY_DIR / f"semana_{_safe_name(login).lower()}_{stamp}.mp4"
    print(f"  Montando {sum(p.length for p in parts) / 60:.1f} min en 1920x1080...")
    render_weekly(parts, vod, name, config.get("canal", "Clipazos ES"), video)
    thumbnail = render_thumbnail(parts, name, video.with_suffix(".jpg"))
    post = weekly_post(parts, vod, name, login)
    post.update({"video": str(video), "miniatura": str(thumbnail)})
    video.with_suffix(".json").write_text(json.dumps(post, ensure_ascii=False, indent=2), encoding="utf-8")
    video.with_suffix(".txt").write_text(
        f"{post['titulo']}\n\n{post['descripcion']}\n\n{' '.join(post['hashtags'])}\n", encoding="utf-8"
    )
    for part in parts:
        part.path.unlink(missing_ok=True)
    print(f"  Vídeo: {video} ({post['duracion'] / 60:.1f} min, {video.stat().st_size / 1e6:.0f} MB)")
    print(f"  Miniatura: {thumbnail}")
    return post


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clipazos ES: el mejor directo de la semana por streamer.")
    parser.add_argument("--streamer", help="Canal de Twitch (por defecto, los de clipazos.json).")
    parser.add_argument("--minutos", type=float, help="Duración objetivo del vídeo.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    args = parse_args(argv)
    config = load_config()
    settings = settings_for(config)
    minutes = args.minutos or float(settings["minutos"])
    logins = [args.streamer.lstrip("@")] if args.streamer else list(settings.get("streamers", []))
    if not logins:
        print("Añade streamers en clipazos.json -> semanal.streamers o usa --streamer.", file=sys.stderr)
        return 1
    print(f"{config.get('canal', 'Clipazos ES')}: el mejor directo de la semana (~{minutes:g} min por streamer)")
    done = 0
    for login in logins:
        try:
            if run_streamer(login, config, minutes):
                done += 1
        except Exception as exc:  # noqa: BLE001 - un streamer fallido no para al resto
            print(f"  Error con {login}: {exc}", file=sys.stderr)
    return 0 if done else 1


if __name__ == "__main__":
    raise SystemExit(main())
