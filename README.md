# Clipazos ES

Bot local que monta Shorts verticales en español con los mejores clips de
streamers de Twitch y Kick y de youtubers. Busca los clips, los transcribe,
encuadra la webcam del streamer, quema subtítulos palabra a palabra y, si está
activado, sube el resultado a YouTube Shorts.

## Archivos

- `clipazos.py`: flujo diario. Localiza clips (Twitch, Kick y YouTube con `yt-dlp`),
  transcribe con `faster-whisper`, detecta la cara con el modelo YuNet de
  `assets/models/` y monta el Short con FFmpeg.
- `semanal.py`: resumen semanal largo de los streamers configurados.
- `video_editor.py`: utilidades comunes de subtítulos ASS y FFmpeg.
- `youtube_upload.py`: subida a YouTube con la API oficial (OAuth).
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
.\.venv\Scripts\python.exe -c "import clipazos, semanal, youtube_upload"
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
