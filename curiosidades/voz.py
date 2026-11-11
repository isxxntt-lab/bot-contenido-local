"""Voz con Edge TTS y marcas WordBoundary, listas para los subtítulos de ``base``."""

from __future__ import annotations

from pathlib import Path


def sintetizar(guion: dict, destino: Path, config: dict | None = None) -> dict:
    """Genera la pista de voz y las marcas de palabra.

    Lo previsto: un archivo de audio y una lista de cues (texto, inicio, fin)
    compatible con ``base.render.Pieza.palabras``. No llama a Edge TTS.
    """
    raise NotImplementedError(
        "La síntesis con Edge TTS y WordBoundary todavía no está implementada."
    )
