"""Búsqueda de streamers fijos e históricos, sin red y sin credenciales."""

from __future__ import annotations

import json
import shutil
import urllib.error
from datetime import datetime, timedelta

import pytest

import clipazos
import publication_guard
from conftest import ROOT


def nodo(
    slug,
    views=1000,
    duration=20,
    language="ES",
    login="ibai",
    offset=12,
    vod="99",
    title="Clip",
    with_language=True,
):
    data = {
        "slug": slug,
        "title": title,
        "viewCount": views,
        "durationSeconds": duration,
        "createdAt": "2020-05-01T00:00:00Z",
        "broadcaster": {"login": login, "displayName": login},
        "game": {"name": "Just Chatting"},
    }
    if with_language:
        data["language"] = language
    if vod is not None:
        data["video"] = {"id": vod}
        if offset is not None:
            data["videoOffsetSeconds"] = offset
    return data


def respuesta(edges):
    return {"user": {"login": "ibai", "clips": {"edges": [{"node": item} for item in edges]}}}


def settings(**extra):
    base = {
        "streamers_fijos": ["Ibai", "@ibai"],
        "recientes": {"periodos": ["LAST_DAY", "LAST_WEEK"], "por_streamer": 5, "vistas_minimas": 100},
        "historicos": {"activo": True, "periodo": "ALL_TIME", "por_streamer": 3, "vistas_minimas": 1000},
        "min_clips": 2,
        "min_clips_fijos": 1,
        "helix": {"activa": False},
    }
    base.update(extra)
    return base


def candidato(name, slug, views, historico=False, login=None, vod=None, offset=None):
    item = clipazos.Candidate(
        "Twitch", name, slug, views, f"https://clips.twitch.tv/{slug}", f"twitch-{slug}",
        historico=historico, login=login or name.casefold(),
    )
    item.vod_id = vod
    item.offset = offset
    return item


def test_config_real_tiene_logins_comprobados_y_flags_apagados():
    config = json.loads((ROOT / "clipazos.json").read_text(encoding="utf-8"))
    logins = config["twitch"]["streamers_fijos"]
    for login in (
        "auronplay", "rubius", "thegrefg", "vegetta777", "ibai", "juansguarnizo",
        "rivers_gg", "elmariana", "spreen", "quackity", "coscu", "carreraaa", "robleis",
    ):
        assert login in logins
    # elrubius existe en Twitch pero la GQL no le devuelve clips. El canal es rubius.
    assert "elrubius" not in logins
    assert config["twitch"]["historicos"] == {
        "activo": True, "periodo": "ALL_TIME", "por_streamer": 4, "vistas_minimas": 50000,
    }
    assert config["twitch"]["min_clips"] == 2
    assert config["twitch"]["min_clips_fijos"] == 1
    assert config["twitch"]["helix"]["activa"] is False
    assert config["asr"]["hotwords_activa"] is False
    assert config["rights"]["unknown_permission_policy"] == "block"


def test_sin_logins_no_consulta(monkeypatch):
    monkeypatch.setattr(clipazos, "_gql", lambda *a, **k: (_ for _ in ()).throw(AssertionError("red")))
    assert clipazos.scout_twitch_streamers({}) == []
    assert clipazos.scout_twitch_helix({}, "ibai", "ALL_TIME", 4) is None


def test_scout_recientes_historicos_idioma_y_dedupe(monkeypatch):
    queries = []

    def fake(query):
        queries.append(query)
        if "period:ALL_TIME" in query:
            return respuesta([
                nodo("Viejo", views=200_000, offset=1000, vod="vod-9"),
                nodo("Poco", views=20, offset=400, vod="vod-1"),
                nodo("Corto", views=999_999, duration=4, offset=1, vod="vod-8"),
                nodo("Ingles", views=300_000, language="EN", offset=80, vod="vod-2"),
                nodo("SinIdioma", views=150_000, with_language=False, offset=90, vod="vod-3"),
                nodo("Mismo", views=180_000, offset=12, vod="vod-1"),
            ])
        if "period:LAST_WEEK" in query:
            return respuesta([
                nodo("Hoy", views=5_000, offset=10, vod="vod-1", title="de esta semana"),
                nodo("Semana", views=4_000, offset=500, vod="vod-4"),
            ])
        if "period:LAST_DAY" in query:
            return respuesta([nodo("Hoy", views=8_000, offset=10, vod="vod-1", title="de hoy")])
        return {"user": None}

    monkeypatch.setattr(clipazos, "_gql", fake)
    found = clipazos.scout_twitch_streamers(settings(), "es")
    by_slug = {clipazos._clip_slug(item): item for item in found}
    assert by_slug["Hoy"].historico is False and by_slug["Hoy"].title == "de hoy"
    assert by_slug["Semana"].historico is False
    assert by_slug["Viejo"].historico is True and by_slug["Viejo"].vod_id == "vod-9"
    assert by_slug["SinIdioma"].historico is True
    assert "Poco" not in by_slug and "Corto" not in by_slug and "Ingles" not in by_slug
    # Hoy y Viejo/Mismo arrancan a menos de 5 s en el mismo VOD: queda el reciente.
    assert "Mismo" not in by_slug
    assert len([item for item in found if item.vod_id == "vod-1"]) == 1
    assert all("user(login:" in query and "criteria:{period:" in query and "languages:" not in query for query in queries)
    assert sum("period:LAST_DAY" in query for query in queries) == 1
    assert any("first:5" in query for query in queries)
    assert any("first:3" in query and "period:ALL_TIME" in query for query in queries)
    assert by_slug["Viejo"].login == "ibai"


def test_login_inexistente_se_salta_y_el_otro_sigue(monkeypatch, capsys):
    def fake(query):
        if "fantasma" in query:
            return {"user": None}
        return respuesta([nodo("Real", views=3000, login="ibai")])

    monkeypatch.setattr(clipazos, "_gql", fake)
    found = clipazos.scout_twitch_streamers(
        settings(streamers_fijos=["fantasma", "ibai"], historicos={"activo": False}), "es"
    )
    err = capsys.readouterr().err
    assert "fantasma" in err and "no existe" in err
    assert {clipazos._clip_slug(item) for item in found} == {"Real"}
    assert all(not item.historico for item in found)


def test_429_guarda_lo_reunido_y_helix_apagado_no_pide_claves(monkeypatch, capsys):
    def fake(query):
        if "ibai" in query and "LAST_DAY" in query:
            return respuesta([nodo("Salvado", views=3000)])
        raise urllib.error.HTTPError("https://gql.twitch.tv/gql", 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(clipazos, "_gql", fake)
    found = clipazos.scout_twitch_streamers(settings(), "es")
    err = capsys.readouterr().err
    assert [clipazos._clip_slug(item) for item in found] == ["Salvado"]
    assert "429" in err and "helix.activa = false" in err
    assert "credencial" not in err.lower() and "token" not in err.lower()


def test_429_con_helix_encendido_avisa_que_no_esta_implementado(monkeypatch, capsys):
    monkeypatch.setattr(
        clipazos, "_gql",
        lambda query: (_ for _ in ()).throw(
            urllib.error.HTTPError("https://gql.twitch.tv/gql", 429, "Too Many Requests", {}, None)
        ),
    )
    found = clipazos.scout_twitch_streamers(settings(helix={"activa": True}), "es")
    err = capsys.readouterr().err
    assert found == []
    assert "no está implementado" in err
    assert "credencial" not in err.lower()


def test_error_de_gql_no_aborta_al_siguiente_login(monkeypatch, capsys):
    def fake(query):
        if "roto" in query:
            raise RuntimeError("caida")
        return respuesta([nodo("Bien", views=2500, login="ibai")])

    monkeypatch.setattr(clipazos, "_gql", fake)
    found = clipazos.scout_twitch_streamers(
        settings(streamers_fijos=["roto", "ibai"], historicos={"activo": False, "periodo": "LAST_WEEK"}),
        "es",
    )
    assert {clipazos._clip_slug(item) for item in found} == {"Bien"}
    assert "roto" in capsys.readouterr().err
    # LAST_WEEK no es un periodo histórico: se usa ALL_TIME, y aquí históricos van apagados.
    assert clipazos.twitch_streamer_plan({"historicos": {"activo": True, "periodo": "LAST_WEEK"}})["historical_period"] == "ALL_TIME"


def test_merge_prefiere_el_reciente_del_mismo_slug():
    reciente = candidato("Ibai", "Mismo", 10, historico=False)
    viejo = candidato("Ibai", "Mismo", 999_999, historico=True, vod="v", offset=4)
    otro = candidato("Ibai", "Otro", 50, historico=True, vod="v", offset=4)
    merged = clipazos.merge_twitch_candidates([reciente], [viejo, otro])
    # Mismo slug: gana el reciente y hereda el VOD. Mismo arranque que Otro: se queda uno.
    assert [clipazos._clip_slug(item) for item in merged] == ["Mismo"]
    assert merged[0].historico is False and merged[0].vod_id == "v"
    reciente = candidato("Ibai", "Mismo", 10, historico=False)
    viejo = candidato("Ibai", "Mismo", 999_999, historico=True, vod="v", offset=4)
    lejos = candidato("Ibai", "Lejos", 50, historico=True, vod="v", offset=80)
    merged = clipazos.merge_twitch_candidates([reciente], [viejo, lejos])
    by_slug = {clipazos._clip_slug(item): item for item in merged}
    assert by_slug["Mismo"].historico is False and by_slug["Mismo"].vod_id == "v"
    assert by_slug["Lejos"].historico is True


def test_no_repite_clips_publicados_aunque_sean_antiguos(tmp_path, monkeypatch):
    monkeypatch.setattr(clipazos, "CLIPAZOS_DIR", tmp_path)
    config = {
        "publication": {
            "db_path": str(tmp_path / "pub.sqlite3"),
            "max_por_dia": 5,
            "max_por_ejecucion": 5,
            "kill_switch_file": str(tmp_path / "STOP"),
        }
    }
    guard = publication_guard.PublicationGuard(config)
    decision = guard.reserve_publication({"streamer": "Ibai", "clips": ["twitch-EnBase"]})
    assert decision.allowed
    guard.confirm_publication(decision.reservation_id, "yt1")
    (tmp_path / "reporte_20200101.json").write_text(json.dumps({
        "shorts": [{"clips": ["twitch-ConYoutube"], "youtube_id": "abc", "streamer": "Ibai"},
                   {"clips": ["twitch-Olvidado"], "streamer": "Auron"}]
    }), encoding="utf-8")
    ayer = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    (tmp_path / f"reporte_{ayer}.json").write_text(json.dumps({
        "shorts": [{"clips": ["twitch-Ayer"], "streamer": "Auron"}]
    }), encoding="utf-8")
    (tmp_path / f"reporte_{ayer}_dryrun.json").write_text(json.dumps({
        "shorts": [{"clips": ["twitch-Dry"], "streamer": "Auron"}]
    }), encoding="utf-8")
    today = datetime.now().strftime("%Y%m%d")
    used = clipazos.clips_ya_publicados(today, 2, config)
    assert {"twitch-enbase", "twitch-conyoutube", "twitch-ayer"} <= used
    assert "twitch-olvidado" not in used and "twitch-dry" not in used
    pool = [
        candidato("Ibai", "EnBase", 10),
        candidato("Ibai", "Ayer", 10),
        candidato("Ibai", "Libre", 10, historico=True),
    ]
    kept = clipazos.sin_clips_publicados(pool, used)
    assert [item.key for item in kept] == ["twitch-Libre"]


def test_un_solo_clip_de_streamer_fijo_entra_y_el_historico_no_roba_el_dia():
    solo = [candidato("Ibai", "uno", 50_000, login="ibai")]
    assert clipazos.top_streamers(solo, 1, set()) == []
    picked = clipazos.top_streamers(solo, 1, set(), min_clips_por_streamer={"ibai": 1})
    assert [name for name, _pool in picked] == ["Ibai"]
    auron = [candidato("Auron", "a", 1000), candidato("Auron", "b", 800)]
    ibai = [candidato("Ibai", f"old{i}", 5_000_000, historico=True, login="ibai") for i in range(3)]
    order = [name for name, _pool in clipazos.top_streamers(auron + ibai, 2, set(), min_clips_por_streamer={"ibai": 1})]
    assert order[0] == "Auron"
    only_old = clipazos.top_streamers(ibai, 1, set(), min_clips_por_streamer={"ibai": 1})
    assert only_old and only_old[0][0] == "Ibai"


def test_el_historico_entra_en_la_ventana_del_juez(monkeypatch):
    monkeypatch.setattr(clipazos, "attach_chat", lambda _candidates: None)
    recent = [candidato("Ibai", f"r{i}", 1000 - i) for i in range(4)]
    old = [candidato("Ibai", "clasico", 9_000_000, historico=True), candidato("Ibai", "otro", 1000, historico=True)]
    ordered = clipazos.ordenar_para_short(recent + old, {"seleccion": {"candidatos_a_evaluar": 4}})
    assert [item.key for item in ordered[:4]] == ["twitch-r0", "twitch-r1", "twitch-r2", "twitch-clasico"]
    assert ordered[4].key == "twitch-r3"


def test_make_short_marca_historico_y_sigue_por_qa(tmp_path, videos, monkeypatch):
    clipazos._SELECTION_LOG.clear()
    candidate = candidato("Ibai", "Old", 100_000, historico=True)

    def fake_build(item, *args, **kwargs):
        moment = clipazos.Moment(item, videos["ok"], 0.5, 12.0, [])
        moment.segment = {"end": 12.5, "notes": []}
        moment.judgement = {"aceptado": True, "puntuacion": 0.91, "motivos": [], "criterios": {}}
        return moment

    def fake_render(_moments, title, _channel, destination):
        assert title[0][0][0] == "UN CLÁSICO DE"
        shutil.copy(videos["ok"], destination)
        shutil.copy(videos["ok"].with_suffix(".ass"), destination.with_suffix(".ass"))
        return destination

    monkeypatch.setattr(clipazos, "build_moment", fake_build)
    monkeypatch.setattr(clipazos, "render_short", fake_render)
    config = {"rights": {"streamers": {"ibai": "allowed"}}, "qa": {"duracion_min": 1}, "idioma": "es"}
    post = clipazos.make_short([candidate], config, [[("LO MEJOR DE", False)]], "Lo mejor de Ibai hoy", tmp_path / "s.mp4", [])
    assert post["historico"] is True
    assert post["titulo"] == "Un clásico de Ibai"
    assert post["fuentes"][0]["historico"] is True and post["fuentes"][0]["key"] == "twitch-Old"
    assert post["seleccion"][0]["historico"] is True and post["seleccion"][0]["aceptado"] is True
    assert post["qa"]["ok"] is True
    assert clipazos.apply_rights([candidate], {}, "Twitch") == []


def test_main_escribe_historicos_sin_descargar_ni_publicar(tmp_path, monkeypatch):
    monkeypatch.setattr(clipazos, "CLIPAZOS_DIR", tmp_path)
    monkeypatch.setattr(clipazos, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(clipazos, "RADAR_PATH", tmp_path / "radar.json")
    monkeypatch.setattr(clipazos, "_gql", lambda *a, **k: (_ for _ in ()).throw(AssertionError("gql")))
    monkeypatch.setattr(clipazos, "_gql_many", lambda *a, **k: (_ for _ in ()).throw(AssertionError("gql")))
    monkeypatch.setattr(clipazos, "fetch_media", lambda *a, **k: (_ for _ in ()).throw(AssertionError("descarga")))
    recent = candidato("Ibai", "Hoy", 100)
    repeated = candidato("Ibai", "Hoy", 100, historico=True)
    old = candidato("Ibai", "Old", 900_000, historico=True)
    published = candidato("Ibai", "Ya", 800_000, historico=True)
    monkeypatch.setattr(clipazos, "scout_twitch", lambda *a: [recent])
    monkeypatch.setattr(clipazos, "scout_twitch_streamers", lambda *a: [repeated, old, published])
    monkeypatch.setattr(clipazos, "scout_kick", lambda *a: [])
    monkeypatch.setattr(clipazos, "scout_youtube", lambda *a: [])
    config = {
        "canal": "Clipazos ES",
        "no_repetir_dias": 2,
        "shorts_por_dia": 1,
        "rights": {"unknown_permission_policy": "block"},
        "subida_youtube": {"activa": False},
        "tiktok_export": {"enabled": False},
        "publication": {"db_path": str(tmp_path / "pub.sqlite3"), "kill_switch_file": str(tmp_path / "STOP")},
        "twitch": {"streamers_fijos": ["ibai"], "min_clips_fijos": 1, "historicos": {"activo": True}},
    }
    guard = publication_guard.PublicationGuard(config)
    decision = guard.reserve_publication({"streamer": "Ibai", "clips": ["twitch-Ya"]})
    guard.confirm_publication(decision.reservation_id, "yt-ya")
    path = tmp_path / "clipazos.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    assert clipazos.main(["--config", str(path), "--dry-run"]) == 1
    report_path = tmp_path / f"reporte_{datetime.now().strftime('%Y%m%d')}_dryrun.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["shorts"] == []
    assert report["historicos"] == [{
        "key": "twitch-Old",
        "streamer": "Ibai",
        "titulo": "Old",
        "vistas": 900_000,
        "url": "https://clips.twitch.tv/Old",
        "juego": "",
        "historico": True,
    }]
    assert "twitch-Ya" not in json.dumps(report)
    assert "twitch-Hoy" not in json.dumps(report["historicos"])
