"""Argumentos de la línea de comandos y el reporte JSON de una ejecución."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from base.configuracion import CLIPAZOS_DIR, CONFIG_PATH, load_config

MENSAJE_CONSTRUCCION = (
    "El formato de datos curiosos está en construcción. No se ha publicado nada."
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """``--dry-run`` y ``--forzar`` se aceptan. Hoy ninguno publica."""
    parser = argparse.ArgumentParser(
        description="Clipazos ES: vídeos de datos curiosos. El formato nuevo aún no publica.",
    )
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument(
        "--forzar",
        action="store_true",
        help="Reservado para rehacer la pieza del día. Hoy no publica.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="No llama a la API de YouTube. El modo normal, hoy, tampoco publica.",
    )
    return parser.parse_args(argv)


def reporte_construccion(config: dict, hoy: str, *, dry_run: bool, forzar: bool) -> dict:
    """Reporte vacío: deja constancia de la pasada y no lleva Shorts."""
    return {
        "canal": config.get("canal", "Clipazos ES"),
        "fecha": hoy,
        "formato": "curiosidades",
        "estado": "en_construccion",
        "mensaje": MENSAJE_CONSTRUCCION,
        "shorts": [],
        "dry_run": dry_run,
        "forzar": forzar,
    }


def escribir_reporte(ruta: Path, payload: dict) -> Path:
    """Guarda el JSON del reporte en UTF-8."""
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return ruta


def ruta_reporte(hoy: str | None = None, *, dry_run: bool = False, carpeta: Path | None = None) -> Path:
    """``output/clipazos/reporte_AAAAMMDD.json`` (con ``_dryrun`` si toca)."""
    dia = hoy or datetime.now().strftime("%Y%m%d")
    sufijo = "_dryrun" if dry_run else ""
    return Path(carpeta or CLIPAZOS_DIR) / f"reporte_{dia}{sufijo}.json"


def cargar(path: Path | None = None) -> dict:
    """Atajo de ``load_config`` para el punto de entrada."""
    return load_config(path)
