"""Montaje vertical genérico: visuales + voz + subtítulos.

No recorta una webcam ni lee un chat. Cada visual (imagen o vídeo) cubre el
lienzo 1080x1920 y la pista de voz sustituye al audio de los planos. Los
números de códec, CRF, fps y sonoridad salen de ``calidad_de``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import entorno
from video_editor import CaptionCue, _ffmpeg_exe, _filter_path, _run_ffmpeg

from base.configuracion import CalidadRender, calidad_de, estilo_de
from base.subtitulos import escribir_subtitulos, ruta_subtitulos

_IMAGENES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


@dataclass(frozen=True)
class Visual:
    """Un plano ya elegido (foto o vídeo) con licencia anotada fuera de aquí."""

    ruta: Path
    duracion: float | None = None
    inicio: float = 0.0
    tipo: str | None = None


@dataclass(frozen=True)
class Pieza:
    """Lo que el render necesita para un Short, sin saber de qué formato viene.

    ``palabras`` puede salir de Edge TTS (WordBoundary) o de Whisper.
    ``duracion`` es la del Short en segundos; los visuales sin duración propia
    se reparten el tiempo que sobre.
    """

    visuales: tuple[Visual, ...]
    voz: Path
    palabras: tuple[CaptionCue, ...] = ()
    duracion: float = 0.0
    titulo: str = ""


@dataclass(frozen=True)
class PlanRender:
    """Archivos y comando listos, sin haber llamado a FFmpeg."""

    ass: Path
    filtro: Path
    comando: list[str]
    filtro_texto: str


def tipo_de(visual: Visual) -> str:
    """``imagen`` o ``video``. Si ``tipo`` viene vacío, manda la extensión."""
    if visual.tipo in ("imagen", "video"):
        return visual.tipo
    if Path(visual.ruta).suffix.lower() in _IMAGENES:
        return "imagen"
    return "video"


def repartir(visuales: tuple[Visual, ...], duracion: float) -> list[tuple[Visual, float]]:
    """Asigna segundos a cada plano. Falla si no cuadran con la pieza."""
    if duracion <= 0:
        raise ValueError("la duración de la pieza tiene que ser mayor que cero")
    if not visuales:
        raise ValueError("la pieza necesita al menos un visual")
    fijadas = []
    for visual in visuales:
        if visual.duracion is None:
            continue
        if visual.duracion <= 0:
            raise ValueError("la duración de un visual tiene que ser mayor que cero")
        fijadas.append(visual.duracion)
    suma = sum(fijadas)
    libres = sum(1 for visual in visuales if visual.duracion is None)
    if suma - duracion > 1e-3:
        raise ValueError("las duraciones de los visuales superan la de la pieza")
    if libres == 0 and abs(suma - duracion) > 0.05:
        raise ValueError("la suma de las duraciones de los visuales no coincide con la pieza")
    cada = (duracion - suma) / libres if libres else 0.0
    if libres and cada <= 0:
        raise ValueError("no queda tiempo para los visuales sin duración")
    return [
        (visual, visual.duracion if visual.duracion is not None else cada)
        for visual in visuales
    ]


def _numero(value: float) -> str:
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def _filtro_visual(indice: int, visual: Visual, segundos: float, calidad: CalidadRender) -> str:
    ancho, alto = calidad.ancho, calidad.alto
    encuadre = (
        f"scale={ancho}:{alto}:force_original_aspect_ratio=increase,"
        f"crop={ancho}:{alto},setsar=1,fps={_numero(calidad.fps)},format={calidad.pix_fmt}"
    )
    if tipo_de(visual) == "imagen":
        return f"[{indice}:v]{encuadre}[v{indice}]"
    fin = visual.inicio + segundos
    return (
        f"[{indice}:v]trim=start={visual.inicio:.3f}:end={fin:.3f},setpts=PTS-STARTPTS,"
        f"{encuadre}[v{indice}]"
    )


def _entradas(visuales: list[tuple[Visual, float]], voz: Path, calidad: CalidadRender) -> list[str]:
    argumentos: list[str] = []
    for visual, segundos in visuales:
        ruta = str(Path(visual.ruta))
        if tipo_de(visual) == "imagen":
            argumentos += [
                "-loop", "1", "-framerate", _numero(calidad.fps), "-t", f"{segundos:.3f}", "-i", ruta,
            ]
        else:
            argumentos += ["-stream_loop", "-1", "-i", ruta]
    argumentos += ["-i", str(Path(voz))]
    return argumentos


def _texto_filtro(
    visuales: list[tuple[Visual, float]],
    ass: Path,
    duracion: float,
    calidad: CalidadRender,
    directorio_fuentes: str,
) -> str:
    partes = [_filtro_visual(indice, visual, segundos, calidad) for indice, (visual, segundos) in enumerate(visuales)]
    etiquetas = "".join(f"[v{indice}]" for indice in range(len(visuales)))
    partes.append(f"{etiquetas}concat=n={len(visuales)}:v=1:a=0[vcat]")
    voz = len(visuales)
    partes.append(
        f"[{voz}:a]atrim=start=0:end={duracion:.3f},asetpts=PTS-STARTPTS,"
        f"loudnorm=I={_numero(calidad.loudness_lufs)}:TP={_numero(calidad.loudness_tp)}:"
        f"LRA={_numero(calidad.loudness_lra)},"
        f"aresample={calidad.frecuencia_audio},apad=whole_dur={duracion:.3f}[aout]"
    )
    fuentes = Path(directorio_fuentes)
    ass_filtro = f"ass=filename='{_filter_path(ass)}'"
    if fuentes.is_dir():
        ass_filtro += f":fontsdir='{_filter_path(fuentes)}'"
    partes.append(f"[vcat]{ass_filtro}[vout]")
    return ";\n".join(partes) + "\n"


def _comando(
    entradas: list[str], filtro: Path, destino: Path, calidad: CalidadRender
) -> list[str]:
    return [
        _binario(), "-y", "-v", "error",
        *entradas,
        *entorno.opcion_filtro_complejo(filtro),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", calidad.codec_video,
        "-preset", calidad.preset,
        "-crf", str(calidad.crf),
        "-pix_fmt", calidad.pix_fmt,
        "-r", _numero(calidad.fps),
        "-c:a", calidad.codec_audio,
        "-b:a", calidad.bitrate_audio,
        "-movflags", calidad.movflags,
        str(destino),
    ]


def _binario() -> str:
    return _ffmpeg_exe()


def _ejecutar(comando: list[str]) -> None:
    _run_ffmpeg(comando)


def plan_render(pieza: Pieza, destino: Path, config: dict | None = None) -> PlanRender:
    """Escribe el .ass y el grafo, y devuelve el comando. No ejecuta FFmpeg."""
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    calidad = calidad_de(config)
    tramos = repartir(tuple(pieza.visuales), float(pieza.duracion))
    ass = ruta_subtitulos(destino)
    escribir_subtitulos(
        list(pieza.palabras), ass, config, titulo=pieza.titulo, duracion=float(pieza.duracion),
    )
    estilo = estilo_de(config)
    filtro = destino.with_name(destino.stem + "_filter.txt")
    texto = _texto_filtro(tramos, ass, float(pieza.duracion), calidad, estilo.directorio_fuentes)
    filtro.write_text(texto, encoding="utf-8")
    comando = _comando(_entradas(tramos, pieza.voz, calidad), filtro, destino, calidad)
    return PlanRender(ass=ass, filtro=filtro, comando=comando, filtro_texto=texto)


def render_pieza(pieza: Pieza, destino: Path, config: dict | None = None) -> Path:
    """Monta el MP4 vertical. El ejecutor se puede sustituir en las pruebas."""
    plan = plan_render(pieza, destino, config)
    _ejecutar(plan.comando)
    return Path(destino)
