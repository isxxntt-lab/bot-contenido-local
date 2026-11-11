"""Orden del formato. Cada paso es un stub hasta que exista el diseño.

1. Guion: gancho, dato, explicación y cierre.
2. Verificación del dato con dos fuentes.
3. Voz con Edge TTS y marcas WordBoundary.
4. Visuales con licencia (Pexels, Pixabay o IA) y registro de esas licencias.
5. Render con ``base.render.render_pieza`` (1080x1920, voz y subtítulos).
6. QA (``qa.py``) y juez de guion (``clip_judge.py`` como marco de puntuación).
7. YouTube (``youtube_upload.py``) y export TikTok (``tiktok_export.py``).
"""

from __future__ import annotations


def ejecutar(tema: str, config: dict | None = None) -> dict:
    """Recorre el flujo de curiosidades. Hoy no llega a publicar."""
    raise NotImplementedError(
        "El formato de datos curiosos está en construcción. "
        "Guion, verificación, voz y visuales no están implementados; "
        "el render y la subida están en el paquete base."
    )
