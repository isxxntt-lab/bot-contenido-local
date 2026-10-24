"""
Cliente webhook: Python -> n8n.

Envia el payload estructurado de cada noticia al workflow de n8n que coordina
el render del video vertical (JSON2Video / Remotion) y su publicacion.

Variables de entorno:
    N8N_WEBHOOK_URL  (por defecto http://localhost:5678/webhook/bot-contenido)
    N8N_API_KEY      (mismo valor que la credencial Header Auth del webhook en n8n)
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "http://localhost:5678/webhook/bot-contenido")
API_KEY = os.getenv("N8N_API_KEY", "")
INTENTOS = 3
PAUSA_BASE_SEG = 5
TIMEOUT_SEG = 30


def construir_payload(
    noticia: dict[str, Any],
    guion: str,
    hashtags: list[str],
    hook: str,
    *,
    motor: str = "json2video",
    plantilla: str = "",
    voz: str = "es-ES-AlvaroNeural",
    broll_keywords: list[str] | None = None,
    duracion_objetivo: int = 45,
    plataformas: list[str] | None = None,
) -> dict[str, Any]:
    """Convierte la salida de scraper/formatter en el contrato de datos de n8n.

    `noticia` debe incluir titulo, fuente, url y resumen (salida de formatter.py).
    """
    slug = uuid.uuid4().hex[:8]
    fecha = datetime.now(timezone.utc).strftime("%Y%m%d")
    ahora = datetime.now(timezone.utc).isoformat()
    return {
        "id": f"{fecha}-{slug}",
        "generado_en": ahora,
        "noticia": noticia,
        "video": {
            "formato": "9:16",
            "duracion_objetivo_seg": duracion_objetivo,
            "hook": hook,
            "guion": guion,
            "hashtags": hashtags,
            "voz": voz,
        },
        "audio": {"modo": "full_auto", "archivo": None, "palabras": []},
        "broll": {"keywords": broll_keywords or [], "clips": []},
        "render": {"motor": motor, "plantilla": plantilla},
        "publicacion": {"plataformas": plataformas or ["youtube"]},
    }


class N8NError(RuntimeError):
    """n8n no acepto el trabajo tras los reintentos."""


class N8NClient:
    def __init__(self, url: str | None = None, api_key: str | None = None) -> None:
        self.url = url or WEBHOOK_URL
        self.api_key = api_key or API_KEY
        if not self.api_key:
            logger.warning(
                "N8N_API_KEY vacio: el webhook de n8n rechazara la peticion si usa Header Auth."
            )

    def enviar(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST con reintentos y backoff lineal.

        n8n responde 200 con {"status": "aceptado", ...} en cuanto valida el
        payload; el render sigue en segundo plano dentro de n8n.
        """
        headers = {"Content-Type": "application/json", "X-API-KEY": self.api_key}
        ultimo_error: Exception | None = None
        for intento in range(1, INTENTOS + 1):
            try:
                resp = requests.post(
                    self.url, json=payload, headers=headers, timeout=TIMEOUT_SEG
                )
                resp.raise_for_status()
                data = resp.json()
                logger.info("n8n acepto el trabajo %s -> %s", payload.get("id"), data)
                return data
            except (requests.RequestException, ValueError) as exc:
                ultimo_error = exc
                logger.warning(
                    "Intento %d/%d fallo enviando %s: %s",
                    intento,
                    INTENTOS,
                    payload.get("id"),
                    exc,
                )
                if intento < INTENTOS:
                    time.sleep(PAUSA_BASE_SEG * intento)
        raise N8NError(
            f"No se pudo entregar {payload.get('id')} a n8n tras {INTENTOS} intentos"
        ) from ultimo_error


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    demo = construir_payload(
        noticia={
            "titulo": "Demo de integracion",
            "fuente": "TechCrunch",
            "url": "https://example.com/demo",
            "resumen": "Resumen de la noticia de prueba.",
        },
        guion="Texto de la locucion de prueba para validar el pipeline.",
        hashtags=["#IA", "#prueba"],
        hook="Esto cambia todo.",
        plantilla="TU_TEMPLATE_ID",
    )
    print(N8NClient().enviar(demo))
