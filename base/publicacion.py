"""Subida del reporte a YouTube. El modo en construcción no llama aquí.

Es el antiguo ``_upload`` de clipazos.py, sin buscar clips ni montar nada.
"""

from __future__ import annotations

from pathlib import Path

import publication_guard


def subir_reporte(
    config: dict,
    report_path: Path,
    config_path: Path | None = None,
    dry_run: bool = False,
) -> None:
    """Sube los Shorts del reporte que aún no tengan id. Con dry_run no llama a la API."""
    upload = config.get("subida_youtube", {})
    if not isinstance(upload, dict) or not upload.get("activa"):
        return
    import youtube_upload

    privacy = publication_guard.resolve_privacy(config)
    ruta = Path(report_path)
    config_ruta = config_path
    if dry_run:
        print("\n--dry-run: se comprueba la subida a YouTube sin llamar a la API...")
        youtube_upload.upload_report(
            ruta, privacy, config=config, config_path=config_ruta, dry_run=True,
        )
        return
    if youtube_upload.is_ready():
        print("\nSubiendo a YouTube...")
        youtube_upload.upload_report(ruta, privacy, config=config, config_path=config_ruta)
    else:
        print("\nSubida a YouTube activa pero sin autorizar: ejecuta autorizar_youtube.bat")
