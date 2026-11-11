"""Cierre de una pieza ya montada: exportar a TikTok si el juez y el QA pasaron.

No sube a YouTube. Un fallo de TikTok se anota y no bloquea una subida posterior.
"""

from __future__ import annotations

from pathlib import Path

import tiktok_export


def registrar_exportacion(
    post: dict,
    config: dict | None,
    *,
    dry_run: bool = False,
    juez: object | None = None,
) -> dict:
    """Exporta a TikTok si ``post['qa']`` está en ok y ``juez`` (o el del post) aprueba.

    Si el QA no pasó, marca ``no_subir``. Si la exportación falla, deja
    ``tiktok_export.estado = error`` y no toca ``no_subir``.
    """
    resultado = post.get("qa") or {}
    if not resultado.get("ok") or post.get("bloqueado_por") == "qa":
        post["no_subir"] = True
        post["bloqueado_por"] = "qa"
        return post
    if not tiktok_export.settings_from(config)["enabled"]:
        return post
    try:
        manifest = tiktok_export.export_short(post, config, dry_run=dry_run, juez=juez)
    except Exception as exc:  # noqa: BLE001 - la exportación es secundaria
        post["tiktok_export"] = {"estado": "error", "motivo": str(exc)}
        return post
    carpeta = str(Path(manifest["archivos"]["manifest.json"]).parent)
    post["tiktok_export"] = {
        "estado": "ok",
        "carpeta": carpeta,
        "manifest": manifest["archivos"]["manifest.json"],
    }
    return post
