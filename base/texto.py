"""Textos de publicación que no dependen de un streamer ni de un clip."""

from __future__ import annotations

import re


def hashtag(texto: str) -> str:
    """Un hashtag en minúsculas, solo con letras y números."""
    limpio = re.sub(r"[^\wáéíóúñü]+", "", texto, flags=re.IGNORECASE)
    return "#" + limpio.casefold()


def texto_publicacion(titulo: str, cuerpo: str, etiquetas: list[str] | None = None) -> dict:
    """Título, descripción y hashtags listos para el reporte.

    El cuerpo lo escribe quien llama (créditos de Pexels, Pixabay o la voz).
    Este módulo no inventa créditos de streamers.
    """
    vistos: set[str] = set()
    etiquetas_limpias: list[str] = []
    for etiqueta in etiquetas or []:
        token = str(etiqueta).strip()
        if not token:
            continue
        token = hashtag(token[1:] if token.startswith("#") else token)
        clave = token.casefold()
        if clave in vistos or len(token) < 2:
            continue
        vistos.add(clave)
        etiquetas_limpias.append(token)
    return {
        "titulo": " ".join(titulo.split()),
        "descripcion": cuerpo.strip(),
        "hashtags": etiquetas_limpias,
    }
