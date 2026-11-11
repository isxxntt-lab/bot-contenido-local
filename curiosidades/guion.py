"""Guion en cuatro tiempos: gancho, dato, explicación y cierre."""

from __future__ import annotations


def escribir_guion(tema: str, config: dict | None = None) -> dict:
    """Devuelve el guion del tema. Todavía no está implementado.

    La forma prevista es un dict con las claves ``gancho``, ``dato``,
    ``explicacion`` y ``cierre``, más el texto seguido para la voz.
    """
    raise NotImplementedError(
        "El guion de curiosidades (gancho, dato, explicación y cierre) "
        "lo implementa otro agente a partir de un diseño aparte."
    )
