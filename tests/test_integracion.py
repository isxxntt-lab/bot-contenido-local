from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone

import pytest

import clipazos
import publication_guard as pg
import youtube_upload
from conftest import ROOT, allowed_config


def fuente(streamer="Ana", key="twitch-a"):
    return {"platform": "Twitch", "streamer": streamer, "title": "Momentazo", "game": "Just Chatting", "url": "u", "key": key}


def report(tmp_path, videos, *shorts):
    path = tmp_path / "reporte_20261109.json"
    path.write_text(json.dumps({"canal": "Clipazos ES", "shorts": list(shorts)}, ensure_ascii=False), encoding="utf-8")
    return path


def good_short(tmp_path, videos, name="a", streamer="Ana", key=None):
    target = tmp_path / f"{name}.mp4"
    shutil.copy(videos["ok"], target)
    shutil.copy(videos["ok"].with_suffix(".ass"), target.with_suffix(".ass"))
    return {"titulo": "Lo mejor", "descripcion": "Créditos", "hashtags": ["#clipazos"], "video": str(target),
            "streamer": streamer, "clips": [key or f"twitch-{name}"], "fuentes": [fuente(streamer, key or f"twitch-{name}")]}


class FakeUploader:
    def __init__(self, fail: bool = False):
        self.calls = []
        self.fail = fail

    def __call__(self, short, privacy):
        self.calls.append((short["video"], privacy))
        if self.fail:
            raise RuntimeError("quotaExceeded simulado")
        return f"vid{len(self.calls)}"


def test_imports_y_help():
    with pytest.raises(SystemExit) as exit_info:
        clipazos.parse_args(["--help"])
    assert exit_info.value.code == 0
    assert clipazos.parse_args(["--dry-run"]).dry_run is True


def test_subida_simulada_ok_y_sin_repetir(tmp_path, videos):
    config = allowed_config(tmp_path)
    path = report(tmp_path, videos, good_short(tmp_path, videos))
    fake = FakeUploader()
    assert youtube_upload.upload_report(path, "private", config=config, config_path=None, uploader=fake) == 1
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["shorts"][0]["youtube_id"] == "vid1"
    assert youtube_upload.upload_report(path, "private", config=config, uploader=fake) == 0
    assert len(fake.calls) == 1
    # El mismo clip en otro reporte (re-render) no se vuelve a subir.
    again = report(tmp_path, videos, good_short(tmp_path, videos, name="a2", key="twitch-a"))
    assert youtube_upload.upload_report(again, "private", config=config, uploader=fake) == 0
    assert "ya subido" in json.loads(again.read_text(encoding="utf-8"))["shorts"][0]["publicacion_bloqueada"]
    assert len(fake.calls) == 1


def test_fallo_de_subida_no_gasta_cupo(tmp_path, videos):
    config = allowed_config(tmp_path, max_por_dia=1)
    path = report(tmp_path, videos, good_short(tmp_path, videos))
    broken = FakeUploader(fail=True)
    assert youtube_upload.upload_report(path, "private", config=config, uploader=broken) == 0
    rows = pg.PublicationGuard(config).rows()
    assert [row["status"] for row in rows] == ["released"]
    fine = FakeUploader()
    assert youtube_upload.upload_report(path, "private", config=config, uploader=fine) == 1


def test_kill_switch_releido_antes_de_la_api(tmp_path, videos, monkeypatch):
    config = allowed_config(tmp_path)
    config_path = tmp_path / "clipazos.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    path = report(tmp_path, videos, good_short(tmp_path, videos))
    guard = pg.PublicationGuard(config, config_path)
    original = guard.kill_switch_reason
    calls = {"n": 0}

    def flip():
        # Se activa el kill switch entre la reserva y la llamada a la API.
        calls["n"] += 1
        if calls["n"] == 2:
            (tmp_path / "STOP_YOUTUBE").write_text("", encoding="utf-8")
        return original()

    monkeypatch.setattr(guard, "kill_switch_reason", flip)
    fake = FakeUploader()
    assert youtube_upload.upload_report(path, "private", config=config, config_path=config_path, guard=guard, uploader=fake) == 0
    assert fake.calls == []
    assert [row["status"] for row in guard.rows()] == ["released"]


def test_derechos_o_qa_fallidos_no_suben(tmp_path, videos):
    config = allowed_config(tmp_path)
    unknown = good_short(tmp_path, videos, name="c", streamer="Desconocido")
    no_subs = good_short(tmp_path, videos, name="d", streamer="Beto")
    # QA falla: se borra el .ass.
    __import__("pathlib").Path(no_subs["video"]).with_suffix(".ass").unlink()
    path = report(tmp_path, videos, unknown, no_subs)
    fake = FakeUploader()
    assert youtube_upload.upload_report(path, "private", config=config, uploader=fake) == 0
    assert fake.calls == []
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["shorts"][0]["publicacion_bloqueada"].startswith("derechos")
    assert data["shorts"][1]["publicacion_bloqueada"].startswith("QA")


def test_dry_run_no_llama_a_youtube_ni_escribe(tmp_path, videos):
    config = allowed_config(tmp_path)
    path = report(tmp_path, videos, good_short(tmp_path, videos))
    before = path.read_text(encoding="utf-8")
    fake = FakeUploader()
    assert youtube_upload.upload_report(path, "public", config=config, dry_run=True, uploader=fake) == 1
    assert fake.calls == []
    assert path.read_text(encoding="utf-8") == before
    assert not (tmp_path / "pub.sqlite3").exists()


def test_upload_de_clipazos_en_dry_run_no_necesita_token(tmp_path, videos, monkeypatch):
    config = allowed_config(tmp_path)
    path = report(tmp_path, videos, good_short(tmp_path, videos))
    monkeypatch.setattr(youtube_upload, "is_ready", lambda: (_ for _ in ()).throw(AssertionError("no debe mirar el token")))
    monkeypatch.setattr(youtube_upload, "upload_short", lambda *a, **k: (_ for _ in ()).throw(AssertionError("API llamada")))
    clipazos._upload(config, path, None, dry_run=True)


def test_collect_moments_no_descarga_sin_permiso(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("no debe descargar")

    monkeypatch.setattr(clipazos, "build_moment", boom)
    candidate = clipazos.Candidate("Twitch", "Nadie", "clip", 100, "u", "twitch-x")
    assert clipazos.collect_moments([candidate], {}, "es") == []
    assert clipazos.apply_rights([candidate], {}, "Twitch") == []


def test_make_short_aplica_qa(tmp_path, videos, monkeypatch):
    candidate = clipazos.Candidate("Twitch", "Ana", "clip", 100, "u", "twitch-a")
    moments = [clipazos.Moment(candidate, videos["ok"], 0, 2.0, []), clipazos.Moment(candidate, videos["ok"], 2, 2.0, [])]
    monkeypatch.setattr(clipazos, "collect_moments", lambda *a, **k: moments)

    def fake_render(_moments, _title, _channel, destination):
        shutil.copy(videos["horizontal"], destination)
        shutil.copy(videos["ok"].with_suffix(".ass"), destination.with_suffix(".ass"))
        return destination

    monkeypatch.setattr(clipazos, "render_short", fake_render)
    post = clipazos.make_short([candidate], {"qa": {"duracion_min": 1}}, [[("X", False)]], "Titular", tmp_path / "s.mp4", [])
    assert post["no_subir"] is True and post["bloqueado_por"] == "qa"
    assert post["clips"] == ["twitch-a"] and post["fuentes"][0]["streamer"] == "Ana"


def test_main_dry_run_sin_red(tmp_path, monkeypatch):
    monkeypatch.setattr(clipazos, "CLIPAZOS_DIR", tmp_path)
    monkeypatch.setattr(clipazos, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(clipazos, "RADAR_PATH", tmp_path / "radar.json")
    candidate = clipazos.Candidate("Twitch", "Nadie", "clip", 100, "u", "twitch-x")
    monkeypatch.setattr(clipazos, "scout_twitch", lambda *a: [candidate, candidate])
    monkeypatch.setattr(clipazos, "scout_kick", lambda *a: [])
    monkeypatch.setattr(clipazos, "scout_youtube", lambda *a: [])
    monkeypatch.setattr(clipazos, "fetch_media", lambda *a: (_ for _ in ()).throw(AssertionError("descarga")))
    config_path = tmp_path / "clipazos.json"
    config_path.write_text(json.dumps({"canal": "Clipazos ES"}), encoding="utf-8")
    assert clipazos.main(["--config", str(config_path), "--dry-run"]) == 1
    assert (tmp_path / f"reporte_{datetime.now().strftime('%Y%m%d')}_dryrun.json").is_file()
    assert clipazos.recently_posted("20991231", 99999) == set()


def test_config_antigua_sigue_funcionando(tmp_path, videos):
    old = {"canal": "Clipazos ES", "subida_youtube": {"activa": True}}
    assert pg.resolve_privacy(old) == "private"
    guard = pg.PublicationGuard(old, dry_run=True)
    assert guard.current_settings()[0]["max_por_dia"] == 1
    path = report(tmp_path, videos, good_short(tmp_path, videos))
    fake = FakeUploader()
    # Sin bloque rights: permiso desconocido → bloqueado.
    assert youtube_upload.upload_report(path, config=old, dry_run=True, uploader=fake) == 0
