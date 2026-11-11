"""Transcripción con tiempos por palabra.

La pista de hotwords de streamers quedó en el archivo. Aquí la llamada a
faster-whisper solo pide idioma y marcas de palabra, que es lo que también
puede aportar Edge TTS con WordBoundary sin pasar por este módulo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from video_editor import CaptionCue, _ffmpeg_exe, sync_cues

_MODELO = None
_MODELO_NOMBRE = ""


def opciones_transcripcion(idioma: str) -> dict:
    """Argumentos de ``WhisperModel.transcribe``. No envía hotwords ni prefix."""
    return {"language": idioma, "word_timestamps": True, "vad_filter": True}


def _whisper(modelo: str = "small"):
    """Carga el modelo una vez. La importación de faster-whisper es perezosa."""
    global _MODELO, _MODELO_NOMBRE
    if _MODELO is None or _MODELO_NOMBRE != modelo:
        from faster_whisper import WhisperModel

        _MODELO = WhisperModel(modelo, device="cpu", compute_type="int8")
        _MODELO_NOMBRE = modelo
    return _MODELO


def transcribir(audio, idioma: str = "es", *, modelo: str = "small") -> list[CaptionCue]:
    """Devuelve una pista de palabras sincronizadas a partir de audio mono float32."""
    segmentos, _info = _whisper(modelo).transcribe(audio, **opciones_transcripcion(idioma))
    return sync_cues(
        [
            CaptionCue(word.word.strip(), float(word.start), float(word.end))
            for segmento in segmentos
            for word in (segmento.words or [])
            if getattr(word, "word", "").strip()
        ]
    )


def cargar_audio(ruta: Path, frecuencia: int = 16_000):
    """PCM mono float32. Hace falta FFmpeg; las pruebas unitarias no lo llaman."""
    import numpy as np

    raw = subprocess.run(
        [
            _ffmpeg_exe(), "-v", "error", "-i", str(ruta),
            "-ac", "1", "-ar", str(frecuencia), "-f", "s16le", "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
