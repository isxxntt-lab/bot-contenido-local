"""Control de calidad bloqueante de los Shorts de Clipazos ES.

Se ejecuta después de montar el vídeo y otra vez justo antes de subirlo. Si algo
falla, o si una herramienta (FFmpeg, ffprobe, YuNet) no responde, el Short queda
bloqueado con un motivo claro. Config en el bloque "qa" de clipazos.json.

Comprueba:
- resolución (1080x1920), fps y duración;
- que haya audio y su sonoridad integrada (LUFS, filtro ebur128);
- tramos negros (blackdetect) y congelados (freezedetect);
- subtítulos: el .ass del Short existe y tiene suficientes líneas de subtítulo;
- geometría de CaptionSplit: el renglón no entra en la banda de la webcam ni se sale del lienzo;
- caras con YuNet (opcional, desactivado por defecto).

ffprobe se usa si está en el PATH; si no, se lee la salida de ``ffmpeg -i``
(imageio-ffmpeg solo trae ffmpeg).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from video_editor import BASE_DIR, FRAME_SIZE, SPLIT_TOP, _ffmpeg_exe

FACE_MODEL = BASE_DIR / "assets" / "models" / "face_detection_yunet_2023mar.onnx"

DEFAULTS: dict = {
    "activa": True,
    "ancho": 1080,
    "alto": 1920,
    "fps": 24.0,
    "fps_tolerancia": 0.5,
    "duracion_min": 10.0,
    "duracion_max": 180.0,
    "lufs_min": -18.0,
    "lufs_max": -10.0,
    "negro_max_segundos": 1.0,
    "negro_umbral_pixel": 0.10,
    "congelado_max_segundos": 2.0,
    "congelado_ruido_db": -60.0,
    "subtitulos_obligatorios": True,
    "subtitulos_min_eventos": 3,
    "timeout_segundos": 600,
    "caras": {
        "activa": False,
        "muestras": 8,
        "min_fotogramas_con_cara": 1,
        "puntuacion_min": 0.7,
    },
}

_NUMBERS = (
    "ancho", "alto", "fps", "fps_tolerancia", "duracion_min", "duracion_max", "lufs_min", "lufs_max",
    "negro_max_segundos", "negro_umbral_pixel", "congelado_max_segundos", "congelado_ruido_db",
    "subtitulos_min_eventos", "timeout_segundos",
)


def settings_from(config: dict | None) -> dict:
    raw = (config or {}).get("qa")
    raw = raw if isinstance(raw, dict) else {}
    settings = {key: value for key, value in DEFAULTS.items() if key != "caras"}
    for key in _NUMBERS:
        value = raw.get(key, DEFAULTS[key])
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            value = DEFAULTS[key]
        settings[key] = value
    for key in ("activa", "subtitulos_obligatorios"):
        value = raw.get(key, DEFAULTS[key])
        settings[key] = value if isinstance(value, bool) else DEFAULTS[key]
    faces_raw = raw.get("caras") if isinstance(raw.get("caras"), dict) else {}
    faces = dict(DEFAULTS["caras"])
    for key, default in DEFAULTS["caras"].items():
        value = faces_raw.get(key, default)
        if isinstance(default, bool):
            faces[key] = value if isinstance(value, bool) else default
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            faces[key] = default
        else:
            faces[key] = value
    settings["caras"] = faces
    return settings


@dataclass
class QAResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    skipped: bool = False

    def to_dict(self) -> dict:
        return {"ok": self.passed, "motivos": self.reasons, "metricas": self.metrics, "omitido": self.skipped}

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


class QAToolError(RuntimeError):
    """FFmpeg, ffprobe o YuNet no pudieron analizar el vídeo."""


def ffmpeg_path() -> str:
    return _ffmpeg_exe()


def ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def _run(command: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise QAToolError(f"{Path(command[0]).name} no se pudo ejecutar ({exc})") from exc


def _fps(text: str | None) -> float | None:
    if not text:
        return None
    if "/" in text:
        top, bottom = text.split("/", 1)
        try:
            return float(top) / float(bottom) if float(bottom) else None
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def probe(path: Path, timeout: float = 120) -> dict:
    """Resolución, fps, duración y si hay audio."""
    tool = ffprobe_path()
    if tool:
        result = _run(
            [tool, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], timeout
        )
        if result.returncode != 0:
            raise QAToolError(f"ffprobe falló: {result.stderr.strip()[-300:]}")
        data = json.loads(result.stdout or "{}")
        video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
        audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
        duration = data.get("format", {}).get("duration")
        return {
            "duracion": float(duration) if duration else None,
            "ancho": int(video["width"]) if video else None,
            "alto": int(video["height"]) if video else None,
            "fps": _fps(video.get("avg_frame_rate") or video.get("r_frame_rate")) if video else None,
            "audio": audio is not None,
            "herramienta": "ffprobe",
        }
    result = _run([ffmpeg_path(), "-hide_banner", "-i", str(path)], timeout)
    text = result.stderr or ""
    if "Invalid data" in text or "No such file" in text or "Error opening input" in text:
        raise QAToolError(f"ffmpeg no puede abrir el vídeo: {text.strip().splitlines()[-1] if text.strip() else '?'}")
    duration = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    video_line = next((line for line in text.splitlines() if re.search(r"Stream #.*Video:", line)), "")
    size = re.search(r",\s*(\d{2,5})x(\d{2,5})", video_line)
    fps = re.search(r"([\d.]+)\s*fps", video_line) or re.search(r"([\d.]+)\s*tbr", video_line)
    if duration is None or not video_line:
        raise QAToolError("ffmpeg no devolvió la duración o la pista de vídeo")
    hours, minutes, seconds = duration.groups()
    return {
        "duracion": int(hours) * 3600 + int(minutes) * 60 + float(seconds),
        "ancho": int(size[1]) if size else None,
        "alto": int(size[2]) if size else None,
        "fps": float(fps[1]) if fps else None,
        "audio": re.search(r"Stream #.*Audio:", text) is not None,
        "herramienta": "ffmpeg",
    }


def _segments(text: str, prefix: str, total: float | None) -> list[tuple[float, float]]:
    """Tramos (inicio, duración) de blackdetect o freezedetect."""
    found: list[tuple[float, float]] = []
    if prefix == "black":
        for match in re.finditer(r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)\s+black_duration:\s*([\d.]+)", text):
            found.append((float(match[1]), float(match[3])))
        return found
    starts = [float(value) for value in re.findall(r"freeze_start:\s*([\d.]+)", text)]
    durations = [float(value) for value in re.findall(r"freeze_duration:\s*([\d.]+)", text)]
    for index, start in enumerate(starts):
        if index < len(durations):
            found.append((start, durations[index]))
        elif total is not None:
            found.append((start, max(0.0, total - start)))
    return found


def analyze(path: Path, settings: dict, has_audio: bool, duration: float | None) -> dict:
    """Una pasada de FFmpeg: blackdetect + freezedetect + ebur128."""
    black = (
        f"blackdetect=d={min(0.5, settings['negro_max_segundos']):.2f}:pix_th={settings['negro_umbral_pixel']:.3f}"
    )
    freeze = f"freezedetect=n={settings['congelado_ruido_db']:.0f}dB:d={min(1.0, settings['congelado_max_segundos']):.2f}"
    graph = f"[0:v]{black},{freeze}[v]"
    command = [ffmpeg_path(), "-hide_banner", "-nostats", "-i", str(path)]
    maps = ["-map", "[v]"]
    if has_audio:
        graph += ";[0:a]ebur128=peak=true[a]"
        maps += ["-map", "[a]"]
    command += ["-filter_complex", graph, *maps, "-f", "null", "-"]
    result = _run(command, float(settings["timeout_segundos"]))
    text = result.stderr or ""
    if result.returncode != 0:
        raise QAToolError(f"ffmpeg falló al analizar: {text.strip()[-300:]}")
    metrics: dict = {
        "negros": _segments(text, "black", duration),
        "congelados": _segments(text, "freeze", duration),
    }
    if has_audio:
        summary = text[text.rfind("Summary:"):] if "Summary:" in text else ""
        loudness = re.search(r"I:\s*(-?[\d.]+|-inf)\s*LUFS", summary)
        if not loudness:
            raise QAToolError("ebur128 no devolvió la sonoridad integrada")
        value = loudness[1]
        metrics["lufs"] = -120.0 if value == "-inf" else float(value)
        peak = re.search(r"Peak:\s*(-?[\d.]+|-inf)\s*dBFS", summary)
        if peak:
            metrics["pico_dbfs"] = -120.0 if peak[1] == "-inf" else float(peak[1])
    return metrics


def check_caption_geometry(ass_path: Path | None) -> tuple[list[str], dict]:
    """Comprueba que CaptionSplit quede debajo de la webcam y dentro del fotograma.

    Solo actúa si el .ass declara el estilo CaptionSplit (los de prueba que solo
    nombran el estilo en un Dialogue no se tocan). Alignment 8: MarginV es el
    borde superior del renglón. La tinta medida cae por debajo de ese borde, así
    que con MarginV >= SPLIT_TOP el texto no entra en la cámara. El golpe al
    130 % crece hacia abajo; el borde inferior suma líneas, contorno y sombra.
    """
    metrics: dict = {"split_eventos": 0}
    if ass_path is None or not Path(ass_path).is_file():
        return [], metrics
    try:
        text = Path(ass_path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        return [f"no se pudo leer la geometría de los subtítulos ({exc})"], metrics
    styles = _ass_styles(text)
    style = styles.get("CaptionSplit")
    if style is None:
        return [], metrics
    play_x, play_y = _play_res(text)
    facecam = int(round(SPLIT_TOP * play_y / FRAME_SIZE[1]))
    reasons: list[str] = []
    if style["alignment"] != 8:
        reasons.append(
            f"CaptionSplit no usa alineación superior (tiene {style['alignment']}); "
            "no se puede asegurar que el texto quede fuera de la webcam"
        )
    bottoms: list[float] = []
    tops: list[float] = []
    for event in _caption_split_events(text):
        metrics["split_eventos"] += 1
        if style["alignment"] != 8:
            continue
        if re.search(r"\\(?:pos|move|org|frz|frx|fry|fax|fay|clip)\s*\(", event["text"]):
            reasons.append("subtítulo split con posición libre; no se puede comprobar la webcam")
            continue
        margin_v = event["margin_v"] if event["margin_v"] else style["margin_v"]
        fontsize = event["fontsize"] or style["fontsize"]
        scale = max(event["scale"], style["scale_y"] / 100.0, 1.0)
        top = float(margin_v)
        bottom = top + (fontsize * event["lineas"] + style["outline"] + style["shadow"]) * scale
        tops.append(top)
        bottoms.append(bottom)
        if top < facecam:
            reasons.append(
                f"subtítulo split dentro de la webcam (borde superior en {top:.0f}, la cámara acaba en {facecam})"
            )
        if bottom > play_y or top < 0:
            reasons.append(
                f"subtítulo split fuera del lienzo (y {top:.0f}-{bottom:.0f}, alto {play_y})"
            )
    if tops:
        metrics["split_y0"] = min(tops)
        metrics["split_y1"] = max(bottoms)
        metrics["split_facecam"] = facecam
    return list(dict.fromkeys(reasons)), metrics


def _play_res(text: str) -> tuple[int, int]:
    width = re.search(r"(?m)^PlayResX:\s*(\d+)", text)
    height = re.search(r"(?m)^PlayResY:\s*(\d+)", text)
    return (
        int(width.group(1)) if width else FRAME_SIZE[0],
        int(height.group(1)) if height else FRAME_SIZE[1],
    )


def _ass_styles(text: str) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for line in text.splitlines():
        if not line.startswith("Style:"):
            continue
        parts = [part.strip() for part in line.split(":", 1)[1].split(",")]
        if len(parts) < 23:
            continue
        try:
            found[parts[0]] = {
                "fontsize": float(parts[2]),
                "scale_y": float(parts[12]),
                "outline": float(parts[16]),
                "shadow": float(parts[17]),
                "alignment": int(float(parts[18])),
                "margin_v": int(float(parts[21])),
            }
        except ValueError:
            continue
    return found


def _caption_split_events(text: str) -> list[dict]:
    events: list[dict] = []
    for line in text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        fields = line.split(":", 1)[1].split(",", 9)
        if len(fields) < 10 or fields[3].strip() != "CaptionSplit":
            continue
        body = fields[9]
        try:
            margin_v = int(float(fields[7].strip() or 0))
        except ValueError:
            margin_v = 0
        sizes = [int(value) for value in re.findall(r"\\fs(\d+)", body)]
        scales = [int(value) for value in re.findall(r"\\fsc[xy](\d+)", body)]
        plain = re.sub(r"\{[^}]*\}", "", body)
        lines = re.split(r"\\N", plain)
        events.append({
            "text": body,
            "margin_v": margin_v,
            "fontsize": max(sizes) if sizes else 0,
            "scale": (max(scales) / 100.0) if scales else 1.0,
            "lineas": max(1, len(lines)),
        })
    return events


def check_subtitles(ass_path: Path | None, settings: dict) -> tuple[list[str], dict]:
    if ass_path is None or not Path(ass_path).is_file():
        return ([f"faltan los subtítulos ({Path(ass_path).name if ass_path else 'sin .ass'})"], {"subtitulos": 0})
    try:
        text = Path(ass_path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        return [f"no se pudieron leer los subtítulos ({exc})"], {"subtitulos": 0}
    events = [
        line for line in text.splitlines()
        if line.startswith("Dialogue:") and len(line.split(",")) > 3 and line.split(",")[3].strip().startswith("Caption")
    ]
    minimum = int(settings["subtitulos_min_eventos"])
    reasons = [] if len(events) >= minimum else [f"pocos subtítulos ({len(events)} < {minimum})"]
    return reasons, {"subtitulos": len(events)}


def check_faces(path: Path, settings: dict, duration: float | None) -> tuple[list[str], dict]:
    faces = settings["caras"]
    model = FACE_MODEL
    if not model.is_file():
        raise QAToolError(f"falta el modelo YuNet ({model.name})")
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise QAToolError(f"OpenCV no disponible ({exc})") from exc
    width, height = 540, 960
    samples = max(1, int(faces["muestras"]))
    span = max(duration or 1.0, 0.1)
    rate = samples / span
    try:
        raw = subprocess.run(
            [ffmpeg_path(), "-v", "error", "-i", str(path), "-vf", f"fps={rate:.4f},scale={width}:{height}",
             "-frames:v", str(samples), "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
            capture_output=True, timeout=float(settings["timeout_segundos"]),
        ).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QAToolError(f"ffmpeg no pudo sacar fotogramas ({exc})") from exc
    frames = np.frombuffer(raw, np.uint8).reshape(-1, height, width, 3) if raw else []
    if not len(frames):
        raise QAToolError("ffmpeg no devolvió fotogramas para buscar caras")
    try:
        detector = cv2.FaceDetectorYN.create(str(model), "", (width, height), float(faces["puntuacion_min"]))
    except Exception as exc:  # noqa: BLE001
        raise QAToolError(f"YuNet no arrancó ({exc})") from exc
    with_face = 0
    for frame in frames:
        _ok, found = detector.detect(np.ascontiguousarray(frame))
        if found is not None and len(found):
            with_face += 1
    minimum = int(faces["min_fotogramas_con_cara"])
    reasons = [] if with_face >= minimum else [f"sin cara visible ({with_face}/{len(frames)} fotogramas, mínimo {minimum})"]
    return reasons, {"fotogramas_con_cara": with_face, "fotogramas_revisados": int(len(frames))}


def run_qa(video_path: Path | str, config: dict | None, ass_path: Path | str | None = None) -> QAResult:
    """QA completo. Devuelve passed=False con motivos ante cualquier fallo."""
    settings = settings_from(config)
    path = Path(video_path)
    if not settings["activa"]:
        return QAResult(True, [], {"aviso": "qa.activa = false: QA desactivado"}, skipped=True)
    if ass_path is None:
        ass_path = path.with_suffix(".ass")
    if not path.is_file() or path.stat().st_size == 0:
        return QAResult(False, [f"no existe el vídeo {path.name}"], {})
    reasons: list[str] = []
    metrics: dict = {}
    try:
        info = probe(path, float(settings["timeout_segundos"]))
    except QAToolError as exc:
        return QAResult(False, [f"QA bloqueado: {exc}"], {})
    metrics.update(info)
    if (info["ancho"], info["alto"]) != (int(settings["ancho"]), int(settings["alto"])):
        reasons.append(f"resolución {info['ancho']}x{info['alto']} (se pide {int(settings['ancho'])}x{int(settings['alto'])})")
    if info["fps"] is None or abs(info["fps"] - float(settings["fps"])) > float(settings["fps_tolerancia"]):
        reasons.append(f"fps {info['fps']} (se pide {settings['fps']} ± {settings['fps_tolerancia']})")
    duration = info["duracion"]
    if duration is None or not (float(settings["duracion_min"]) <= duration <= float(settings["duracion_max"])):
        reasons.append(f"duración {duration} s fuera de {settings['duracion_min']}-{settings['duracion_max']} s")
    if not info["audio"]:
        reasons.append("el vídeo no tiene audio")
    try:
        analysis = analyze(path, settings, info["audio"], duration)
    except QAToolError as exc:
        reasons.append(f"QA bloqueado: {exc}")
        analysis = {}
    metrics.update(analysis)
    if "lufs" in analysis:
        lufs = analysis["lufs"]
        if not (float(settings["lufs_min"]) <= lufs <= float(settings["lufs_max"])):
            reasons.append(f"sonoridad {lufs:.1f} LUFS fuera de {settings['lufs_min']} a {settings['lufs_max']}")
    limit_black = float(settings["negro_max_segundos"])
    for start, length in analysis.get("negros", []):
        if length >= limit_black:
            reasons.append(f"pantalla negra de {length:.2f} s en {start:.2f} s")
    limit_freeze = float(settings["congelado_max_segundos"])
    for start, length in analysis.get("congelados", []):
        if length >= limit_freeze:
            reasons.append(f"imagen congelada {length:.2f} s desde {start:.2f} s")
    ass_file = Path(ass_path) if ass_path else None
    if settings["subtitulos_obligatorios"]:
        sub_reasons, sub_metrics = check_subtitles(ass_file, settings)
        reasons.extend(sub_reasons)
        metrics.update(sub_metrics)
    if ass_file is not None and ass_file.is_file():
        geo_reasons, geo_metrics = check_caption_geometry(ass_file)
        reasons.extend(geo_reasons)
        metrics.update(geo_metrics)
    if settings["caras"]["activa"]:
        try:
            face_reasons, face_metrics = check_faces(path, settings, duration)
            reasons.extend(face_reasons)
            metrics.update(face_metrics)
        except QAToolError as exc:
            reasons.append(f"QA bloqueado: {exc}")
    return QAResult(not reasons, reasons, metrics)
