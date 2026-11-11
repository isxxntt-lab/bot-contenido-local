"""Comprueba, antes de descargar o montar, que este PC puede hacer un Short.

FFmpeg 7.0 acepta leer una opción desde un archivo con ``-/opción RUTA``.
FFmpeg 9 ya no reconoce ``-filter_complex_script`` (sale con código 8:
``Unrecognized option``). El montaje y el smoke usan siempre ``-/filter_complex``.

Si algo falta, ``comprobar`` devuelve un informe no apto. ``clipazos.main`` lo
escribe en el log con el código ``entorno_no_apto`` y termina sin montar ni subir.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


CODIGO_NO_APTO = "entorno_no_apto"
EXIT_ENTORNO_NO_APTO = 2
FFMPEG_MIN_MAJOR = 7
FILTROS = ("ass", "loudnorm", "blackdetect", "freezedetect", "ebur128")
FUENTE = "Arial Black"
# Nombre de archivo de Arial Black en C:\Windows\Fonts (el fontsdir del editor).
ARCHIVOS_FUENTE = ("ariblk.ttf", "arial black.ttf", "arialblack.ttf")
CARPETA_FUENTES_WINDOWS = Path(r"C:\Windows\Fonts")

LAVFI_VIDEO = "testsrc=d=1:s=640x360:r=30"
LAVFI_AUDIO = "sine=d=1"
GRAFO_SMOKE = "[0:v]scale=320:240,setsar=1[vout];[1:a]aresample=44100[aout]"

_VERSION_RAW = re.compile(r"ffmpeg version\s+(\S+)", re.IGNORECASE)
_VERSION_NUM = re.compile(r"^n?(\d+)\.(\d+)(?:\.(\d+))?")
_FLAGS = re.compile(r"^[.TSC|]{2,4}$")
_NOMBRE_FILTRO = re.compile(r"^[A-Za-z0-9_]+$")

FFmpeg = str | list[str]


@dataclass(frozen=True)
class InformeEntorno:
    """Resultado del chequeo. ``apto`` False siempre lleva motivos y el código."""

    apto: bool
    motivos: tuple[str, ...] = ()
    version: str | None = None

    @property
    def codigo(self) -> str | None:
        return None if self.apto else CODIGO_NO_APTO

    def linea_log(self) -> str:
        if self.apto:
            return f"entorno apto: FFmpeg {self.version or '?'}"
        return f"{CODIGO_NO_APTO}: " + "; ".join(self.motivos)


def opcion_filtro_complejo(script: Path | str) -> list[str]:
    """Argumentos para leer un filter_complex desde un archivo.

    La ruta va en su propio argumento, sin comillas añadidas: así un camino con
    espacios o acentos (``Proyecto Bot contenido\\tmpé x\\g.txt``) llega entero
    a FFmpeg. No usa el shell.
    """
    return ["-/filter_complex", str(script)]


def argumentos_grafo_minimo(ffmpeg: FFmpeg, script: Path | str, salida: list[str]) -> list[str]:
    """testsrc + sine de 1 s, grafo en ``script`` y la salida que se le pase."""
    return [
        *_prefijo(ffmpeg),
        "-f", "lavfi", "-i", LAVFI_VIDEO,
        "-f", "lavfi", "-i", LAVFI_AUDIO,
        *opcion_filtro_complejo(script),
        "-map", "[vout]", "-map", "[aout]",
        *salida,
    ]


def montar_filtro_minimo(
    destino: Path, ffmpeg: FFmpeg | None = None, script: Path | None = None
) -> Path:
    """Monta el grafo mínimo a un MP4. La usa la prueba real de los binarios."""
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    script = Path(script) if script is not None else destino.with_name(destino.stem + "_filter.txt")
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(GRAFO_SMOKE + "\n", encoding="utf-8")
    exe: FFmpeg = ffmpeg if ffmpeg is not None else ffmpeg_del_editor()
    command = argumentos_grafo_minimo(exe, script, [
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        str(destino),
    ])
    hueco = len(_prefijo(exe))
    command[hueco:hueco] = ["-y", "-v", "error"]
    result = _ejecutar(command, 60)
    if result is None or result.returncode != 0:
        texto = "" if result is None else (result.stderr or result.stdout or "")
        cola = "\n".join(texto.splitlines()[-20:])
        raise RuntimeError(f"ffmpeg falló al montar el filtro mínimo:\n{cola}")
    return destino


def ffmpeg_del_editor() -> str:
    from video_editor import _ffmpeg_exe

    return _ffmpeg_exe()


def version_ffmpeg(texto: str) -> tuple[int, int, int] | None:
    """(mayor, menor, parche) a partir de la primera línea de ``ffmpeg -version``."""
    raw = _VERSION_RAW.search(texto or "")
    if raw is None:
        return None
    numeros = _VERSION_NUM.match(raw.group(1))
    if numeros is None:
        return None
    return int(numeros.group(1)), int(numeros.group(2)), int(numeros.group(3) or 0)


def token_version(texto: str) -> str | None:
    raw = _VERSION_RAW.search(texto or "")
    return raw.group(1) if raw else None


def nombres_filtros(texto: str) -> set[str]:
    """Nombres de ``ffmpeg -filters``.

    FFmpeg 7 pone tres marcas (``...``, ``.S.``) y FFmpeg 9 dos (``..``, ``.S``).
    """
    nombres: set[str] = set()
    for linea in (texto or "").splitlines():
        partes = linea.split()
        if len(partes) >= 2 and _FLAGS.fullmatch(partes[0]) and _NOMBRE_FILTRO.fullmatch(partes[1]):
            nombres.add(partes[1])
    return nombres


def fuente_arial_black(carpeta: Path | None = None) -> str | None:
    """Ruta de Arial Black, o None si el editor no va a poder usarla.

    Con ``carpeta`` solo se mira esa carpeta (las pruebas). Sin ella, se mira
    ``C:\\Windows\\Fonts`` y, si no está, fontconfig por el nombre de familia.
    ``fc-match`` no sirve: devuelve otra fuente si Arial Black no está instalada.
    """
    explicita = carpeta is not None
    carpeta = CARPETA_FUENTES_WINDOWS if carpeta is None else Path(carpeta)
    hallada = _archivo_fuente(carpeta)
    if hallada is not None:
        return hallada
    if explicita:
        return None
    return _fuente_fontconfig()


def comprobar(ffmpeg: FFmpeg | None = None, carpeta_fuentes: Path | None = None) -> InformeEntorno:
    """Versión >= 7, filtros, Arial Black y un smoke de 1 s con ``-/filter_complex``.

    No descarga, no monta un Short y no sube. Un código distinto de 0 en el smoke
    deja el informe no apto.
    """
    motivos: list[str] = []
    try:
        exe: FFmpeg = ffmpeg if ffmpeg is not None else ffmpeg_del_editor()
    except (OSError, ImportError, RuntimeError) as exc:
        return InformeEntorno(False, (f"no se encuentra el FFmpeg de imageio-ffmpeg ({exc})",))

    version_txt: str | None = None
    try:
        version_run = _ejecutar([*_prefijo(exe), "-version"], 20)
    except subprocess.TimeoutExpired:
        return InformeEntorno(False, ("ffmpeg -version no terminó a tiempo",))
    if version_run is None:
        mostrado = exe if isinstance(exe, str) else " ".join(exe)
        return InformeEntorno(False, (f"no se encuentra el FFmpeg ({mostrado})",))
    banner = f"{version_run.stdout or ''}\n{version_run.stderr or ''}"
    version_txt = token_version(banner)
    numeros = version_ffmpeg(banner)
    if numeros is None:
        motivos.append("no se pudo leer la versión de FFmpeg")
    elif numeros[0] < FFMPEG_MIN_MAJOR:
        motivos.append(f"FFmpeg {version_txt} es anterior a {FFMPEG_MIN_MAJOR}")

    try:
        filtros_run = _ejecutar([*_prefijo(exe), "-hide_banner", "-filters"], 20)
    except subprocess.TimeoutExpired:
        motivos.append("ffmpeg -filters no terminó a tiempo")
        filtros_run = None
    if filtros_run is None and not any("filtros" in motivo for motivo in motivos):
        motivos.append("ffmpeg -filters no pudo arrancar")
    elif filtros_run is not None:
        if filtros_run.returncode != 0:
            motivos.append(
                f"ffmpeg -filters terminó con código {filtros_run.returncode}"
                + _detalle(filtros_run.stderr or filtros_run.stdout or "")
            )
        presentes = nombres_filtros(f"{filtros_run.stdout or ''}\n{filtros_run.stderr or ''}")
        faltan = [nombre for nombre in FILTROS if nombre not in presentes]
        if faltan:
            motivos.append("faltan los filtros " + ", ".join(faltan))

    if fuente_arial_black(carpeta_fuentes) is None:
        motivos.append(
            "falta la fuente Arial Black "
            "(los estilos ASS del editor la nombran; en Windows es C:\\Windows\\Fonts\\ariblk.ttf)"
        )

    try:
        codigo, detalle = _smoke(exe)
    except subprocess.TimeoutExpired:
        motivos.append("el smoke de -/filter_complex no terminó a tiempo")
    else:
        if codigo != 0:
            motivos.append(f"el smoke de -/filter_complex terminó con código {codigo}{detalle}")

    if motivos:
        return InformeEntorno(False, tuple(motivos), version_txt)
    return InformeEntorno(True, (), version_txt)


def _smoke(ffmpeg: FFmpeg) -> tuple[int, str]:
    with tempfile.TemporaryDirectory(prefix="clipazos-entorno-") as carpeta:
        script = Path(carpeta) / "g.txt"
        script.write_text(GRAFO_SMOKE + "\n", encoding="utf-8")
        argv = argumentos_grafo_minimo(ffmpeg, script, ["-f", "null", "-"])
        result = _ejecutar(argv, 60)
    if result is None:
        return 127, ""
    if result.returncode == 0:
        return 0, ""
    return result.returncode, _detalle(result.stderr or result.stdout or "")


def _archivo_fuente(carpeta: Path) -> str | None:
    try:
        archivos = [path for path in carpeta.iterdir() if path.is_file()]
    except OSError:
        return None
    por_nombre = {path.name.casefold(): path for path in archivos}
    for nombre in ARCHIVOS_FUENTE:
        path = por_nombre.get(nombre)
        if path is not None:
            return str(path)
    return None


def _fuente_fontconfig() -> str | None:
    try:
        result = _ejecutar(["fc-list", ":family=Arial Black", "file"], 10)
    except subprocess.TimeoutExpired:
        return None
    if result is None or result.returncode != 0:
        return None
    for linea in (result.stdout or "").splitlines():
        ruta = linea.strip().split(":", 1)[0].strip()
        if ruta:
            return ruta
    return None


def _prefijo(ffmpeg: FFmpeg) -> list[str]:
    if isinstance(ffmpeg, str):
        return [ffmpeg]
    return list(ffmpeg)


def _ejecutar(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        return None


def _detalle(texto: str, limite: int = 240) -> str:
    plano = " ".join((texto or "").split())
    if not plano:
        return ""
    if len(plano) > limite:
        plano = plano[-limite:]
    return f" ({plano})"
