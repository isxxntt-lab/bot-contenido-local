"""Planos con licencia (Pexels, Pixabay o imagen generada) y su registro."""

from __future__ import annotations


def buscar(guion: dict, config: dict | None = None) -> list:
    """Elige los visuales del guion y anota la licencia de cada uno.

    Lo previsto: una lista de ``base.render.Visual`` más un registro
    (origen, autor, URL y tipo de licencia) que viaja en el reporte.
    No descarga nada.
    """
    raise NotImplementedError(
        "La búsqueda de visuales con licencia todavía no está implementada."
    )
