"""Juez local de momentos: puntúa si un tramo tiene un arco completo.

Versión simple, sin LLM ni red. Cada criterio da de 0 a 1 punto con un motivo
legible; la puntuación final es la media ponderada. Un momento se descarta si
falla un criterio obligatorio (por defecto inicio, final, remate y duración) o
si no llega a ``puntuacion_min``.

Criterios:
- hook: hay voz desde el primer segundo y varias palabras en los primeros
  ``hook_segundos``.
- inicio_completo: el tramo empieza al inicio de una frase (no con la voz del
  clip ya en marcha).
- final_completo: el tramo acaba al cerrar una frase (puntuación o pausa), no
  cortado por el final del clip.
- remate: tras el pico quedan al menos ``remate_min_segundos`` y alguna palabra.
- autocontencion: no empieza con un conector («y», «pero», «porque»...), no
  acaba colgando («de», «que», «el»...) y tiene al menos ``min_palabras``.
- duracion: la duración final está entre seleccion.segundos_min y segundos_max.

Es una heurística sobre la transcripción: no entiende el chiste ni el contexto.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import narrative_segments

WEIGHTS = {
    "hook": 0.2,
    "inicio_completo": 0.2,
    "final_completo": 0.2,
    "remate": 0.2,
    "autocontencion": 0.1,
    "duracion": 0.1,
}

DEFAULTS: dict = {
    "puntuacion_min": 0.6,
    "hook_segundos": 3.0,
    "hook_palabras_min": 3,
    "remate_min_segundos": 1.0,
    "min_palabras": 6,
    "obligatorios": ["inicio_completo", "final_completo", "remate", "duracion"],
}

START_CONNECTORS = {
    "y", "e", "pero", "porque", "pues", "entonces", "aunque", "además", "ademas",
    "también", "tambien", "osea", "sino", "que", "o",
}
DANGLING_ENDS = {
    "y", "e", "o", "u", "pero", "porque", "que", "de", "del", "a", "al", "en", "con",
    "por", "para", "el", "la", "los", "las", "un", "una", "mi", "tu", "su", "le", "lo",
    "se", "me", "te", "si", "como", "cuando", "donde",
}


def settings_from(config: dict | None) -> dict:
    """Sub-bloque "seleccion.juez" con valores seguros."""
    selection = (config or {}).get("seleccion")
    raw = selection.get("juez") if isinstance(selection, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    settings = dict(DEFAULTS)
    for key in ("puntuacion_min", "hook_segundos", "hook_palabras_min", "remate_min_segundos", "min_palabras"):
        value = raw.get(key, DEFAULTS[key])
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            value = DEFAULTS[key]
        settings[key] = value
    required = raw.get("obligatorios", DEFAULTS["obligatorios"])
    if not isinstance(required, list):
        required = DEFAULTS["obligatorios"]
    settings["obligatorios"] = [name for name in required if name in WEIGHTS]
    return settings


@dataclass
class Judgement:
    accepted: bool
    score: float
    criteria: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "aceptado": self.accepted,
            "puntuacion": round(self.score, 3),
            "criterios": self.criteria,
            "motivos": self.reasons,
        }

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def _plain(word: str) -> str:
    return re.sub(r"[^\wáéíóúñü]", "", word, flags=re.IGNORECASE).casefold()


def judge(
    words: list,
    segment: narrative_segments.Segment,
    length: float | None = None,
    config: dict | None = None,
) -> Judgement:
    """Puntúa un tramo. ``words`` es la transcripción del clip entero (tiempos del clip).

    ``length`` es la duración final tras quitar silencios; si falta se usa la del tramo.
    """
    s = settings_from(config)
    selection = narrative_segments.settings_from(config)
    inside = [word for word in words if segment.start <= word.start < segment.end]
    duration = float(length if length is not None else segment.length)
    criteria: dict[str, dict] = {}

    def put(name: str, points: float, reason: str) -> None:
        criteria[name] = {"puntos": round(max(0.0, min(1.0, points)), 3), "ok": points >= 1.0 - 1e-9, "motivo": reason}

    # hook
    hook_end = segment.start + float(s["hook_segundos"])
    early = [word for word in inside if word.start < hook_end]
    first_gap = (inside[0].start - segment.start) if inside else None
    voice_now = first_gap is not None and first_gap <= 1.0
    enough = len(early) >= int(s["hook_palabras_min"])
    put(
        "hook",
        (0.5 if voice_now else 0.0) + (0.5 if enough else 0.0),
        f"{len(early)} palabras en los primeros {s['hook_segundos']:.0f} s"
        + (f", la voz entra a {first_gap:.1f} s" if first_gap is not None else ", sin voz"),
    )
    # inicio y final
    put(
        "inicio_completo",
        1.0 if inside and segment.complete_start else 0.0,
        "empieza al inicio de una frase" if inside and segment.complete_start else "empieza a mitad de frase o sin voz",
    )
    put(
        "final_completo",
        1.0 if inside and segment.complete_end else 0.0,
        "acaba al cerrar una frase" if inside and segment.complete_end else "acaba a mitad de frase (cortado)",
    )
    # remate
    after = segment.end - segment.peak
    words_after = [word for word in inside if word.start >= segment.peak]
    payoff = after >= float(s["remate_min_segundos"]) and bool(words_after)
    put(
        "remate",
        1.0 if payoff else (0.5 if after >= float(s["remate_min_segundos"]) else 0.0),
        f"{after:.1f} s y {len(words_after)} palabras tras el pico" if payoff
        else f"acaba en el pico: {after:.1f} s y {len(words_after)} palabras después",
    )
    # autocontención
    points = 1.0
    notes: list[str] = []
    if inside and _plain(inside[0].text) in START_CONNECTORS:
        points -= 0.5
        notes.append(f"empieza con «{inside[0].text.strip()}»")
    if inside and _plain(inside[-1].text) in DANGLING_ENDS and not inside[-1].text.rstrip().endswith(narrative_segments.SENTENCE_END):
        points -= 0.5
        notes.append(f"acaba colgando en «{inside[-1].text.strip()}»")
    if len(inside) < int(s["min_palabras"]):
        points = 0.0
        notes.append(f"solo {len(inside)} palabras")
    if segment.crosses_units:
        points -= 0.25
        notes.append("mezcla dos intervenciones")
    put("autocontencion", points, "; ".join(notes) or "se entiende solo")
    # duración
    ok_length = selection["segundos_min"] <= duration <= selection["segundos_max"]
    put(
        "duracion",
        1.0 if ok_length else 0.0,
        f"{duration:.1f} s (rango {selection['segundos_min']:.0f}-{selection['segundos_max']:.0f} s)",
    )

    score = sum(WEIGHTS[name] * criteria[name]["puntos"] for name in WEIGHTS) / sum(WEIGHTS.values())
    reasons = [f"{name}: {criteria[name]['motivo']}" for name in s["obligatorios"] if not criteria[name]["ok"]]
    if score < float(s["puntuacion_min"]):
        reasons.append(f"puntuación {score:.2f} < {float(s['puntuacion_min']):.2f}")
    return Judgement(not reasons, score, criteria, reasons)
