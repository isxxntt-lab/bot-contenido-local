"""Binarios reales de imageio-ffmpeg 0.7.0 (FFmpeg 7.x) y 0.8.1 (FFmpeg 9).

La suite offline no descarga wheels. Para ejecutarla:

    CLIPAZOS_FFMPEG_REAL=1 python -m pytest -q tests/test_ffmpeg_real.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import entorno

pytestmark = pytest.mark.ffmpeg_real


def _python(venv: Path) -> Path:
    for relativo in (Path("Scripts") / "python.exe", Path("bin") / "python"):
        candidato = venv / relativo
        if candidato.is_file():
            return candidato
    raise FileNotFoundError(f"no hay intérprete en {venv}")


def _ffmpeg_de(venv: Path) -> str:
    codigo = "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
    salida = subprocess.run(
        [_python(venv), "-c", codigo],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return salida.stdout.strip()


def _instalar(venv: Path, version: str) -> str:
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True, text=True)
    instalacion = subprocess.run(
        [_python(venv), "-m", "pip", "install", "-q", "--disable-pip-version-check", f"imageio-ffmpeg=={version}"],
        capture_output=True, text=True,
    )
    if instalacion.returncode != 0:
        raise RuntimeError(instalacion.stderr or instalacion.stdout)
    return _ffmpeg_de(venv)


def _corrida(ffmpeg: str, script: Path, opcion: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            ffmpeg, "-hide_banner", "-v", "error",
            "-f", "lavfi", "-i", entorno.LAVFI_VIDEO,
            "-f", "lavfi", "-i", entorno.LAVFI_AUDIO,
            opcion, str(script),
            "-map", "[vout]", "-map", "[aout]",
            "-f", "null", "-",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def test_imageio_070_y_081_montan_el_grafo_minimo(tmp_path):
    if os.environ.get("CLIPAZOS_FFMPEG_REAL") != "1":
        pytest.skip("prueba de red; definir CLIPAZOS_FFMPEG_REAL=1")
    esperado = {
        "0.7.0": {"script": 0, "slash": 0, "mayor": 7},
        "0.8.1": {"script": 8, "slash": 0, "mayor": 9},
    }
    for version, meta in esperado.items():
        ffmpeg = _instalar(tmp_path / f"venv-{version}", version)
        banner = subprocess.run(
            [ffmpeg, "-version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        texto = (banner.stdout or "") + (banner.stderr or "")
        numeros = entorno.version_ffmpeg(texto)
        assert numeros is not None, texto
        assert numeros[0] == meta["mayor"], texto.splitlines()[0]
        carpeta = tmp_path / version / "Proyecto Bot contenido" / "tmpé x"
        script = carpeta / "g.txt"
        destino = carpeta / "salida.mp4"
        entorno.montar_filtro_minimo(destino, ffmpeg=ffmpeg, script=script)
        assert script.read_text(encoding="utf-8").strip() == entorno.GRAFO_SMOKE
        assert " " in str(script) and "é" in str(script)
        assert destino.stat().st_size > 500
        vieja = _corrida(ffmpeg, script, "-filter_complex_script")
        nueva = _corrida(ffmpeg, script, "-/filter_complex")
        print(
            f"\n[{version}] {texto.splitlines()[0]}\n"
            f"  -filter_complex_script rc={vieja.returncode} stderr={vieja.stderr!r}\n"
            f"  -/filter_complex rc={nueva.returncode} stderr={nueva.stderr!r}\n"
            f"  mp4={destino.stat().st_size} ruta={script}"
        )
        assert vieja.returncode == meta["script"], vieja.stderr
        assert nueva.returncode == meta["slash"], nueva.stderr
        if meta["script"] != 0:
            assert "Unrecognized option" in vieja.stderr
            assert "filter_complex_script" in vieja.stderr
