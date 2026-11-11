"""Transcripciones sintéticas: palabras con tiempos, como las de faster-whisper."""

from __future__ import annotations

from video_editor import CaptionCue

WORD = 0.30
GAP = 0.05


def build(sentences: list[tuple[str, float]], start: float = 0.5) -> list[CaptionCue]:
    """[(frase, pausa_antes), ...] -> palabras con inicio y fin."""
    words: list[CaptionCue] = []
    cursor = start
    for text, pause in sentences:
        cursor += pause
        for token in text.split():
            words.append(CaptionCue(token, round(cursor, 3), round(cursor + WORD, 3)))
            cursor += WORD + GAP
    return words


def word_at(words: list[CaptionCue], text: str) -> CaptionCue:
    return next(word for word in words if word.text.strip(".,!?¡¿").casefold() == text.casefold())


# Historia con preparación, pico («¡NOOO!») y remate, separada de otra charla por 3 s.
STORY = [
    ("Vale chat os cuento lo que me ha pasado hoy.", 0.0),
    ("Estaba jugando tranquilo en la partida final.", 0.2),
    ("Y de repente aparece el jefe por detrás.", 0.2),
    ("¡NOOO!", 0.2),
    ("Me ha matado con un solo golpe increíble.", 0.2),
    ("No me lo puedo creer de verdad chat.", 0.2),
    ("Bueno vamos a mirar los subs que han llegado.", 3.0),
    ("Gracias a todos por el apoyo de hoy.", 0.2),
]
