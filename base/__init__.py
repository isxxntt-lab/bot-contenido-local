"""Base reutilizable de Clipazos para vídeos verticales de datos curiosos.

La API que usa ``curiosidades/`` está documentada en el README de la raíz.
Este paquete no busca streamers, no baja clips y no habla con un chat.
"""

from base.configuracion import (
    CLIPAZOS_DIR,
    CONFIG_PATH,
    CalidadRender,
    EstiloSubtitulo,
    calidad_de,
    estilo_de,
    load_config,
)
from base.informe import MENSAJE_CONSTRUCCION, parse_args, reporte_construccion
from base.render import Pieza, PlanRender, Visual, plan_render, render_pieza
from base.subtitulos import CuePalabra, escribir_subtitulos, eventos_palabra, sincronizar
from base.texto import hashtag, texto_publicacion
from base.transcripcion import opciones_transcripcion, transcribir

__all__ = [
    "CLIPAZOS_DIR",
    "CONFIG_PATH",
    "CalidadRender",
    "CuePalabra",
    "EstiloSubtitulo",
    "MENSAJE_CONSTRUCCION",
    "Pieza",
    "PlanRender",
    "Visual",
    "calidad_de",
    "escribir_subtitulos",
    "estilo_de",
    "eventos_palabra",
    "hashtag",
    "load_config",
    "opciones_transcripcion",
    "parse_args",
    "plan_render",
    "render_pieza",
    "reporte_construccion",
    "sincronizar",
    "texto_publicacion",
    "transcribir",
]
