# Arquitectura Híbrida — Bot de Contenido (Python → n8n → Motor de Render)

> **Decisión de pivot ejecutada.** Python ya no renderiza vídeo. Python queda como
> *cabeza* de la media house (extracción, guiones, voz, lógica de negocio) y delega
> la edición visual en un motor externo orquestado por n8n vía webhooks.
> Este documento define el contrato de datos, el flujo de n8n y la elección del motor.

---

## 0. Estado de la puesta en marcha (actualizado 2026-10-24)

| Componente | Estado |
|---|---|
| Docker Desktop + n8n 2.44.6 (`n8n/docker-compose.yml`) | ✅ Corriendo en `http://localhost:5678` |
| Workflow `Bot Contenido — Renderer` importado y **publicado** (`active: true`) | ✅ id `60bl0Zo4dcP0zMkO` |
| Webhook `POST /webhook/bot-contenido` con Header Auth | ✅ 403 sin clave · 200 con clave |
| Contrato Python → n8n probado de punta a punta | ✅ `{"status":"aceptado", "id", "motor"}` |
| Cliente Python (`src/n8n_client.py`) con `.venv` + `requests` | ✅ Envío real verificado |
| Credencial n8n `Bot Contenido API Key` (X-API-KEY) | ✅ Creada (id `lPrMx8UCBPtILaub`) |
| Credencial n8n `Telegram Bot` | ⚠️ Placeholder: pega tu token de @BotFather en la UI |
| `PEXELS_API_KEY` / `JSON2VIDEO_API_KEY` / `TELEGRAM_CHAT_ID` | ❌ Rellenar en `n8n/.env.n8n` + `docker compose up -d` |
| Plantilla en JSON2Video | ✅ **TechNewsVertical v1** vive en el nodo Code `Construir Movie JSON` del workflow (no requiere plantilla en su editor). Referencia con placeholders: [`n8n/plantilla-json2video.json`](../n8n/plantilla-json2video.json) |

Cuenta owner local: ver `n8n/CREDENCIALES-LOCAL.txt`. Nota de compatibilidad
descubierta en n8n 2.44: el webhook guarda su credencial bajo la clave
`httpHeaderAuth` (no `headerAuth` como en versiones antiguas) — el workflow de
este repo ya viene corregido. El acceso a `$env.*` dentro de los nodos requiere
`N8N_BLOCK_ENV_ACCESS_IN_NODE=false` (ya configurado en el compose).

---

## 1. Arquitectura objetivo

```
┌─────────────────────────── LOCAL (tu PC) ───────────────────────────┐
│                                                                      │
│  main.py (orquestador, cron local)                                   │
│    ├─ scraper.py      → noticias RSS (TechCrunch, The Verge, WIRED)  │
│    ├─ formatter.py    → guion + hook + hashtags (contenido base)     │
│    ├─ tts.py          → locución edge-tts + timestamps de palabra    │
│    └─ n8n_client.py   → POST webhook  (payload estructurado)         │
│                              │                                       │
└──────────────────────────────┼──────────────────────────────────────┘
                               ▼
              n8n self-hosted en Docker ($0) — localhost:5678
              Workflow "Bot Contenido — Renderer":
                Webhook → normalizar → b-roll (Pexels) → render → aviso
                               │
        ┌──────────────────────┴──────────────────────┐
        ▼ (Fase 1)                                    ▼ (Fase 2)
  JSON2Video API                              Remotion local (Node.js)
  plantilla CapCut-style,                     subtítulos palabra a palabra,
  TTS Azure + subtítulos auto                 b-roll, $0 real, sin watermark
        └──────────────────────┬──────────────────────┘
                               ▼
        MP4 vertical 1080×1920 → Telegram (revisión) → publicación
        (Fase 3: YouTube Shorts → TikTok → Instagram Reels)
```

**Principio clave — motor intercambiable:** el payload lleva `render.motor`.
El workflow de n8n tiene un nodo Switch que enruta al motor elegido sin tocar
Python ni el contrato de datos. Empiezas con JSON2Video (rápido de validar) y
migras a Remotion (coste $0 real) cambiando un campo.

---

## 2. Contrato de datos: Python → n8n

Payload único para todos los motores (ejemplo completo en
[`n8n/payload_ejemplo.json`](../n8n/payload_ejemplo.json)):

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | string | Identificador único del trabajo (`fecha-slug`) |
| `generado_en` | string | Timestamp ISO-8601 |
| `noticia.titulo / fuente / url / resumen` | string | Salida de `scraper.py` + `formatter.py` |
| `video.hook` | string | Primeros ~5 s (los 3 s que deciden la retención) |
| `video.guion` | string | Locución completa (fuente de TTS y subtítulos) |
| `video.hashtags` | list | Para descripción y overlay final |
| `video.voz` | string | Voz edge-tts/Azure (`es-ES-AlvaroNeural`) |
| `audio.modo` | `full_auto` \| `audio_propio` | Ver abajo |
| `audio.palabras` | list | `[{texto, ini, fin}]` — solo en `audio_propio` |
| `broll.keywords` | list | 3-6 términos para buscar clips en Pexels |
| `render.motor` | `json2video` \| `remotion` | Motor de render |
| `render.plantilla` | string | ID de plantilla (solo JSON2Video) |
| `publicacion.plataformas` | list | Fase 3 (`youtube`, `tiktok`, `instagram`) |

### Dos modos de audio (y por qué importa)

- **`full_auto` (recomendado para empezar):** el motor de render genera la voz y
  sincroniza subtítulos él solo. JSON2Video incluye las voces **Azure —las mismas
  que usa edge-tts—**, con lo que la calidad de locución es idéntica y **no hay
  que subir ningún archivo de audio a la nube**. Menos piezas, menos fallos.
- **`audio_propio` (modo avanzado, Fase 2 con Remotion):** Python genera el MP3
  con edge-tts y exporta los *word boundaries* que edge-tts emite de serie, para
  subtítulos palabra a palabra sin forzar alineación externa:

```python
import asyncio
import edge_tts

async def tts_con_boundaries(texto: str, voz: str, ruta_mp3: str) -> list[dict]:
    """Genera locución + timestamps por palabra (offset/duration en 100-ns)."""
    com = edge_tts.Communicate(texto, voz)
    palabras = []
    with open(ruta_mp3, "wb") as f:
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                palabras.append({
                    "texto": chunk["text"],
                    "ini": round(chunk["offset"] / 1e7, 3),                    # segundos
                    "fin": round((chunk["offset"] + chunk["duration"]) / 1e7, 3),
                })
    return palabras
```

> En `audio_propio` el archivo debe ser accesible por URL pública para un motor
> cloud (p. ej. Cloudflare R2, capa gratuita 10 GB). Con Remotion local no hace
> falta: todo queda en tu máquina. Esa es otra razón para migrar a Remotion.

---

## 3. Flujo n8n: workflow "Bot Contenido — Renderer"

Importable desde [`n8n/workflow-renderer.json`](../workflow-renderer.json)
(n8n → Workflows → Import from File). Nodo a nodo:

| # | Nodo | Función |
|---|---|---|
| 1 | **Webhook** `POST /webhook/bot-contenido` | Entrada, protegida con Header Auth (`X-API-KEY`) |
| 2 | **Normalizar Payload** (Set) | Aplana el cuerpo a campos planos con valores por defecto |
| 3 | **Aceptar (HTTP 200)** | Responde a Python en el acto: `{"status": "aceptado", id, motor}` |
| 4 | **Pexels: Buscar B-Roll** | API gratuita de Pexels, búsqueda de vídeo vertical por keywords |
| 5 | **Seleccionar Clips** (Code) | Elige hasta 4 clips verticales en alta calidad |
| 6 | **Elegir Motor** (Switch) | Enruta según `render.motor` |
| 7a | **JSON2Video: Enviar Render** | `POST /v2/movies` con plantilla + variables |
| 7b | **Remotion Local: Render** | `POST http://host.docker.internal:3001/render` (Fase 2) |
| 8 | **Esperar 20s → Estado → ¿Render OK?** | Bucle de sondeo cada 20 s hasta `done`/`error` |
| 9 | **Descargar Vídeo** | Baja el MP4 final como binario |
| 10 | **Telegram: Vídeo Listo / Fallo** | Envía el MP4 al chat para revisión humana |

Detalles de la API JSON2Video usados en el workflow (verificados en su
documentación oficial, octubre 2026):

- Enviar: `POST https://api.json2video.com/v2/movies`, header `x-api-key`,
  cuerpo `{"template": "<id>", "quality": "high", "variables": {...}}`.
- Sondear: `GET https://api.json2video.com/v2/movies?project=<id>`.
- Estados: `pending` → `running` → `done` / `error`; el MP4 queda en
  `movie.url` cuando el estado es `done`.
- El cuerpo admite hasta 2 MB; existe soporte de webhooks de finalización,
  pero el sondeo con `Wait` es más simple y funciona igual con cualquier motor.

### 3.1 Plantilla TechNewsVertical v1 (movie JSON)

No hace falta diseñar plantilla en el editor de JSON2Video: el nodo Code
**`Construir Movie JSON`** del workflow genera el movie completo con cada
ejecución (formato 1080×1920, calidad high). Estructura:

| Escena | Contenido | Duración |
|---|---|---|
| **HOOK** | B-roll (clip1) + titular con caja oscura + badge amarillo con la fuente | 5 s |
| **CUERPO** | B-roll en bucle (clip2) + locución Azure `es-ES-AlvaroNeural` | = voz + 1.2 s |
| **CTA** | B-roll (clip3) + hashtags en Oswald | 3 s |

Los **subtítulos estilo Hormozi** (`classic-one-word`, palabra amarilla
resaltada, contorno negro, Oswald 92, ALL CAPS) van **una sola vez a nivel de
movie**: JSON2Video transcribe la locución y los sincroniza al final del
pipeline. Claves de sintaxis verificadas en su documentación (oct. 2026):

- `subtitles`: un único elemento movie-level; estilos válidos `classic`,
  `classic-progressive`, `classic-one-word`, `boxed-line`, `boxed-word`;
  colores vía `word-color` / `line-color` / `outline-color` (no existe `color`).
- `video`: `resize: "cover"` + `position: "center-center"` para recorte
  vertical; `duration: -2` = durar lo que la escena; `loop: -1` para bucles.
- `text`: estilos CSS dentro de `settings` (usar `color`, no `font-color`).

Para retocar el estilo edita el nodo `Construir Movie JSON` en n8n (o el
archivo de referencia `n8n/plantilla-json2video.json`, que usa `{{placeholders}}`
y se puede probar directamente desde el dashboard de JSON2Video).

---

## 4. Elección del motor de render (verificado oct. 2026)

| Criterio | JSON2Video | Shotstack | Creatomate | **Remotion local** |
|---|---|---|---|---|
| Plan gratuito | 600 créditos únicos, **con watermark** | Sandbox ilimitado, **con watermark** | Solo trial (50 créditos) | **Gratis e ilimitado** |
| Coste producción | $49.95/mes | Desde $10 prepago (~$0.30/min) | Desde $54/mes | **$0** |
| Subtítulos estilo Hormozi | Sí (plantillas) | Sí | Sí | Sí (plantilla propia) |
| TTS integrado (voces Azure) | Sí | Parcial | Sí | edge-tts ya lo resuelve |
| Automatización total | Sí | Sí | Sí | Sí (requiere microservicio) |
| Riesgo de coste a escala | Alto | Medio | Alto | **Ninguno** |

**Decisión en dos fases:**

- **Fase 1 — Validar con JSON2Video.** Sus 600 créditos free (~10-12 vídeos de
  45-60 s, con watermark) dan justo para ajustar la plantilla y el flujo sin
  pagar. Diseñas la plantilla una vez en su editor visual (aspecto CapCut/Hormozi,
  subtítulos dinámicos, huecos para b-roll) y n8n solo rellena variables.
- **Fase 2 — Producción con Remotion local ($0 real, sin watermark).** Remotion
  es gratuito para individuos y empresas de hasta 3 personas, con uso comercial
  permitido. Es un microservicio Node.js que expone `POST /render` y devuelve la
  URL del MP4; se activa desde el mismo payload con `render.motor: "remotion"`.
  Aquí aplicas el modo `audio_propio` + word boundaries para subtítulos
  palabra a palabra idénticos a CapCut.
- **Opción C (opcional, con humano en el bucle):** la API no-oficial de drafts
  de CapCut (`RayBenefit/capcut-api`) genera el *borrador* montado en tu CapCut
  local y tú pulsas exportar. Máxima calidad de edición, pero rompe la
  automatización total. Útil como plan B para vídeos "premium".

---

## 5. Instalación y credenciales

1. **n8n local ($0):** en [`n8n/`](.) ejecuta `docker compose up -d` y abre
   `http://localhost:5678`. Rellena y descomenta las claves en el compose:
   - `PEXELS_API_KEY` — gratis en pexels.com/api
   - `JSON2VIDEO_API_KEY` — gratis (600 créditos) en json2video.com
   - `TELEGRAM_CHAT_ID` — para el bot de avisos
2. **Credenciales en n8n (UI):**
   - *Header Auth* para el webhook → mismo valor que `N8N_API_KEY` en Python.
   - *Telegram Bot* → token de @BotFather.
3. **Python:** exporta `N8N_WEBHOOK_URL=http://localhost:5678/webhook/bot-contenido`
   y `N8N_API_KEY=<tu clave>`. El emisor está en
   [`src/n8n_client.py`](../src/n8n_client.py); integración en `main.py`:

```python
from src.n8n_client import N8NClient, construir_payload

payload = construir_payload(
    noticia=noticia,            # salida de formatter/scraper
    guion=guion,
    hashtags=hashtags,
    hook=hook,
    motor="json2video",
    plantilla="TU_TEMPLATE_ID",
    broll_keywords=["inteligencia artificial", "robot", "tecnologia"],
)
N8NClient().enviar(payload)     # reintentos con backoff incluidos
```

---

## 6. Puesta en marcha y pruebas

1. `docker compose up -d` → importar `workflow-renderer.json` → asignar
   credenciales (Header Auth, Telegram) → **activar** el workflow.
2. Copiar la URL de producción del webhook (Panel del webhook → Production URL).
3. Crear la plantilla en el editor de JSON2Video (vertical 1080×1920, estilo
   CapCut: subtítulos grandes palabra a palabra, hueco para b-roll, caja de
   título, overlay de hashtags) → copiar su **template ID**.
4. Prueba del contrato sin Python: en n8n, pin de datos en el webhook con
   [`payload_ejemplo.json`](../payload_ejemplo.json) → ejecutar → verificar que
   llega el MP4 a Telegram.
5. Prueba end-to-end: `python src/n8n_client.py` (modo demo) → comprobar
   respuesta `{"status": "aceptado"}` y el vídeo en Telegram.
6. Cablear en `main.py` tras `formatter.py`.

---

## 7. Roadmap

| Fase | Entregable | Estado |
|---|---|---|
| 0 | Contrato de datos + workflow n8n + cliente Python + n8n en Docker | **Este paquete** |
| 1 | Plantilla JSON2Video validada, pipeline end-to-end con watermark | Siguiente |
| 2 | Microservicio Remotion (`POST /render`), modo `audio_propio` + word boundaries | Producción $0 |
| 3 | Workflow "Publicador": YouTube Shorts (nodo YouTube de n8n) → TikTok Content Posting API → Instagram Reels (Graph API), todo con APIs gratuitas | Multi-plataforma |
| 4 | Error-workflow global en n8n, registro de publicaciones (Google Sheets), A/B de hooks | Optimización |

**Nota de seguridad:** el webhook exige Header Auth y solo escucha en localhost;
si algún día expones n8n a internet, ponlo tras HTTPS (proxy inverso) y rota la
clave.

---

## 8. Fuentes (verificado octubre 2026)

- [JSON2Video — Pricing](https://json2video.com/pricing) ·
  [API: crear película (POST)](https://json2video.com/docs/v2/reference/api-endpoints/movies-create) ·
  [API: estado (GET)](https://json2video.com/docs/v2/reference/api-endpoints/movies-status) ·
  [Elemento voice (voces Azure)](https://json2video.com/docs/v2/reference/json-syntax/element/voice)
- [Shotstack — Pricing](https://shotstack.io/pricing) ·
  [Quitar watermark de sandbox (foro)](https://community.shotstack.io/t/how-to-remove-the-shotstack-watermark-logo-from-the-video/540)
- [Creatomate — Pricing](https://creatomate.com/pricing)
- [Remotion — License & Pricing](https://www.remotion.dev/docs/license/pricing) ·
  [License FAQ](https://www.remotion.dev/docs/license/faq)
