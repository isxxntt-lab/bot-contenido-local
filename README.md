# Clipazos ES

Bot local que monta Shorts verticales en español con los mejores clips de
streamers de Twitch y Kick y de youtubers. Busca los clips, los transcribe,
encuadra la webcam del streamer, quema subtítulos palabra a palabra y, si está
activado, sube el resultado a YouTube Shorts.

## Archivos

- `clipazos.py`: flujo diario. Localiza clips (Twitch, Kick y YouTube con `yt-dlp`),
  transcribe con `faster-whisper`, detecta la cara con el modelo YuNet de
  `assets/models/` y monta el Short con FFmpeg.
- `video_editor.py`: utilidades comunes de subtítulos ASS y FFmpeg.
- `youtube_upload.py`: subida a YouTube con la API oficial (OAuth).
- `rights.py`: política de derechos (qué streamers y fuentes se pueden usar).
- `qa.py`: control de calidad bloqueante del vídeo montado.
- `publication_guard.py`: kill switch, cupos y duplicados de YouTube (SQLite).
- `clipazos.json`: configuración (streamers de Kick, categorías de Twitch, canales
  de YouTube, duración, privacidad de la subida, días que se guardan los vídeos...).
- `run_clipazos.bat`: lanzador que usa el venv y guarda el log.
- `autorizar_youtube.bat`: autoriza la cuenta de YouTube la primera vez.

## Instalación

Python 3.11 o superior, en Windows (PowerShell), desde esta carpeta:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Comprobación rápida:

```powershell
.\.venv\Scripts\python.exe -c "import clipazos, youtube_upload, publication_guard, rights, qa"
```

## Uso

```powershell
.\run_clipazos.bat
```

El `.bat` activa `.venv`, ejecuta `clipazos.py` y deja los vídeos y los reportes
en `output\clipazos\`. La última ejecución queda en `output\clipazos\ultimo_run.log`
y todas se acumulan en `output\clipazos\historial.log`. La carpeta `output/` no se
versiona.

La tarea programada de Windows `BotContenidoLocal` lanza `run_clipazos.bat`
automáticamente.

### Prueba sin subir (`--dry-run`)

```powershell
.\.venv\Scripts\python.exe clipazos.py --dry-run
.\.venv\Scripts\python.exe youtube_upload.py output\clipazos\reporte_20261109.json --dry-run
```

`clipazos.py --dry-run` hace todo lo demás: busca clips, **descarga y monta** los
permitidos, pasa derechos, QA, cupos y kill switch, pero no llama a la API de
YouTube ni gasta cupo en la base. Los archivos llevan el sufijo `_dryrun`
(`reporte_<fecha>_dryrun.json`, `streamer_<nombre>_<fecha>_dryrun.mp4`) para no pisar
los de verdad, y no cuentan para «no repetir streamer». El radar sí se actualiza.
`youtube_upload.py --dry-run` repite las comprobaciones sobre un reporte ya hecho
sin tocar el reporte ni la base.

## Seguridad de publicación

Orden en cada ejecución:

1. **Derechos** (`rights.py`), antes de descargar o editar: los clips sin permiso
   ni se descargan.
2. **QA** (`qa.py`), después de montar: si falla, el Short queda en el reporte con
   `"no_subir": true` y el motivo en `"qa"`.
3. **Guardia** (`publication_guard.py`), antes de subir: kill switch, cupos y
   duplicados. `youtube_upload.py` repite derechos + QA + guardia justo antes de la
   API, también cuando se ejecuta por separado.

### Bloque `publication`

| Clave | Qué hace | Si falta |
| --- | --- | --- |
| `kill_switch` | `true` para cortar las subidas. La descarga y el montaje siguen. Se relee del disco antes de cada subida. | `false` |
| `kill_switch_file` | Si existe este archivo en la carpeta (por defecto `STOP_YOUTUBE`), no se sube nada. | `STOP_YOUTUBE` |
| `max_por_dia` | Subidas máximas por día (hora de Madrid). | 1 |
| `max_por_ejecucion` | Subidas máximas en una misma ejecución. | 1 |
| `max_por_streamer_dia` | Subidas máximas del mismo streamer al día. | 1 |
| `zona_horaria` | Zona para contar los días. | `Europe/Madrid` |
| `db_path` | Base SQLite de reservas y subidas (no se versiona). | `data/publicaciones.sqlite3` |

La privacidad sale de `subida_youtube.privacidad`. Si falta o tiene un valor raro,
se sube como `private`.

Las reservas se guardan en SQLite con transacciones, así que dos procesos a la vez
no se pasan del cupo y el estado sobrevive a un reinicio. Si la subida falla, la
reserva se libera y no gasta cupo. Un clip (o el mismo MP4) que ya está subido o
reservado no se vuelve a subir. Si el proceso muere a mitad de una subida, la
reserva sigue contando porque no se sabe si el vídeo llegó a YouTube; se ve y se
libera a mano:

```powershell
.\.venv\Scripts\python.exe publication_guard.py --estado
.\.venv\Scripts\python.exe publication_guard.py --liberar 12
```

### Bloque `rights`: política de derechos

**Por defecto no se publica nada.** `unknown_permission_policy` es `"block"` y
`rights.streamers` empieza vacío, así que todos los streamers tienen el permiso en
`unknown` y sus clips se bloquean antes de descargarlos. Mientras no añadas
streamers como `allowed`, cada ejecución termina sin Shorts (y `run_clipazos.bat`
devuelve error, como cuando no hay clips).

Para dar permiso a un streamer (solo si de verdad lo tienes):

```json
"streamers": {
  "twitch:auronplay": {"estado": "allowed", "nota": "permiso por email del 10/11/2026"},
  "kick:westcol": "allowed",
  "elzeein": "denied"
}
```

- Clave `"plataforma:nombre"` (solo esa plataforma) o `"nombre"` (cualquiera).
- Estados: `allowed`, `unknown`, `denied`.
- `blocked_streamers`, `allowed_sources` y `blocked_sources`: vetos directos.
- `blocked_terms` y `blocked_patterns` (regex): bloquean por título o juego. Un
  patrón mal escrito bloquea por seguridad.
- `third_party_risk_terms`: señales de contenido de terceros (música, fútbol,
  películas, reacciones...). Con `third_party_risk_policy: "block"` bloquean; con
  `"warn"` solo avisan.

Acreditar al streamer en la descripción no es lo mismo que tener permiso, y
nada de esto evita un Content ID o un strike.

### Bloque `qa`

Bloquea la subida si falla algo, o si FFmpeg, ffprobe o YuNet no responden:

- resolución `ancho`x`alto` (1080x1920) y `fps` (24 ± `fps_tolerancia`);
- duración entre `duracion_min` y `duracion_max` segundos;
- audio presente y sonoridad integrada entre `lufs_min` y `lufs_max` (EBU R128). El
  montaje normaliza a -14 LUFS, así que el margen por defecto es de -18 a -10;
- tramos negros de `negro_max_segundos` o más (blackdetect) y congelados de
  `congelado_max_segundos` o más (freezedetect);
- subtítulos: el `.ass` del Short existe y tiene al menos `subtitulos_min_eventos`
  líneas;
- caras con YuNet (`caras.activa`, apagado por defecto).

Un `clipazos.json` antiguo sin estos bloques sigue funcionando: cada clave que
falta toma un valor seguro.

## Pruebas

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q tests
```

Usan vídeos sintéticos generados con FFmpeg y una API de YouTube simulada: no
descargan clips, no necesitan credenciales y no publican nada.

## Limitaciones

- Los permisos de `rights.streamers` los pones tú; el programa no verifica
  licencias ni detecta música con derechos dentro del audio.
- El QA mira la técnica del vídeo, no si el contenido es bueno.
- La hora de Madrid se calcula con `zoneinfo`; en Windows sin el paquete `tzdata`
  se usa la regla de horario de verano de la UE.
- El kill switch frena las subidas nuevas, pero no cancela una que ya está en curso.

## Subida a YouTube

La subida está en `youtube_upload.py` y se controla con `subida_youtube` en
`clipazos.json` (`activa` y `privacidad`).

1. Crea en Google Cloud un cliente OAuth de tipo "App de escritorio" y guarda el
   JSON como `credenciales\client_secret.json`.
2. Ejecuta `autorizar_youtube.bat` una vez. El token queda en
   `credenciales\youtube_token.json`.

## Credenciales

La carpeta `credenciales/` (con `client_secret.json` y `youtube_token.json`) y
cualquier `.env` están en `.gitignore` y **no se suben nunca** a git.
