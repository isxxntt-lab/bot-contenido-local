"""Comprueba el dato con dos fuentes antes de sintetizar la voz."""

from __future__ import annotations


def verificar(guion: dict, config: dict | None = None) -> dict:
    """Confirma el dato del guion con dos fuentes independientes.

    La forma prevista anota cada fuente (título, URL y qué frase respalda).
    Si no hay dos fuentes, el dato no sigue hacia la voz.
    """
    raise NotImplementedError(
        "La verificación con dos fuentes todavía no está implementada."
    )
