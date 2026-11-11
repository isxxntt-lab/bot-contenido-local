"""Configuración común: lectura de clipazos.json y parámetros de calidad.

Todo número del render y de los subtítulos sale de aquí. El resto del código
pide un ``CalidadRender`` o un ``EstiloSubtitulo`` y no repite constantes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from video_editor import BASE_DIR

CONFIG_PATH = BASE_DIR / "clipazos.json"
OUTPUT_DIR = BASE_DIR / "output"
CLIPAZOS_DIR = OUTPUT_DIR / "clipazos"

_HEX_BGR = re.compile(r"^[0-9A-Fa-f]{6}$")

# Valores de partida del vertical. clipazos.json puede sustituir cualquiera.
DEFAULTS_RENDER: dict = {
    "ancho": 1080,
    "alto": 1920,
    "fps": 30,
    "codec_video": "libx264",
    "preset": "medium",
    "crf": 18,
    "pix_fmt": "yuv420p",
    "codec_audio": "aac",
    "bitrate_audio": "192k",
    "frecuencia_audio": 48000,
    "loudness_lufs": -14.0,
    "loudness_tp": -1.5,
    "loudness_lra": 11.0,
    "movflags": "+faststart",
}

# Posición segura: la barra de Shorts/TikTok ocupa abajo ~480 px y arriba ~250 px.
DEFAULTS_SUBTITULOS: dict = {
    "fuente": "Arial Black",
    "tamano": 78,
    "tamano_titulo": 72,
    "color_bgr": "FFFFFF",
    "resalte_bgr": "00FFFF",
    "contorno": 5,
    "sombra": 3,
    "contorno_titulo": 4,
    "sombra_titulo": 2,
    "alineacion": 2,
    "alineacion_titulo": 8,
    "margen_lateral": 90,
    "margen_inferior": 480,
    "margen_superior": 250,
    "espaciado": 0,
    "negrita": -1,
    "palabras_por_linea": 3,
    "escala_palabra_activa": 130,
    "fundido_entrada_ms": 20,
    "fundido_salida_ms": 40,
    "duracion_minima_palabra": 0.08,
    "titulo_segundos": 2.5,
    "directorio_fuentes": r"C:\Windows\Fonts",
}


@dataclass(frozen=True)
class CalidadRender:
    """Códec, tasa y sonoridad del MP4 vertical. Una sola fuente para el montaje."""

    ancho: int
    alto: int
    fps: float
    codec_video: str
    preset: str
    crf: int
    pix_fmt: str
    codec_audio: str
    bitrate_audio: str
    frecuencia_audio: int
    loudness_lufs: float
    loudness_tp: float
    loudness_lra: float
    movflags: str


@dataclass(frozen=True)
class EstiloSubtitulo:
    """Tipografía y zona segura de los subtítulos palabra a palabra.

    ``color_bgr`` y ``resalte_bgr`` son seis hexadecimales en el orden BGR de
    ASS (blanco ``FFFFFF``, amarillo ``00FFFF``), sin el prefijo ``&H``.
    ``alineacion`` 2 ancla el subtítulo al borde inferior; ``margen_inferior``
    es la distancia en píxeles que se deja libre para la UI de Shorts/TikTok.
    ``margen_superior`` es la Y mínima del bloque de texto.
    """

    fuente: str
    tamano: int
    tamano_titulo: int
    color_bgr: str
    resalte_bgr: str
    contorno: int
    sombra: int
    contorno_titulo: int
    sombra_titulo: int
    alineacion: int
    alineacion_titulo: int
    margen_lateral: int
    margen_inferior: int
    margen_superior: int
    espaciado: int
    negrita: int
    palabras_por_linea: int
    escala_palabra_activa: int
    fundido_entrada_ms: int
    fundido_salida_ms: int
    duracion_minima_palabra: float
    titulo_segundos: float
    directorio_fuentes: str


def load_config(path: Path | None = None) -> dict:
    """Lee clipazos.json (UTF-8). Sin ruta, el de la raíz del proyecto."""
    origin = Path(path) if path is not None else CONFIG_PATH
    return json.loads(origin.read_text(encoding="utf-8"))


def _bloque(config: dict | None, nombre: str) -> dict:
    raw = (config or {}).get(nombre)
    return raw if isinstance(raw, dict) else {}


def _entero(raw: dict, key: str, default: int, *, minimo: int | None = None, maximo: int | None = None) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        value = default
    number = int(value)
    if minimo is not None and number < minimo:
        return int(default)
    if maximo is not None and number > maximo:
        return int(default)
    return number


def _real(raw: dict, key: str, default: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float(default)
    return float(value)


def _texto(raw: dict, key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or not value.strip():
        return default
    return value.strip()


def calidad_de(config: dict | None = None) -> CalidadRender:
    """Parámetros de render del bloque ``render``, con los huecos rellenos."""
    raw = _bloque(config, "render")
    ancho = _entero(raw, "ancho", DEFAULTS_RENDER["ancho"], minimo=16)
    alto = _entero(raw, "alto", DEFAULTS_RENDER["alto"], minimo=16)
    fps = _real(raw, "fps", DEFAULTS_RENDER["fps"])
    if fps <= 0:
        fps = float(DEFAULTS_RENDER["fps"])
    crf = _entero(raw, "crf", DEFAULTS_RENDER["crf"], minimo=0, maximo=51)
    return CalidadRender(
        ancho=ancho,
        alto=alto,
        fps=fps,
        codec_video=_texto(raw, "codec_video", DEFAULTS_RENDER["codec_video"]),
        preset=_texto(raw, "preset", DEFAULTS_RENDER["preset"]),
        crf=crf,
        pix_fmt=_texto(raw, "pix_fmt", DEFAULTS_RENDER["pix_fmt"]),
        codec_audio=_texto(raw, "codec_audio", DEFAULTS_RENDER["codec_audio"]),
        bitrate_audio=_texto(raw, "bitrate_audio", DEFAULTS_RENDER["bitrate_audio"]),
        frecuencia_audio=_entero(raw, "frecuencia_audio", DEFAULTS_RENDER["frecuencia_audio"], minimo=8000),
        loudness_lufs=_real(raw, "loudness_lufs", DEFAULTS_RENDER["loudness_lufs"]),
        loudness_tp=_real(raw, "loudness_tp", DEFAULTS_RENDER["loudness_tp"]),
        loudness_lra=_real(raw, "loudness_lra", DEFAULTS_RENDER["loudness_lra"]),
        movflags=_texto(raw, "movflags", DEFAULTS_RENDER["movflags"]),
    )


def _color(raw: dict, key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or _HEX_BGR.fullmatch(value.strip()) is None:
        return default.upper()
    return value.strip().upper()


def estilo_de(config: dict | None = None) -> EstiloSubtitulo:
    """Estilo de subtítulos del bloque ``subtitulos``, con los huecos rellenos."""
    raw = _bloque(config, "subtitulos")
    escala = _entero(raw, "escala_palabra_activa", DEFAULTS_SUBTITULOS["escala_palabra_activa"], minimo=100, maximo=200)
    palabras = _entero(raw, "palabras_por_linea", DEFAULTS_SUBTITULOS["palabras_por_linea"], minimo=1, maximo=8)
    return EstiloSubtitulo(
        fuente=_texto(raw, "fuente", DEFAULTS_SUBTITULOS["fuente"]),
        tamano=_entero(raw, "tamano", DEFAULTS_SUBTITULOS["tamano"], minimo=8),
        tamano_titulo=_entero(raw, "tamano_titulo", DEFAULTS_SUBTITULOS["tamano_titulo"], minimo=8),
        color_bgr=_color(raw, "color_bgr", DEFAULTS_SUBTITULOS["color_bgr"]),
        resalte_bgr=_color(raw, "resalte_bgr", DEFAULTS_SUBTITULOS["resalte_bgr"]),
        contorno=_entero(raw, "contorno", DEFAULTS_SUBTITULOS["contorno"], minimo=0),
        sombra=_entero(raw, "sombra", DEFAULTS_SUBTITULOS["sombra"], minimo=0),
        contorno_titulo=_entero(raw, "contorno_titulo", DEFAULTS_SUBTITULOS["contorno_titulo"], minimo=0),
        sombra_titulo=_entero(raw, "sombra_titulo", DEFAULTS_SUBTITULOS["sombra_titulo"], minimo=0),
        alineacion=_entero(raw, "alineacion", DEFAULTS_SUBTITULOS["alineacion"], minimo=1, maximo=9),
        alineacion_titulo=_entero(raw, "alineacion_titulo", DEFAULTS_SUBTITULOS["alineacion_titulo"], minimo=1, maximo=9),
        margen_lateral=_entero(raw, "margen_lateral", DEFAULTS_SUBTITULOS["margen_lateral"], minimo=0),
        margen_inferior=_entero(raw, "margen_inferior", DEFAULTS_SUBTITULOS["margen_inferior"], minimo=0),
        margen_superior=_entero(raw, "margen_superior", DEFAULTS_SUBTITULOS["margen_superior"], minimo=0),
        espaciado=_entero(raw, "espaciado", DEFAULTS_SUBTITULOS["espaciado"], minimo=0),
        negrita=_entero(raw, "negrita", DEFAULTS_SUBTITULOS["negrita"]),
        palabras_por_linea=palabras,
        escala_palabra_activa=escala,
        fundido_entrada_ms=_entero(raw, "fundido_entrada_ms", DEFAULTS_SUBTITULOS["fundido_entrada_ms"], minimo=0),
        fundido_salida_ms=_entero(raw, "fundido_salida_ms", DEFAULTS_SUBTITULOS["fundido_salida_ms"], minimo=0),
        duracion_minima_palabra=max(0.02, _real(raw, "duracion_minima_palabra", DEFAULTS_SUBTITULOS["duracion_minima_palabra"])),
        titulo_segundos=max(0.0, _real(raw, "titulo_segundos", DEFAULTS_SUBTITULOS["titulo_segundos"])),
        directorio_fuentes=_texto(raw, "directorio_fuentes", DEFAULTS_SUBTITULOS["directorio_fuentes"]),
    )
