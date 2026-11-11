"""Punto de entrada de Clipazos ES.

El formato de datos curiosos (guion, voz, visuales) todavía no está hecho.
Si el entorno puede montar, esta pasada termina sin publicar. El render, los
subtítulos y la subida viven en el paquete ``base``.
"""

from __future__ import annotations

import sys
from datetime import datetime

import entorno
from video_editor import _utf8_stdio

from base.configuracion import CLIPAZOS_DIR, load_config
from base.informe import (
    MENSAJE_CONSTRUCCION,
    escribir_reporte,
    parse_args,
    reporte_construccion,
    ruta_reporte,
)
from base.publicacion import subir_reporte

__all__ = [
    "CLIPAZOS_DIR",
    "MENSAJE_CONSTRUCCION",
    "_upload",
    "load_config",
    "main",
    "parse_args",
]


def _upload(config: dict, report_path, config_path=None, dry_run: bool = False) -> None:
    """Sube un reporte ya escrito. El modo por defecto no llama aquí."""
    subir_reporte(config, report_path, config_path, dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    """Comprueba el entorno y sale. No busca clips ni llama a YouTube."""
    _utf8_stdio()
    args = parse_args(argv)
    config = load_config(args.config)
    informe = entorno.comprobar()
    if not informe.apto:
        print(informe.linea_log(), file=sys.stderr)
        return entorno.EXIT_ENTORNO_NO_APTO
    print(informe.linea_log())
    print(MENSAJE_CONSTRUCCION)
    if args.dry_run:
        print("Modo --dry-run: no se llamará a la API de YouTube.")
    hoy = datetime.now().strftime("%Y%m%d")
    ruta = ruta_reporte(hoy, dry_run=bool(args.dry_run), carpeta=CLIPAZOS_DIR)
    escribir_reporte(
        ruta,
        reporte_construccion(config, hoy, dry_run=bool(args.dry_run), forzar=bool(args.forzar)),
    )
    print(f"Reporte: {ruta}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
