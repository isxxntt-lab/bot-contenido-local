"""Política de derechos de Clipazos ES: qué clips se pueden descargar, editar y subir.

Se aplica antes de descargar o editar un clip (clipazos.py) y otra vez justo
antes de subir (youtube_upload.py). Config en el bloque "rights" de clipazos.json:

- allowed_sources / blocked_sources: plataformas permitidas o vetadas.
- blocked_streamers: streamers que nunca entran.
- streamers: permiso por streamer, con estado "allowed", "unknown" o "denied".
  Clave "nombre" (cualquier plataforma) o "plataforma:nombre" (solo esa).
- unknown_permission_policy: qué hacer con un streamer sin permiso apuntado.
  Por defecto "block": sin permiso conocido, no se publica.
- blocked_terms / blocked_patterns: palabras o regex en título o juego que bloquean.
- third_party_risk_terms: señales de contenido de terceros (música, fútbol,
  películas...). Con third_party_risk_policy "block" (por defecto) bloquean.

Añadir un streamer con permiso (ejemplo):
    "streamers": {
      "twitch:auronplay": {"estado": "allowed", "nota": "permiso por email del 10/11/2026"}
    }

Acreditar al streamer no es lo mismo que tener permiso.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

STATUSES = ("allowed", "unknown", "denied")

DEFAULT_THIRD_PARTY_TERMS = [
    "música", "canción", "concierto", "videoclip", "película", "pelicula", "serie",
    "trailer", "tráiler", "partido", "fútbol", "futbol", "champions", "laliga",
    "la liga", "mundial", "dazn", "netflix", "hbo", "disney", "anime", "dmca",
    "copyright", "reacciona", "reaccionando", "reacción a", "watch party",
]
DEFAULT_BLOCKED_TERMS = ["casino", "apuestas", "slots", "nsfw", "+18"]

DEFAULTS: dict = {
    "unknown_permission_policy": "block",
    "allowed_sources": ["Twitch", "Kick", "YouTube"],
    "blocked_sources": [],
    "blocked_streamers": [],
    "streamers": {},
    "blocked_terms": DEFAULT_BLOCKED_TERMS,
    "blocked_patterns": [],
    "third_party_risk_terms": DEFAULT_THIRD_PARTY_TERMS,
    "third_party_risk_policy": "block",
}


@dataclass
class RightsDecision:
    allowed: bool
    status: str
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"permitido": self.allowed, "estado": self.status, "motivos": self.reasons, "avisos": self.warnings}

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def _fold(text: str) -> str:
    """Minúsculas y sin tildes, para comparar «Música» con «musica»."""
    text = unicodedata.normalize("NFKD", str(text or "")).casefold()
    return "".join(char for char in text if not unicodedata.combining(char)).strip()


def _list(raw: dict, key: str) -> list[str]:
    value = raw.get(key, DEFAULTS[key])
    if not isinstance(value, list):
        value = DEFAULTS[key]
    return [str(item) for item in value if str(item).strip()]


def settings_from(config: dict | None) -> dict:
    raw = (config or {}).get("rights")
    raw = raw if isinstance(raw, dict) else {}
    settings = {key: _list(raw, key) for key in (
        "allowed_sources", "blocked_sources", "blocked_streamers",
        "blocked_terms", "blocked_patterns", "third_party_risk_terms",
    )}
    policy = str(raw.get("unknown_permission_policy", "block")).strip().lower()
    settings["unknown_permission_policy"] = policy if policy in ("block", "allow") else "block"
    risk = str(raw.get("third_party_risk_policy", "block")).strip().lower()
    settings["third_party_risk_policy"] = risk if risk in ("block", "warn") else "block"
    streamers = raw.get("streamers", {})
    settings["streamers"] = streamers if isinstance(streamers, dict) else {}
    return settings


def _field(item, name: str, default=""):
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def source_info(candidate) -> dict:
    """Datos del clip que se guardan en el reporte para volver a comprobar antes de subir."""
    return {
        "platform": _field(candidate, "platform"),
        "streamer": _field(candidate, "streamer"),
        "title": _field(candidate, "title"),
        "game": _field(candidate, "game") or "",
        "url": _field(candidate, "url"),
        "key": _field(candidate, "key"),
    }


def streamer_status(settings: dict, platform: str, streamer: str) -> str:
    name = _fold(streamer).lstrip("@")
    plat = _fold(platform)
    if name in {_fold(item).lstrip("@") for item in settings["blocked_streamers"]}:
        return "denied"
    found = None
    for key, value in settings["streamers"].items():
        key_folded = _fold(key).lstrip("@")
        if ":" in key_folded:
            key_platform, key_name = key_folded.split(":", 1)
            if key_platform.strip() == plat and key_name.strip().lstrip("@") == name:
                found = value
                break  # la clave con plataforma manda
        elif key_folded == name and found is None:
            found = value
    if found is None:
        return "unknown"
    state = found.get("estado", found.get("status")) if isinstance(found, dict) else found
    state = _fold(state or "unknown")
    return state if state in STATUSES else "unknown"


def _term_hit(term: str, text: str) -> bool:
    folded = _fold(term)
    if not folded:
        return False
    if re.fullmatch(r"[\w ]+", folded):
        return re.search(rf"(?<!\w){re.escape(folded)}(?!\w)", text) is not None
    return folded in text


def check_candidate(candidate, config: dict | None) -> RightsDecision:
    """Decide si un clip se puede usar. Acepta un Candidate o un dict de fuente."""
    settings = settings_from(config)
    platform = str(_field(candidate, "platform") or "")
    streamer = str(_field(candidate, "streamer") or "")
    text = _fold(" ".join(str(_field(candidate, name) or "") for name in ("title", "game")))
    reasons: list[str] = []
    warnings: list[str] = []

    allowed_sources = {_fold(item) for item in settings["allowed_sources"]}
    if _fold(platform) in {_fold(item) for item in settings["blocked_sources"]}:
        reasons.append(f"fuente bloqueada: {platform}")
    elif _fold(platform) not in allowed_sources:
        reasons.append(f"fuente no permitida: {platform or 'desconocida'}")

    if not streamer.strip():
        status = "unknown"
        reasons.append("clip sin streamer identificado")
    else:
        status = streamer_status(settings, platform, streamer)
        if status == "denied":
            reasons.append(f"@{streamer}: permiso denegado o streamer bloqueado")
        elif status == "unknown" and settings["unknown_permission_policy"] == "block":
            reasons.append(f"@{streamer}: sin permiso conocido (unknown_permission_policy = block)")
        elif status == "unknown":
            warnings.append(f"@{streamer}: permiso desconocido, se deja pasar por unknown_permission_policy = allow")

    for term in settings["blocked_terms"]:
        if _term_hit(term, text):
            reasons.append(f"término bloqueado: {term}")
    for pattern in settings["blocked_patterns"]:
        try:
            if re.search(pattern, text, re.IGNORECASE) or re.search(
                pattern, " ".join(str(_field(candidate, n) or "") for n in ("title", "game")), re.IGNORECASE
            ):
                reasons.append(f"patrón bloqueado: {pattern}")
        except re.error as exc:
            reasons.append(f"patrón inválido en rights.blocked_patterns ({pattern!r}: {exc}); se bloquea por seguridad")
    risky = [term for term in settings["third_party_risk_terms"] if _term_hit(term, text)]
    if risky:
        message = f"posible contenido de terceros: {', '.join(risky)}"
        if settings["third_party_risk_policy"] == "block":
            reasons.append(message)
        else:
            warnings.append(message)
    return RightsDecision(not reasons, status, reasons, warnings)


def filter_candidates(candidates: list, config: dict | None) -> tuple[list, list[tuple[object, RightsDecision]]]:
    allowed: list = []
    blocked: list[tuple[object, RightsDecision]] = []
    for candidate in candidates:
        decision = check_candidate(candidate, config)
        if decision.allowed:
            allowed.append(candidate)
        else:
            blocked.append((candidate, decision))
    return allowed, blocked


def check_short(short: dict, config: dict | None) -> RightsDecision:
    """Comprobación final de un Short del reporte: todas sus fuentes deben pasar."""
    sources = short.get("fuentes") or []
    if not sources:
        return RightsDecision(False, "unknown", ["el Short no tiene fuentes registradas; no se puede verificar el permiso"])
    reasons: list[str] = []
    warnings: list[str] = []
    statuses: list[str] = []
    for source in sources:
        decision = check_candidate(source, config)
        statuses.append(decision.status)
        reasons.extend(decision.reasons)
        warnings.extend(decision.warnings)
    status = "denied" if "denied" in statuses else "unknown" if "unknown" in statuses else "allowed"
    return RightsDecision(not reasons, status, list(dict.fromkeys(reasons)), list(dict.fromkeys(warnings)))
