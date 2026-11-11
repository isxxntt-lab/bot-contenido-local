"""Formato de datos curiosos. Solo el esqueleto: otro agente implementa el flujo.

El orden previsto está en ``pipeline.ejecutar`` y en el README de este paquete.
El montaje, los subtítulos y la subida ya están en ``base``.
"""

from curiosidades.guion import escribir_guion
from curiosidades.pipeline import ejecutar
from curiosidades.verificacion import verificar
from curiosidades.visuales import buscar
from curiosidades.voz import sintetizar

__all__ = ["buscar", "ejecutar", "escribir_guion", "sintetizar", "verificar"]
