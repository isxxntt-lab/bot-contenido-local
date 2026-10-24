# Bot de contenido local

Bot autónomo que lee noticias de tecnología e IA desde feeds RSS públicos y arma posts de texto con reglas locales. No usa APIs de pago ni modelos en la nube.

## Requisitos

Python 3.11 o superior.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Uso

Desde esta carpeta, con el entorno virtual activo:

```powershell
python main.py
python main.py --limit 2
```

Los posts quedan en `output/` (esa carpeta no se versiona). `output/latest.txt` y `output/latest.json` son siempre el último resultado.

## Módulos

- `scraper.py`: descarga título, enlace y resumen desde TechCrunch, The Verge y WIRED.
- `formatter.py`: asigna hashtags por palabras clave y rellena una plantilla de texto.
- `main.py`: orquesta el flujo y guarda el resultado.

Para añadir un hashtag, edita `KEYWORD_HASHTAGS` en `formatter.py`.

## Flujo híbrido con n8n (opcional)

Si defines `N8N_WEBHOOK_URL` y `N8N_API_KEY`, `main.py` envía cada noticia al
workflow de n8n: b-roll de Pexels, render vertical con JSON2Video (subtítulos
dinámicos palabra a palabra) y aviso con el MP4 por Telegram. Sin esas variables,
el bot usa el montaje 100% local de `video_editor.py` como plan B.

- Workflow importable y docker-compose de n8n: carpeta `n8n/`.
- Contrato de datos, arquitectura y puesta en marcha: `docs/ARQUITECTURA-HIBRIDA.md`.
- Claves de servicios (Pexels, JSON2Video, Telegram): `n8n/.env.n8n.example`.
