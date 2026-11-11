"""Preflight del entorno y argumentos de -/filter_complex, sin red.

El binario de FFmpeg es un script de Python. La prueba con los wheels reales
está en test_ffmpeg_real.py y no forma parte de la suite offline.
"""

from __future__ import annotations

import json
import stat
import sys
from datetime import datetime
from pathlib import Path

import pytest

import clipazos
import entorno
from video_editor import CaptionCue


def _fuente(carpeta: Path) -> Path:
    carpeta.mkdir(parents=True, exist_ok=True)
    archivo = carpeta / "ariblk.ttf"
    archivo.write_bytes(b"fuente-de-prueba")
    return archivo


def _binario(
    tmp_path: Path,
    *,
    version: str = "7.1",
    filtros: tuple[str, ...] = entorno.FILTROS,
    smoke_rc: int = 0,
    filtros_rc: int = 0,
    marcas: str = " ...",
) -> list[str]:
    """Script que imita las respuestas de ffmpeg que mira el preflight."""
    script = tmp_path / "ffmpeg_falso.py"
    script.write_text(
        f"""#!/usr/bin/env python3
import sys
from pathlib import Path

args = sys.argv[1:]
registro = Path({str(tmp_path / "argv.txt")!r})
previo = registro.read_text(encoding="utf-8") if registro.exists() else ""
registro.write_text(previo + "\\n".join(args) + "\\n---\\n", encoding="utf-8")

if args[:1] == ["-version"]:
    print("ffmpeg version {version} Copyright (c) 2000-2026 the FFmpeg developers")
    raise SystemExit(0)
if "-filters" in args:
    print("Filters:")
    for nombre in {list(filtros)!r}:
        print("{marcas} " + nombre + " V->V filtro de prueba")
    raise SystemExit({filtros_rc})
if "-/filter_complex" in args:
    ruta = Path(args[args.index("-/filter_complex") + 1])
    texto = ruta.read_text(encoding="utf-8")
    if texto.strip() != {entorno.GRAFO_SMOKE!r}:
        print("grafo distinto: " + texto, file=sys.stderr)
        raise SystemExit(3)
    if {smoke_rc} != 0:
        print("Unrecognized option simulado", file=sys.stderr)
    raise SystemExit({smoke_rc})
print("argumentos inesperados", args, file=sys.stderr)
raise SystemExit(1)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return [sys.executable, str(script)]


def _apto(tmp_path: Path, **kwargs) -> entorno.InformeEntorno:
    ffmpeg = _binario(tmp_path, **kwargs)
    return entorno.comprobar(ffmpeg, carpeta_fuentes=tmp_path / "fuentes")


def test_el_constructor_conserva_espacios_y_acentos():
    ruta = r"C:\Users\pc\Proyecto Bot contenido\tmpé x\g.txt"
    assert entorno.opcion_filtro_complejo(ruta) == ["-/filter_complex", ruta]
    assert " " in ruta and "é" in ruta
    assert "'" not in entorno.opcion_filtro_complejo(ruta)[1]


def test_el_grafo_minimo_usa_la_misma_opcion_y_el_lavfi_de_un_segundo(tmp_path):
    script = tmp_path / "g.txt"
    argv = entorno.argumentos_grafo_minimo("ffmpeg", script, ["-f", "null", "-"])
    assert argv == [
        "ffmpeg",
        "-f", "lavfi", "-i", "testsrc=d=1:s=640x360:r=30",
        "-f", "lavfi", "-i", "sine=d=1",
        "-/filter_complex", str(script),
        "-map", "[vout]", "-map", "[aout]",
        "-f", "null", "-",
    ]
    assert "-filter_complex_script" not in argv


def test_render_short_pasa_la_ruta_con_espacios_sin_la_opcion_vieja(tmp_path, monkeypatch):
    capturado: dict[str, list[str]] = {}

    def grabar(command):
        capturado["cmd"] = command

    monkeypatch.setattr(clipazos, "_run_ffmpeg", grabar)
    monkeypatch.setattr(clipazos, "_ffmpeg_exe", lambda: "ffmpeg-falso")
    destino = tmp_path / "Proyecto Bot contenido" / "tmpé x" / "short final.mp4"
    destino.parent.mkdir(parents=True)
    candidato = clipazos.Candidate("Prueba local", "TEST", "Título", 0, "local:test", "k")
    momento = clipazos.Moment(
        candidato, tmp_path / "clip.mp4", 0.0, 1.0, [CaptionCue("hola", 0.0, 0.4)],
        layout=clipazos.Layout("blur", 16 / 9),
    )
    clipazos.render_short([momento], [[("HOLA", True)]], "TEST", destino)
    comando = capturado["cmd"]
    assert "-filter_complex_script" not in comando
    indice = comando.index("-/filter_complex")
    script = Path(comando[indice + 1])
    assert script.is_file()
    assert "tmpé x" in script.as_posix()
    assert " " in script.as_posix()
    assert "[vout]" in script.read_text(encoding="utf-8")


def test_banners_reales_de_imageio_y_de_ffmpeg_6():
    assert entorno.version_ffmpeg(
        "ffmpeg version 7.0.2-static https://johnvansickle.com/ffmpeg/  Copyright"
    ) == (7, 0, 2)
    assert entorno.token_version(
        "ffmpeg version 7.0.2-static https://johnvansickle.com/ffmpeg/  Copyright"
    ) == "7.0.2-static"
    assert entorno.version_ffmpeg(
        "ffmpeg version n9.0.2-17-g2a571b6068-20260930 Copyright (c) 2000-2026"
    ) == (9, 0, 2)
    assert entorno.version_ffmpeg(
        "ffmpeg version 6.1.1-3ubuntu5 Copyright (c) 2000-2023 the FFmpeg developers"
    ) == (6, 1, 1)
    assert entorno.version_ffmpeg("ffmpeg version 7.1 Copyright") == (7, 1, 0)
    assert entorno.version_ffmpeg("no es ffmpeg") is None


def test_los_filtros_de_ffmpeg_7_y_de_ffmpeg_9():
    siete = "\n".join([
        " ... ass               V->V       Render ASS subtitles",
        " ... loudnorm          A->A       EBU R128 loudness normalization",
        " .S. blackdetect       V->V       Detect video intervals",
        " ... freezedetect      V->V       Detects frozen video input.",
        " ... ebur128           A->N       EBU R128 scanner.",
    ])
    nueve = "\n".join([
        " .. ass               V->V       Render ASS subtitles onto input video",
        " .. loudnorm          A->A       EBU R128 loudness normalization",
        " .S blackdetect       V->V       Detect video intervals that are (almost) black.",
        " .. freezedetect      V->V       Detects frozen video input.",
        " .. ebur128           A->N       EBU R128 scanner.",
        " .. blackdetect_vulkan V->V       Detect video intervals",
    ])
    assert set(entorno.FILTROS) <= entorno.nombres_filtros(siete)
    assert set(entorno.FILTROS) <= entorno.nombres_filtros(nueve)
    assert "blackdetect_vulkan" in entorno.nombres_filtros(nueve)


def test_preflight_apto_con_binario_simulado(tmp_path):
    _fuente(tmp_path / "fuentes")
    informe = _apto(tmp_path)
    assert informe.apto
    assert informe.codigo is None
    assert informe.version == "7.1"
    assert informe.linea_log() == "entorno apto: FFmpeg 7.1"
    argv = (tmp_path / "argv.txt").read_text(encoding="utf-8")
    assert "-/filter_complex" in argv
    assert "-filter_complex_script" not in argv
    assert "testsrc=d=1:s=640x360:r=30" in argv
    assert "sine=d=1" in argv
    assert "-f\nnull\n-" in argv or "-f null -" in argv.replace("\n", " ")


def test_version_anterior_a_7_es_entorno_no_apto(tmp_path):
    _fuente(tmp_path / "fuentes")
    informe = _apto(tmp_path, version="6.1.1-3ubuntu5")
    assert not informe.apto
    assert informe.codigo == "entorno_no_apto"
    assert informe.linea_log().startswith("entorno_no_apto:")
    assert any("anterior a 7" in motivo for motivo in informe.motivos)


def test_falta_un_filtro_o_la_fuente(tmp_path):
    _fuente(tmp_path / "fuentes")
    sin_ass = _apto(tmp_path, filtros=tuple(nombre for nombre in entorno.FILTROS if nombre != "ass"))
    assert not sin_ass.apto
    assert any("faltan los filtros ass" in motivo for motivo in sin_ass.motivos)

    vacia = tmp_path / "vacia"
    vacia.mkdir()
    otro = tmp_path / "otro"
    otro.mkdir()
    informe = entorno.comprobar(_binario(otro), carpeta_fuentes=vacia)
    assert not informe.apto
    assert any("Arial Black" in motivo for motivo in informe.motivos)
    assert informe.linea_log().startswith("entorno_no_apto:")


def test_el_smoke_con_codigo_distinto_de_cero_para_el_lote(tmp_path):
    _fuente(tmp_path / "fuentes")
    informe = _apto(tmp_path, smoke_rc=8)
    assert not informe.apto
    assert informe.codigo == "entorno_no_apto"
    assert any("smoke" in motivo and "código 8" in motivo for motivo in informe.motivos)
    assert "Unrecognized option simulado" in informe.linea_log()


def test_sin_binario_no_se_monta(tmp_path):
    informe = entorno.comprobar(str(tmp_path / "no-existe-ffmpeg"), carpeta_fuentes=tmp_path)
    assert not informe.apto
    assert informe.linea_log().startswith("entorno_no_apto:")
    assert any("no se encuentra el FFmpeg" in motivo for motivo in informe.motivos)


def test_la_fuente_se_encuentra_por_nombre_sin_importar_mayusculas(tmp_path):
    carpeta = tmp_path / "Fonts"
    carpeta.mkdir()
    (carpeta / "ARIBLK.TTF").write_bytes(b"x")
    assert entorno.fuente_arial_black(carpeta) is not None
    assert entorno.fuente_arial_black(tmp_path / "nada") is None


def test_fontconfig_no_se_consulta_si_la_carpeta_es_explicita(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("fc-list no debería ejecutarse")

    monkeypatch.setattr(entorno, "_fuente_fontconfig", boom)
    assert entorno.fuente_arial_black(tmp_path) is None


def test_main_se_para_antes_de_buscar_descargar_o_subir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        entorno, "comprobar",
        lambda: entorno.InformeEntorno(False, ("faltan los filtros ass",), version="9.0.2"),
    )
    llamadas: list[str] = []
    monkeypatch.setattr(clipazos, "scout_twitch", lambda *a, **k: llamadas.append("scout"))
    monkeypatch.setattr(clipazos, "scout_kick", lambda *a, **k: llamadas.append("kick"))
    monkeypatch.setattr(clipazos, "scout_youtube", lambda *a, **k: llamadas.append("youtube"))
    monkeypatch.setattr(clipazos, "fetch_media", lambda *a, **k: llamadas.append("descarga"))
    monkeypatch.setattr(clipazos, "render_short", lambda *a, **k: llamadas.append("montaje"))
    monkeypatch.setattr(clipazos, "_upload", lambda *a, **k: llamadas.append("subida"))
    monkeypatch.setattr(clipazos, "CLIPAZOS_DIR", tmp_path)
    config = tmp_path / "clipazos.json"
    config.write_text(json.dumps({"canal": "Clipazos ES"}), encoding="utf-8")
    codigo = clipazos.main(["--config", str(config), "--forzar", "--dry-run"])
    assert codigo == entorno.EXIT_ENTORNO_NO_APTO
    assert llamadas == []
    assert list(tmp_path.glob("reporte_*.json")) == []
    assert list(tmp_path.glob("*.mp4")) == []


def test_un_reporte_de_hoy_no_se_sube_si_el_entorno_falla(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        entorno, "comprobar",
        lambda: entorno.InformeEntorno(
            False, ("el smoke de -/filter_complex terminó con código 8",), version="9.0.2"
        ),
    )
    subidas: list[object] = []
    monkeypatch.setattr(clipazos, "_upload", lambda *a, **k: subidas.append(a))
    monkeypatch.setattr(clipazos, "CLIPAZOS_DIR", tmp_path)
    hoy = datetime.now().strftime("%Y%m%d")
    (tmp_path / f"reporte_{hoy}.json").write_text(
        json.dumps({"shorts": [{"streamer": "Ana", "video": "x.mp4"}]}), encoding="utf-8"
    )
    config = tmp_path / "clipazos.json"
    config.write_text(
        json.dumps({"canal": "Clipazos ES", "subida_youtube": {"activa": True}}),
        encoding="utf-8",
    )
    assert clipazos.main(["--config", str(config)]) == entorno.EXIT_ENTORNO_NO_APTO
    assert subidas == []
    error = capsys.readouterr().err
    assert error.splitlines()[0].startswith("entorno_no_apto:")
    assert "código 8" in error


def test_marcas_de_filtro_de_ffmpeg_9_en_el_binario_simulado(tmp_path):
    _fuente(tmp_path / "fuentes")
    informe = _apto(tmp_path, marcas=" ..")
    assert informe.apto, informe.motivos


@pytest.mark.parametrize("marca", [" .S", " .S."])
def test_blackdetect_con_las_dos_anchuras_de_marca(marca):
    texto = f"{marca} blackdetect       V->V       Detect"
    assert "blackdetect" in entorno.nombres_filtros(texto)


def test_una_ruta_con_espacios_y_acentos_monta_de_verdad(tmp_path):
    """El argv no pasa por el shell: el espacio y la é llegan al binario real."""
    carpeta = tmp_path / "Proyecto Bot contenido" / "tmpé x"
    script = carpeta / "g.txt"
    destino = carpeta / "salida.mp4"
    entorno.montar_filtro_minimo(destino, script=script)
    assert destino.is_file()
    assert destino.stat().st_size > 500
    assert script.read_text(encoding="utf-8").strip() == entorno.GRAFO_SMOKE
    assert " " in str(script) and "é" in str(script)
    assert entorno.opcion_filtro_complejo(script) == ["-/filter_complex", str(script)]
