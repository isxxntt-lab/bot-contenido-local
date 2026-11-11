from __future__ import annotations

import json

import rights
from conftest import ROOT


def clip(streamer="Ana", platform="Twitch", title="Se cae de la silla", game="Just Chatting"):
    return {"platform": platform, "streamer": streamer, "title": title, "game": game, "url": "u", "key": f"k-{streamer}"}


def test_por_defecto_bloquea_lo_desconocido():
    decision = rights.check_candidate(clip(), {})
    assert not decision.allowed and decision.status == "unknown"
    assert "sin permiso conocido" in decision.reason


def test_config_real_empieza_sin_streamers_autorizados():
    config = json.loads((ROOT / "clipazos.json").read_text(encoding="utf-8"))
    assert config["rights"]["streamers"] == {}
    assert config["rights"]["unknown_permission_policy"] == "block"
    assert not rights.check_candidate(clip(), config).allowed


def test_allowed_denied_y_clave_con_plataforma():
    config = {"rights": {"streamers": {"twitch:ana": {"estado": "allowed"}, "beto": "denied", "kick:ana": "denied"}}}
    assert rights.check_candidate(clip("Ana"), config).allowed
    assert rights.check_candidate(clip("ANA", platform="Kick"), config).status == "denied"
    assert rights.check_candidate(clip("Beto"), config).status == "denied"
    assert rights.check_candidate(clip("Carla"), config).status == "unknown"


def test_politica_allow_deja_pasar_con_aviso():
    decision = rights.check_candidate(clip(), {"rights": {"unknown_permission_policy": "allow"}})
    assert decision.allowed and decision.warnings
    assert not rights.check_candidate(clip(), {"rights": {"unknown_permission_policy": "loquesea"}}).allowed


def test_fuentes_y_streamers_bloqueados():
    base = {"streamers": {"ana": "allowed"}}
    assert not rights.check_candidate(clip(), {"rights": {**base, "blocked_streamers": ["@ana"]}}).allowed
    assert not rights.check_candidate(clip(), {"rights": {**base, "blocked_sources": ["twitch"]}}).allowed
    assert not rights.check_candidate(clip(platform="TikTok"), {"rights": base}).allowed
    assert not rights.check_candidate(clip(), {"rights": {**base, "allowed_sources": ["Kick"]}}).allowed


def test_terminos_patrones_y_terceros():
    config = {"rights": {"streamers": {"ana": "allowed"}, "blocked_patterns": [r"\bsorteo\b"]}}
    assert not rights.check_candidate(clip(title="Gané en el CASINO"), config).allowed
    assert not rights.check_candidate(clip(title="Mega sorteo hoy"), config).allowed
    risky = rights.check_candidate(clip(title="Reacciona al partido del Madrid"), config)
    assert not risky.allowed and "terceros" in risky.reason
    assert rights.check_candidate(
        clip(title="Música de fondo"), {"rights": {"streamers": {"ana": "allowed"}, "third_party_risk_policy": "warn"}}
    ).allowed
    assert rights.check_candidate(clip(title="Una seriedad total"), config).allowed  # «serie» solo como palabra
    broken = rights.check_candidate(clip(), {"rights": {"streamers": {"ana": "allowed"}, "blocked_patterns": ["("]}})
    assert not broken.allowed and "inválido" in broken.reason


def test_check_short_exige_fuentes():
    config = {"rights": {"streamers": {"ana": "allowed"}}}
    assert not rights.check_short({"video": "x.mp4"}, config).allowed
    assert rights.check_short({"fuentes": [clip()]}, config).allowed
    assert not rights.check_short({"fuentes": [clip(), clip("Beto")]}, config).allowed
