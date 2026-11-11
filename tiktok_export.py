"""Exportación lista para TikTok de los Shorts aprobados (no sube nada a TikTok).

Por cada Short que aprueban el juez (clip_judge.py) y el QA (qa.py) se crea:

    export_tiktok/<fecha>_<streamer>_<titulo>/
        video.mp4      el Short final (H.264 + AAC, 1080x1920, faststart)
        caption.txt    descripción y hashtags listos para pegar
        cover.jpg      portada sacada de un fotograma del propio vídeo
        manifest.json  datos del clip, del juez, del QA, de la portada y de YouTube

Tras cada export correcto la carpeta se copia también a ``carpeta_visible``
(por defecto ``{DESKTOP}\\Clipazos TikTok``, el Escritorio real del usuario).

La exportación nunca bloquea la subida a YouTube: clipazos.py captura cualquier
error, lo apunta en el reporte y sigue. Se puede usar suelto para exportar un
Short de un reporte ya hecho:

    python tiktok_export.py output/clipazos/reporte_20261109.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import base.permisos as rights
import publication_guard
import qa
from video_editor import BASE_DIR, _ffmpeg_exe

WIDTH, HEIGHT = 1080, 1920
SCAN_SIZE = (270, 480)
CAPTION_LIMIT = 2200  # caracteres máximos de una descripción de TikTok

DEFAULTS = {
    "enabled": True,
    "carpeta": "export_tiktok",
    "carpeta_visible": "{DESKTOP}\\Clipazos TikTok",
    "hashtags_base": ["#clipazos", "#streamers", "#clipsespañol", "#fyp"],
    "max_hashtags": 10,
    "optimizar_video": True,
    "portada_muestras": 9,
    "portada_ventana_segundos": 2.0,
    "timeout_segundos": 300,
}


class ExportError(RuntimeError):
    """No se pudo completar la exportación de un Short."""


def settings_from(config: dict | None) -> dict:
    block = (config or {}).get("tiktok_export")
    block = block if isinstance(block, dict) else {}
    settings = {**DEFAULTS, **{key: value for key, value in block.items() if key in DEFAULTS}}
    settings["enabled"] = bool(settings["enabled"])
    tags = settings["hashtags_base"]
    settings["hashtags_base"] = [str(tag) for tag in tags] if isinstance(tags, list) else list(DEFAULTS["hashtags_base"])
    for key in ("max_hashtags", "portada_muestras"):
        try:
            settings[key] = max(1, int(settings[key]))
        except (TypeError, ValueError):
            settings[key] = DEFAULTS[key]
    for key in ("portada_ventana_segundos", "timeout_segundos"):
        try:
            settings[key] = max(0.0, float(settings[key]))
        except (TypeError, ValueError):
            settings[key] = DEFAULTS[key]
    return settings


def export_root(config: dict | None) -> Path:
    folder = Path(str(settings_from(config)["carpeta"]))
    return folder if folder.is_absolute() else BASE_DIR / folder


def desktop_dir() -> Path:
    """Escritorio real del usuario (también si está en OneDrive o se llama Escritorio).

    En Windows se pregunta al sistema (SHGetFolderPathW, CSIDL_DESKTOPDIRECTORY), que
    funciona igual desde la tarea programada que corre como el mismo usuario.
    """
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            buffer = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buffer) == 0 and buffer.value:
                return Path(buffer.value)
        except Exception:  # noqa: BLE001 - se prueba con las rutas de siempre
            pass
    home = Path.home()
    for candidate in (
        home / "Desktop", home / "Escritorio",
        home / "OneDrive" / "Desktop", home / "OneDrive" / "Escritorio",
    ):
        if candidate.is_dir():
            return candidate
    return home / "Desktop"


def visible_root(config: dict | None) -> Path | None:
    """Carpeta visible donde se copian los exports; None si está desactivada.

    Admite {DESKTOP}, {HOME}, variables de entorno (%USERPROFILE%) y ~. Una ruta
    relativa se toma desde la carpeta del proyecto.
    """
    raw = settings_from(config).get("carpeta_visible")
    if not raw or not str(raw).strip():
        return None
    text = str(raw).strip()
    if "{DESKTOP}" in text:
        text = text.replace("{DESKTOP}", str(desktop_dir()))
    if "{HOME}" in text:
        text = text.replace("{HOME}", str(Path.home()))
    path = Path(os.path.expanduser(os.path.expandvars(text)))
    return path if path.is_absolute() else BASE_DIR / path


def copy_to_visible(folder: Path, config: dict | None) -> dict:
    """Copia la carpeta del export a la carpeta visible. Nunca lanza: devuelve el resultado."""
    try:
        root = visible_root(config)
        if root is None:
            return {"estado": "desactivada"}
        target = root / folder.name
        target.mkdir(parents=True, exist_ok=True)
        for item in folder.iterdir():
            if item.is_file():
                shutil.copy2(item, target / item.name)
        return {"estado": "ok", "carpeta": str(target)}
    except Exception as exc:  # noqa: BLE001 - la copia visible es solo una comodidad
        return {"estado": "error", "motivo": str(exc)}


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def slug(text: str, limit: int = 40) -> str:
    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    plain = re.sub(r"[^a-zA-Z0-9]+", "-", plain).strip("-").lower()
    return plain[:limit].strip("-") or "short"


def madrid_now(now: datetime | None = None) -> datetime:
    """Hora actual en Europe/Madrid (con tzdata o con la regla de la UE si falta)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        return now.astimezone(ZoneInfo("Europe/Madrid"))
    except Exception:  # noqa: BLE001 - Windows sin tzdata
        offset = publication_guard.madrid_offset(now)
        return now.astimezone(timezone(offset, "CET" if offset.total_seconds() == 3600 else "CEST"))


def _run(command: list[str], timeout: float, binary: bool = False) -> subprocess.CompletedProcess:
    try:
        if binary:
            return subprocess.run(command, capture_output=True, timeout=timeout or None)
        return subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout or None
        )
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise ExportError(f"{Path(command[0]).name} no se pudo ejecutar ({exc})") from exc


def _short_streamer(short: dict) -> str:
    """Nombre de carpeta: tema o serie del formato nuevo, o el streamer de un reporte viejo."""
    for key in ("tema", "serie", "streamer"):
        value = short.get(key)
        if value:
            return str(value)
    names = [source.get("streamer") for source in short.get("fuentes", []) if source.get("streamer")]
    return names[0] if names else "pieza"


# ---------------------------------------------------------------------------
# Vídeo
# ---------------------------------------------------------------------------


def _top_atoms(path: Path) -> list[str]:
    """Átomos de primer nivel del MP4, en orden (para ver si moov va antes que mdat)."""
    atoms: list[str] = []
    size_total = path.stat().st_size
    with path.open("rb") as handle:
        position = 0
        while position + 8 <= size_total and len(atoms) < 64:
            handle.seek(position)
            header = handle.read(8)
            size = int.from_bytes(header[:4], "big")
            kind = header[4:8].decode("latin-1")
            if size == 1:
                size = int.from_bytes(handle.read(8), "big")
            elif size == 0:
                size = size_total - position
            if size < 8:
                break
            atoms.append(kind)
            position += size
    return atoms


def inspect_video(path: Path, timeout: float = 120) -> dict:
    """Códecs, tamaño, duración y faststart. Usa ffprobe si está; si no, ffmpeg -i."""
    info: dict = {"herramienta": None, "video_codec": None, "audio_codec": None,
                  "ancho": None, "alto": None, "duracion": None}
    tool = qa.ffprobe_path()
    if tool:
        result = _run([tool, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], timeout)
        if result.returncode != 0:
            raise ExportError(f"ffprobe falló: {result.stderr.strip()[-300:]}")
        data = json.loads(result.stdout or "{}")
        streams = data.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), {})
        audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
        duration = data.get("format", {}).get("duration")
        info.update(
            herramienta="ffprobe", video_codec=video.get("codec_name"), audio_codec=audio.get("codec_name"),
            ancho=video.get("width"), alto=video.get("height"), duracion=float(duration) if duration else None,
        )
    else:
        result = _run([_ffmpeg_exe(), "-hide_banner", "-i", str(path)], timeout)
        text = result.stderr
        if "Invalid data" in text or "No such file" in text:
            raise ExportError(f"ffmpeg no puede leer {path.name}")
        video = re.search(r"Video:\s*(\w+).*?(\d{2,5})x(\d{2,5})", text)
        audio = re.search(r"Audio:\s*(\w+)", text)
        duration = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        info.update(
            herramienta="ffmpeg",
            video_codec=video[1] if video else None,
            audio_codec=audio[1] if audio else None,
            ancho=int(video[2]) if video else None,
            alto=int(video[3]) if video else None,
            duracion=(int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])) if duration else None,
        )
    atoms = _top_atoms(path)
    info["faststart"] = "moov" in atoms and "mdat" in atoms and atoms.index("moov") < atoms.index("mdat")
    return info


def _tiktok_ready(info: dict) -> bool:
    return (
        info.get("video_codec") == "h264"
        and info.get("audio_codec") == "aac"
        and info.get("ancho") == WIDTH
        and info.get("alto") == HEIGHT
        and bool(info.get("faststart"))
    )


def export_video(source: Path, target: Path, settings: dict) -> dict:
    """Copia el Short para TikTok. Si hace falta lo convierte; si algo falla, copia el original."""
    timeout = float(settings["timeout_segundos"])
    original = inspect_video(source, timeout)
    if not settings["optimizar_video"] or _tiktok_ready(original):
        shutil.copy2(source, target)
        return {"metodo": "copia", "verificacion": original}
    partial = target.with_name(target.stem + ".part.mp4")
    if original.get("video_codec") == "h264" and original.get("audio_codec") == "aac" \
            and original.get("ancho") == WIDTH and original.get("alto") == HEIGHT:
        method = "remux_faststart"
        codec = ["-c", "copy"]
    else:
        method = "recodificado"
        codec = ["-vf", f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
                        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "160k", "-ar", "44100"]
    note = ""
    try:
        result = _run([_ffmpeg_exe(), "-y", "-v", "error", "-i", str(source), *codec,
                       "-movflags", "+faststart", str(partial)], timeout)
        if result.returncode != 0:
            raise ExportError(f"ffmpeg falló: {result.stderr.strip()[-300:]}")
        checked = inspect_video(partial, timeout)
        same_length = (
            checked.get("duracion") is None or original.get("duracion") is None
            or abs(checked["duracion"] - original["duracion"]) <= 0.5
        )
        if not (_tiktok_ready(checked) and same_length):
            raise ExportError(f"la copia optimizada no pasa la verificación ({checked})")
        partial.replace(target)
        return {"metodo": method, "verificacion": checked}
    except ExportError as exc:
        note = str(exc)
    finally:
        partial.unlink(missing_ok=True)
    shutil.copy2(source, target)
    return {"metodo": "copia", "aviso": f"no se optimizó, se copió el original: {note}", "verificacion": original}


# ---------------------------------------------------------------------------
# Portada
# ---------------------------------------------------------------------------


def _face_detector():
    if not qa.FACE_MODEL.is_file():
        return None
    try:
        import cv2

        return cv2.FaceDetectorYN.create(str(qa.FACE_MODEL), "", SCAN_SIZE, 0.6)
    except Exception:  # noqa: BLE001 - sin OpenCV o modelo roto: portada sin caras
        return None


def _grab(video: Path, second: float, size: tuple[int, int], timeout: float):
    import numpy as np

    width, height = size
    result = _run(
        [_ffmpeg_exe(), "-v", "error", "-ss", f"{second:.3f}", "-i", str(video), "-frames:v", "1",
         "-vf", f"scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        timeout, binary=True,
    )
    raw = result.stdout or b""
    if len(raw) < width * height * 3:
        return None
    return np.frombuffer(raw[: width * height * 3], np.uint8).reshape(height, width, 3)


def _sharpness(frame) -> float:
    import numpy as np

    gray = frame.mean(axis=2)
    return float(np.abs(np.diff(gray, axis=0)).mean() + np.abs(np.diff(gray, axis=1)).mean())


def cover_candidates(duration: float, peaks: list[float], settings: dict) -> list[float]:
    """Segundos a probar: alrededor del pico primero; repartidos por el vídeo si no hay pico."""
    low, high = min(0.5, duration / 2), max(0.0, duration - 0.3)

    def clamp(value: float) -> float:
        return round(min(max(value, low), high), 2)

    samples = int(settings["portada_muestras"])
    window = float(settings["portada_ventana_segundos"])
    times: list[float] = []
    for peak in peaks:
        steps = max(1, samples - 1)
        times += [clamp(peak - window + 2 * window * index / steps) for index in range(samples)]
    spread = max(3, samples // 2)
    times += [clamp(duration * (index + 1) / (spread + 1)) for index in range(spread)]
    return list(dict.fromkeys(times))


def choose_cover(video: Path, duration: float, peaks: list[float], settings: dict) -> dict:
    """Elige el fotograma de portada del propio vídeo.

    Prioridad: fotograma con la cara más grande y clara (YuNet) cerca del pico;
    si no hay caras, el fotograma más nítido cerca del pico; sin pico, el más
    nítido del vídeo.
    """
    timeout = float(settings["timeout_segundos"])
    detector = _face_detector()
    window = max(float(settings["portada_ventana_segundos"]), 0.1)
    best: dict | None = None
    for second in cover_candidates(duration, peaks, settings):
        frame = _grab(video, second, SCAN_SIZE, timeout)
        if frame is None:
            continue
        face = 0.0
        if detector is not None:
            import numpy as np

            _ok, faces = detector.detect(np.ascontiguousarray(frame))
            for found in faces if faces is not None else []:
                area = float(found[2] * found[3]) / (SCAN_SIZE[0] * SCAN_SIZE[1])
                face = max(face, float(found[-1]) * min(1.0, area * 20) ** 0.5)
        distance = min((abs(second - peak) for peak in peaks), default=None)
        near = 0.0 if distance is None else max(0.0, 1.0 - distance / (2 * window))
        sharp = min(1.0, _sharpness(frame) / 20.0)
        score = (2.0 if face > 0 else 0.0) + face + 0.6 * near + 0.3 * sharp
        if best is None or score > best["puntuacion"]:
            best = {"segundo": second, "puntuacion": round(score, 3), "cara": round(face, 3),
                    "cerca_del_pico": distance is not None and distance <= window}
    if best is None:
        raise ExportError("no se pudo sacar ningún fotograma del vídeo para la portada")
    if best["cara"] > 0:
        method = "cara_cerca_del_pico" if best["cerca_del_pico"] else "cara"
    elif peaks:
        method = "pico" if best["cerca_del_pico"] else "fotograma_nitido"
    else:
        method = "fotograma_nitido"
    return {**best, "metodo": method, "detector_caras": "yunet" if detector is not None else "no_disponible"}


def write_cover(video: Path, second: float, target: Path, timeout: float) -> None:
    result = _run(
        [_ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{second:.3f}", "-i", str(video), "-frames:v", "1",
         "-vf", f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
                f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1",
         "-q:v", "2", str(target)],
        timeout,
    )
    if result.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        raise ExportError(f"no se pudo guardar la portada: {result.stderr.strip()[-300:]}")


# ---------------------------------------------------------------------------
# Texto y manifiesto
# ---------------------------------------------------------------------------


def _tag(text: str) -> str:
    return "#" + re.sub(r"[^\wáéíóúñü]+", "", str(text), flags=re.IGNORECASE).lower()


def build_caption(short: dict, settings: dict) -> str:
    """Descripción con crédito al streamer y hashtags, solo con datos del pipeline."""
    lines = [str(short.get("titulo") or "").strip()]
    sources = short.get("fuentes") or []
    if sources:
        lines.append("")
        for source in sources:
            name = source.get("streamer") or "streamer"
            platform = source.get("platform") or ""
            title = (source.get("title") or "").strip()
            game = (source.get("game") or "").strip()
            text = f"🎬 Clip de @{name}" + (f" en {platform}" if platform else "")
            if title:
                text += f": «{title}»"
            if game:
                text += f" ({game})"
            lines.append(text)
        lines.append("")
        lines.append("Créditos al streamer original:")
        for source in sources:
            credit = f"@{source.get('streamer') or 'streamer'}"
            if source.get("platform"):
                credit += f" ({source['platform']})"
            if source.get("url"):
                credit += f" · {source['url']}"
            lines.append(credit)
    tags = [_tag(tag) for tag in settings["hashtags_base"] + list(short.get("hashtags") or [])]
    tags += [_tag(source["platform"]) for source in sources if source.get("platform")]
    tags += [_tag(source["streamer"]) for source in sources if source.get("streamer")]
    tags = [tag for tag in dict.fromkeys(tags) if len(tag) > 1][: int(settings["max_hashtags"])]
    body = "\n".join(lines).strip()
    tag_line = " ".join(tags)
    room = CAPTION_LIMIT - len(tag_line) - 2
    if len(body) > room:
        body = body[: max(0, room - 1)].rstrip() + "…"
    return f"{body}\n\n{tag_line}\n"


def _judge_info(short: dict) -> list[dict]:
    accepted = [entry for entry in short.get("seleccion") or [] if entry.get("aceptado")]
    return [
        {
            "clip": entry.get("clip"),
            "puntuacion": entry.get("puntuacion"),
            "motivos": entry.get("motivos", []),
            "criterios": entry.get("criterios", {}),
        }
        for entry in accepted
    ]


def _veredicto_aceptado(veredicto: object) -> bool:
    """True si el juez que se le pasa dio el visto bueno.

    Acepta un dict con ``aceptado``, un objeto con ``accepted`` (el
    ``Judgement`` de clip_judge) o una lista de esos veredictos. Una lista
    vacía no aprueba.
    """
    if veredicto is None:
        return False
    if isinstance(veredicto, dict):
        if "aceptado" in veredicto:
            return bool(veredicto.get("aceptado"))
        if "accepted" in veredicto:
            return bool(veredicto.get("accepted"))
        return False
    if isinstance(veredicto, (list, tuple)):
        return any(_veredicto_aceptado(item) for item in veredicto)
    accepted = getattr(veredicto, "accepted", None)
    if accepted is not None:
        return bool(accepted)
    return False


def approval(short: dict, juez: object | None = None) -> tuple[bool, str]:
    """Solo se exporta si el QA pasó y el juez que se le pasa aprobó.

    ``juez`` es el veredicto de quien llama (juez de guion, o el local). Si no
    se pasa, se mira ``short['juez']`` y, en último caso, ``short['seleccion']``
    para los reportes del formato de streamers que ya estaban en disco.
    """
    result = short.get("qa") or {}
    if not result.get("ok"):
        return False, "el QA no aprobó el Short"
    if short.get("bloqueado_por") == "qa":
        return False, "el QA bloqueó el Short"
    veredicto = juez if juez is not None else short.get("juez")
    if veredicto is None:
        veredicto = short.get("seleccion")
    if not _veredicto_aceptado(veredicto):
        return False, "ningún momento aprobado por el juez"
    video = Path(short.get("video") or "")
    if not video.is_file():
        return False, f"no existe el vídeo {video}"
    return True, ""


def youtube_status(short: dict, config: dict | None, dry_run: bool) -> dict:
    """Estado de YouTube que se conoce al exportar (derechos) o tras la subida."""
    status: dict = {}
    if short.get("youtube_id"):
        status.update(estado="subido", id=short["youtube_id"], url=short.get("youtube_url"),
                      privacidad=short.get("youtube_privacidad"))
        return status
    if short.get("publicacion_bloqueada"):
        reason = str(short["publicacion_bloqueada"])
        status.update(estado="bloqueado", motivo=reason,
                      bloqueado_por="derechos" if reason.startswith("derechos") else "publication_guard")
        return status
    upload = (config or {}).get("subida_youtube", {})
    try:
        decision = rights.check_short(short, config)
        status["derechos"] = decision.to_dict()
        if not decision.allowed:
            status.update(estado="bloqueado", bloqueado_por="derechos", motivo=decision.reason)
            return status
    except Exception as exc:  # noqa: BLE001 - solo es informativo
        status["derechos_error"] = str(exc)
    if dry_run:
        status["estado"] = "dry_run_no_se_sube"
    elif not upload.get("activa"):
        status["estado"] = "subida_desactivada"
    else:
        status["estado"] = "pendiente"
    return status


def folder_name(short: dict, when: datetime, dry_run: bool) -> str:
    name = f"{when:%Y%m%d}_{slug(_short_streamer(short), 24)}_{slug(short.get('titulo') or '', 40)}"
    return name + ("_dryrun" if dry_run else "")


def export_short(
    short: dict,
    config: dict | None,
    *,
    dry_run: bool = False,
    root: Path | None = None,
    now: datetime | None = None,
    juez: object | None = None,
) -> dict:
    """Crea la carpeta de exportación del Short. Devuelve el manifiesto.

    Lanza ExportError si el Short no está aprobado o algo falla; clipazos.py lo
    captura para no tocar la subida a YouTube.
    """
    settings = settings_from(config)
    ok, reason = approval(short, juez)
    if not ok:
        raise ExportError(f"no se exporta: {reason}")
    when = madrid_now(now)
    root = Path(root) if root else export_root(config)
    folder = root / folder_name(short, when, dry_run)
    folder.mkdir(parents=True, exist_ok=True)
    source = Path(short["video"])
    paths = {name: folder / name for name in ("video.mp4", "caption.txt", "cover.jpg", "manifest.json")}

    video = export_video(source, paths["video.mp4"], settings)
    duration = (video.get("verificacion") or {}).get("duracion") or float(short.get("duracion") or 0) or 1.0
    peaks = [float(value) for value in short.get("picos_short") or [] if value is not None]
    cover = choose_cover(paths["video.mp4"], duration, peaks, settings)
    write_cover(paths["video.mp4"], cover["segundo"], paths["cover.jpg"], float(settings["timeout_segundos"]))
    caption = build_caption(short, settings)
    paths["caption.txt"].write_text(caption, encoding="utf-8")

    sources = short.get("fuentes") or []
    manifest = {
        "id": folder.name,
        "fecha": when.isoformat(timespec="seconds"),
        "zona_horaria": "Europe/Madrid",
        "dry_run": dry_run,
        "titulo": short.get("titulo"),
        "streamer": _short_streamer(short),
        "plataforma": sources[0].get("platform") if sources else None,
        "url_fuente": sources[0].get("url") if sources else None,
        "fuentes": sources,
        "clips": short.get("clips", []),
        "cortes": short.get("cortes", []),
        "duracion": round(duration, 2),
        "juez": _judge_info(short),
        "qa": short.get("qa"),
        "portada": cover,
        "video": {key: value for key, value in video.items()},
        "hashtags": caption.strip().splitlines()[-1].split(),
        "youtube": youtube_status(short, config, dry_run),
        "archivos": {name: str(path) for name, path in paths.items()},
        "video_original": str(source),
        "subido_a_tiktok": False,
    }
    paths["manifest.json"].write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest["copia_visible"] = copy_to_visible(folder, config)
    _write_manifest(paths["manifest.json"], manifest)
    if manifest["copia_visible"].get("estado") == "error":
        print(f"  Aviso: no se pudo copiar a la carpeta visible ({manifest['copia_visible']['motivo']})", file=sys.stderr)
    return manifest


def _write_manifest(path: Path, manifest: dict) -> None:
    """Escribe el manifest en export_tiktok/ y, si hay copia visible, también allí."""
    text = json.dumps(manifest, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")
    visible = manifest.get("copia_visible") or {}
    if visible.get("estado") == "ok":
        try:
            (Path(visible["carpeta"]) / path.name).write_text(text, encoding="utf-8")
        except OSError as exc:
            visible.update(estado="error", motivo=f"no se pudo actualizar el manifest visible ({exc})")
            path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def refresh_youtube(report_path: Path, config: dict | None, dry_run: bool = False) -> int:
    """Tras la subida, copia el estado de YouTube del reporte a cada manifest.json."""
    try:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    updated = 0
    for short in report.get("shorts", []):
        export = short.get("tiktok_export") or {}
        manifest_path = Path(export.get("manifest") or "")
        if export.get("estado") != "ok" or not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["youtube"] = youtube_status(short, config, dry_run)
            _write_manifest(manifest_path, manifest)
            updated += 1
        except (OSError, ValueError):
            continue
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Exporta para TikTok los Shorts aprobados de un reporte (no sube nada).")
    parser.add_argument("reporte", type=Path, help="reporte_<fecha>.json de output/clipazos")
    parser.add_argument("--config", type=Path, default=BASE_DIR / "clipazos.json")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config.is_file() else {}
    report = json.loads(args.reporte.read_text(encoding="utf-8"))
    dry_run = args.reporte.stem.endswith("_dryrun")
    exported = 0
    for short in report.get("shorts", []):
        try:
            manifest = export_short(short, config, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            print(f"No se exporta {short.get('video')}: {exc}", file=sys.stderr)
            continue
        exported += 1
        print(f"Exportado: {Path(manifest['archivos']['manifest.json']).parent}")
    return 0 if exported else 1


if __name__ == "__main__":
    raise SystemExit(main())
