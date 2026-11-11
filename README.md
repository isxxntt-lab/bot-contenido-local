# Clipazos ES

El canal deja los clips de streamers y pasa a vídeos de **datos curiosos**.
Esta rama deja la base de edición y de subida lista, y aparta el código que
solo servía para Twitch, Kick y los directos.

El formato nuevo (guion, voz, visuales) **está en construcción**. `clipazos.py`
no publica: si el entorno puede montar, escribe un reporte vacío y termina.
Guion, TTS y planos viven como esqueleto en `curiosidades/`.

## Snapshot del bot de streamers

La copia completa y ejecutable (merges de PR #2 y PR #3, y el WIP del juez
contextual) es la rama **`archivo-streamers-20261111`**, en el commit
`6d6870e7d08729c55aa7d7f039a3f4fae416cf59`. No hay un tag con ese nombre.

`_archive/streamers/` es el recorte movido con `git mv` (el historial de cada
archivo se conserva). Pytest no entra ahí. La config de aquel formato está en
`_archive/streamers/clipazos.streamers.json`.

## Qué se archivó

Código que no reutiliza la base de curiosidades:

| Pieza | Dónde quedó |
| --- | --- |
| Pipeline de clips (`Candidate`, `Moment`, `Layout`, Twitch/Kick/YouTube, radar, chat, picos, webcam) | `_archive/streamers/clipazos.py` |
| Segmentos narrativos, juez contextual, paquete contextual, prueba privada | `_archive/streamers/` |
| Política de derechos tal como estaba en la raíz | `_archive/streamers/rights.py` (la parte común que usa la subida está en `base/permisos.py`) |
| Hotwords del ASR, margen de subtítulo de la webcam (`split_caption_margin_v`) | dentro del `clipazos.py` archivado |
| Pruebas de ese formato, incluido `test_clip_judge.py` (solo hablaba con el segmentador) | `_archive/streamers/tests/` |
| Secciones `twitch`, `kick`, `youtube`, `rights`, `seleccion`, `radar`, `asr` y `semanal` | solo en `clipazos.streamers.json` |

`test_rights` (tres casos de `unknown_permission_policy = allow` fallaban a
propósito) y `test_una_parada_ajena_con_un_post_no_se_adopta` se archivaron
sin arreglarlos.

## Qué se recicla

| Pieza | Dónde vive |
| --- | --- |
| Subida a YouTube | `youtube_upload.py` (importa el permiso desde `base.permisos`) |
| Autorización OAuth | `autorizar_youtube.bat` |
| Kill switch, cupos y la base SQLite | `publication_guard.py`. El cupo `max_por_streamer_dia` y la columna `streamer` se quedan: ahí están los Shorts ya publicados. **TODO:** pasará a ser cupo por tema o serie. La base sigue en `data/publicaciones.sqlite3` (no se versiona y no se borra). |
| QA bloqueante, caras opcionales | `qa.py` (`qa.caras.activa` sigue en false) |
| Export TikTok | `tiktok_export.py`. `approval(short, juez=...)` usa el veredicto que se le pasa. Si no se pasa, mira `short["juez"]` y, si tampoco está, `short["seleccion"]` para los reportes viejos. |
| FFmpeg `-/filter_complex`, reloj ASS | `entorno.py`, `video_editor.py` |
| Marco de criterios y puntuación | `clip_judge.py` (ya no importa el segmentador de clips) |
| Render, subtítulos, transcripción, textos, reporte | paquete `base/` |
| Lanzador | `run_clipazos.bat` sigue llamando a `python clipazos.py` |

### Dónde quedó cada función que salía de `clipazos.py`

| Antes | Ahora |
| --- | --- |
| `load_config` | `base.configuracion.load_config` |
| `transcribe`, `_whisper`, `_opciones_transcripcion` | `base.transcripcion.transcribir`, `_whisper`, `opciones_transcripcion` (sin hotwords de streamer) |
| `write_short_captions`, `_live_caption_events`, `_pop` | `base.subtitulos.escribir_subtitulos` y `eventos_palabra` (el resalte y el margen salen de la config) |
| `split_caption_margin_v` | archivada: era el margen de la webcam. La zona segura es `subtitulos.margen_superior` / `margen_inferior` |
| `render_short`, `_assemble_cuts` | `base.render.render_pieza` y `plan_render` (visuales + voz, sin layout de directo) |
| `_hashtag`, `post_text`, `_title_text` | `base.texto.hashtag` y `texto_publicacion` |
| `_upload` | `base.publicacion.subir_reporte` y `clipazos._upload` |
| `parse_args` (`--dry-run`, `--forzar`) y el reporte | `base.informe` y `clipazos.main` |
| `sync_cues`, reloj ASS, binario de FFmpeg | siguen en `video_editor.py` |

## API de la base (la que usará `curiosidades/`)

```python
from pathlib import Path
from video_editor import CaptionCue
from base.render import Pieza, Visual, render_pieza

pieza = Pieza(
    visuales=(Visual(Path("plano.jpg"), tipo="imagen"),),
    voz=Path("voz.mp3"),
    palabras=(CaptionCue("hola", 0.10, 0.42), CaptionCue("mundo", 0.42, 0.90)),
    duracion=8.0,
    titulo="¿Sabías que…?",
)
render_pieza(pieza, Path("output/clipazos/pieza.mp4"))  # lee clipazos.json
```

- `Pieza`: lista de visuales (imagen o vídeo), pista de voz y cues de palabra.
  Los cues valen tanto si vienen de Edge TTS (WordBoundary) como de Whisper.
- `render_pieza` / `plan_render`: vertical **1080x1920**. Cada plano cubre el
  lienzo y se recorta al centro. El audio de los planos no se usa: suena la voz.
- `plan_render` escribe el `.ass` y el grafo y devuelve el comando, sin ejecutar
  FFmpeg. El grafo entra por `-/filter_complex` (`entorno.opcion_filtro_complejo`).
- Subtítulos palabra a palabra en ASS. Fuente, tamaño, color de resalte
  (BGR de ASS: amarillo `00FFFF`) y márgenes salen de `clipazos.json` → `subtitulos`.
  Alineación 2: `margen_inferior` (480 px) deja libre la barra de Shorts/TikTok.
  `margen_superior` (250 px) es el tope del bloque. No hay números sueltos en el render.
- Calidad en `clipazos.json` → `render`: códec, preset, CRF, fps, bitrate,
  `loudness_lufs` **-14**, true peak -1.5 y LRA 11 (`loudnorm`, el mismo criterio
  que ya usaba el montaje). `qa.fps` del config activo es 30, igual que `render.fps`.
- `base.transcripcion.transcribir` es el camino Whisper, por si no hay marcas
  de WordBoundary. No envía hotwords.
- `base.salida.registrar_exportacion(post, config, juez=veredicto)` exporta a
  TikTok si el QA pasó y ese juez aprobó. No sube a YouTube.
- `clipazos._upload(...)` sube un reporte ya escrito. `main` no lo llama.

`curiosidades/README.md` describe el flujo previsto: guion (gancho + dato +
explicación + cierre) → dos fuentes → Edge TTS → visuales con licencia →
render → QA → juez → YouTube y TikTok.

## Instalación

Python 3.11 o superior, desde esta carpeta:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Comprobación rápida:

```powershell
.\.venv\Scripts\python.exe -c "import clipazos, base, curiosidades, youtube_upload, publication_guard, qa"
```

## Uso

```powershell
.\run_clipazos.bat
.\.venv\Scripts\python.exe clipazos.py --dry-run
```

El `.bat` activa `.venv`, ejecuta `clipazos.py` y deja el log en
`output\clipazos\ultimo_run.log` (y lo acumula en `historial.log`). `output/`
no se versiona.

Hoy el modo por defecto, también con `--dry-run` o `--forzar`, **no publica**.
Si FFmpeg o la fuente no valen, sale con código `entorno_no_apto` (2) y no
escribe el reporte. Si el entorno es apto, el reporte
`output/clipazos/reporte_<fecha>.json` queda en `en_construccion` y `shorts` vacío.

La subida del config activo está apagada (`subida_youtube.activa: false`).
Para un reporte que ya exista:

```powershell
.\.venv\Scripts\python.exe youtube_upload.py output\clipazos\reporte_AAAAMMDD.json --dry-run
```

## Pruebas

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q tests
```

No usan red ni credenciales. El render nuevo se comprueba mirando el comando y
el `.ass`, sin FFmpeg. Las pruebas de QA, TikTok y el grafo mínimo sí usan el
FFmpeg de `imageio-ffmpeg`. `tests/test_ffmpeg_real.py` se salta si no está
`CLIPAZOS_FFMPEG_REAL=1` (descarga wheels). La carpeta `_archive` no entra en
la colección.

## Publicación y credenciales

La guardia (`publication_guard.py`) sigue con kill switch, cupos y la base
SQLite. Un vídeo ya publicado no se vuelve a subir. La carpeta `credenciales/`
(`client_secret.json`, `youtube_token.json`) y cualquier `.env` están en
`.gitignore` y no se tocan.

La primera autorización, cuando el formato publique, sigue siendo
`autorizar_youtube.bat`.
