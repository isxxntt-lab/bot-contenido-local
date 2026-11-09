"""El modo privado no publica, no toca el registro y no rebaja el juez."""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
from pathlib import Path

import clipazos
import prueba_privada
import publication_guard
import qa
import tiktok_export
import youtube_upload
from video_editor import CaptionCue


def _boom(label: str):
    def _blocked(*_args, **_kwargs):
        raise AssertionError(f"efecto de publicación prohibido: {label}")

    return _blocked


def _tripwires(monkeypatch) -> None:
    monkeypatch.setattr(youtube_upload, "upload_report", _boom("upload_report"))
    monkeypatch.setattr(youtube_upload, "upload_short", _boom("upload_short"))
    monkeypatch.setattr(youtube_upload, "_credentials", _boom("credentials"))
    monkeypatch.setattr(publication_guard.PublicationGuard, "reserve_publication", _boom("reserve"))
    monkeypatch.setattr(publication_guard.PublicationGuard, "confirm_publication", _boom("confirm"))
    monkeypatch.setattr(publication_guard, "reserve_publication", _boom("reserve_fn"))
    monkeypatch.setattr(publication_guard, "confirm_publication", _boom("confirm_fn"))
    monkeypatch.setattr(clipazos, "_upload", _boom("clipazos_upload"))
    monkeypatch.setattr(tiktok_export, "export_short", _boom("tiktok_export"))
    real_import = __import__("builtins").__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if name.split(".")[0] in {"youtube_upload", "publication_guard", "tiktok_export"}:
            raise AssertionError(f"import activo prohibido: {name}")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", guarded)


def _candidate(title: str = "una historia") -> clipazos.Candidate:
    return clipazos.Candidate("Twitch", "Ibai", title, 1, "https://clips.twitch.tv/Abc", "twitch-Abc")


def test_el_modulo_no_importa_el_uploader():
    tree = ast.parse(Path(prueba_privada.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "youtube_upload" not in imported
    assert "publication_guard" not in imported
    assert "tiktok_export" not in imported
    assert prueba_privada.isolation_ok()
    prueba_privada.require_judge()


def test_dry_run_y_el_registro_real_siguen_bloqueando(tmp_path):
    original = clipazos.CONFIG_PATH.read_bytes()
    config = json.loads(original)
    assert config["rights"]["unknown_permission_policy"] == "block"
    assert config["rights"]["streamers"] == {}
    candidate = _candidate()
    assert clipazos.apply_rights([candidate], config, "Twitch") == []
    decision = prueba_privada.private_decision(candidate, config)
    assert decision["ensayable"] is True and decision["rights_status"] == "unknown"
    denied = prueba_privada.private_decision(
        candidate, {"rights": {"streamers": {"ibai": "denied"}, "unknown_permission_policy": "block"}}
    )
    assert denied["ensayable"] is False and denied["rights_status"] == "denied"
    casino = prueba_privada.private_decision(
        _candidate("noche de casino"),
        {"rights": {"blocked_terms": ["casino"], "unknown_permission_policy": "block", "streamers": {}}},
    )
    assert casino["ensayable"] is False
    version = prueba_privada.judge_version(config)
    assert version["puntuacion_min"] == 0.6
    assert "inicio_completo" in version["obligatorios"]
    assert version["segundos_min"] == 10 and version["segundos_max"] == 45
    assert clipazos.CONFIG_PATH.read_bytes() == original
    assert not (tmp_path / "no-se-usa").exists()


def test_lo_restringido_no_llega_al_juez(tmp_path, monkeypatch):
    called: list[int] = []
    monkeypatch.setattr(clipazos, "build_moment", lambda *_a, **_k: called.append(1))
    record = prueba_privada.evaluate_candidate(
        _candidate("noche de casino"),
        {"rights": {"blocked_terms": ["casino"], "unknown_permission_policy": "block", "streamers": {}}},
        tmp_path / "casino",
        tmp_path,
    )
    assert record["estado"] == "test_rejected"
    assert record["rechazado_por"] == ["derechos_restringidos"]
    assert record["publication_eligible"] is False and record["ready_for_manual_upload"] is False
    assert record["juez"]["ejecutado"] is False
    assert called == []


def test_unknown_rechazado_por_el_juez_no_se_exporta(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "twitch-Abc.mp4").write_bytes(b"cache")
    moment = clipazos.Moment(_candidate(), tmp_path / "no.mp4", 0.0, 12.0, [CaptionCue("hola", 0.2, 0.5)], 4.0)
    moment.judgement = {"aceptado": False, "puntuacion": 0.4, "motivos": ["duracion: corta"], "criterios": {}}
    monkeypatch.setattr(clipazos, "build_moment", lambda *_a, **_k: moment)
    desk = tmp_path / "desk"
    summary = prueba_privada.run(
        json.loads(clipazos.CONFIG_PATH.read_text(encoding="utf-8")),
        [_candidate()],
        tmp_path / "out",
        desk,
        cache_dir=cache,
    )
    assert summary["exportados"] == 0 and summary["rechazados"]["juez"] == 1
    assert not desk.exists()
    saved = json.loads(next((tmp_path / "out").rglob("manifest.json")).read_text(encoding="utf-8"))
    assert saved["rights_status"] == "unknown" and saved["estado"] == "test_rejected"
    assert saved["juez"]["puntuacion"] == 0.4 and saved["juez"]["ejecutado"] is True
    assert saved["publication_eligible"] is False


def test_main_vacio_no_dispara_publicacion(tmp_path, monkeypatch):
    _tripwires(monkeypatch)
    before = clipazos.CONFIG_PATH.read_bytes()
    code = prueba_privada.main([
        "--config", str(clipazos.CONFIG_PATH),
        "--cache", str(tmp_path / "cache"),
        "--reportes", str(tmp_path / "reportes"),
        "--salida", str(tmp_path / "out"),
        "--escritorio", str(tmp_path / "desk"),
        "--max-evaluar", "2",
        "--max-exportar", "1",
    ])
    assert code == 0
    assert json.loads((tmp_path / "out" / "resumen.json").read_text(encoding="utf-8"))["exportados"] == 0
    assert clipazos.CONFIG_PATH.read_bytes() == before
    assert "PRUEBA PRIVADA" in (tmp_path / "out" / "README.txt").read_text(encoding="utf-8")


def test_cache_alterno_no_llama_a_ytdlp_ni_publica(tmp_path, monkeypatch):
    _tripwires(monkeypatch)
    calls: list[str] = []

    def _no_download(*_args, **_kwargs):
        calls.append("yt-dlp")
        raise AssertionError("yt-dlp no debe arrancar")

    monkeypatch.setattr(clipazos, "_ytdlp", _no_download)
    monkeypatch.setattr(clipazos, "CACHE_DIR", tmp_path / "global-vacio")
    alt = tmp_path / "cache-pedido"
    alt.mkdir()
    (alt / "twitch-Abc.mp4").write_bytes(b"video-local")
    found = clipazos.fetch_media(_candidate(), cache_dir=alt)
    assert found == alt / "twitch-Abc.mp4"
    assert calls == []
    empty = tmp_path / "cache-ausente"
    empty.mkdir()
    try:
        clipazos.fetch_media(_candidate(), cache_dir=empty)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("un caché vacío no puede seguir como si hubiera archivo")
    assert calls == []
    try:
        clipazos.fetch_media(_candidate())
    except AssertionError as exc:
        assert "yt-dlp" in str(exc)
    else:
        raise AssertionError("sin caché explícita la producción sí intenta descargar")
    assert calls == ["yt-dlp"]


def test_catalogo_solo_usa_la_cache(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "twitch-Abc.mp4").write_bytes(b"video")
    (cache / "kick-clip_ZZ.mp4").write_bytes(b"video")
    report = tmp_path / "reporte_prueba.json"
    report.write_text(
        json.dumps({
            "shorts": [{
                "descripcion": "@Ibai (Twitch): https://clips.twitch.tv/Abc\n@Brunenger (Kick): https://kick.com/brunenger/clips/clip_ZZ",
                "fuentes": [{
                    "platform": "Twitch", "streamer": "Ibai", "title": "el remate",
                    "game": "Minecraft", "url": "https://clips.twitch.tv/Abc", "key": "twitch-Abc",
                }],
            }]
        }),
        encoding="utf-8",
    )
    found = {item.key: item for item in prueba_privada.catalog(tmp_path, cache)}
    assert found["twitch-Abc"].title == "el remate"
    assert found["kick-clip_ZZ"].streamer == "Brunenger"
    assert "twitch-NoEsta" not in found


def test_aprobado_se_copia_como_prueba_y_no_publica(tmp_path, videos, monkeypatch):
    _tripwires(monkeypatch)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "twitch-Abc.mp4").write_bytes(b"cache")
    cues = [CaptionCue("mira", 0.2, 0.6), CaptionCue("esto", 1.2, 1.6), CaptionCue("gana", 2.2, 2.8)]
    moment = clipazos.Moment(
        _candidate(), videos["ok"], 0.0, 4.0, cues, 2.0,
        clipazos.Layout("split", 16 / 9, (0.4, 0.2, 0.2, 0.3)),
    )
    moment.judgement = {"aceptado": True, "puntuacion": 1.0, "motivos": [], "criterios": {}}
    moment.segment = {"start": 0.0, "end": 4.0, "peak": 2.0}
    monkeypatch.setattr(clipazos, "build_moment", lambda *_a, **_k: moment)

    def fake_render(_moments, _title, _channel, destination: Path):
        shutil.copy(videos["ok"], destination)
        shutil.copy(videos["ok"].with_suffix(".ass"), destination.with_suffix(".ass"))
        return destination

    monkeypatch.setattr(clipazos, "render_short", fake_render)
    config = {
        "idioma": "es",
        "rights": {"unknown_permission_policy": "block", "streamers": {}},
        "qa": {"duracion_min": 1, "duracion_max": 60},
        "seleccion": {"segundos_min": 10, "segundos_max": 45, "juez": {"puntuacion_min": 0.6}},
    }
    before_export = {path.name for path in (clipazos.BASE_DIR / "export_tiktok").glob("*")} if (clipazos.BASE_DIR / "export_tiktok").is_dir() else set()
    summary = prueba_privada.run(config, [_candidate()], tmp_path / "out", tmp_path / "desk", cache_dir=cache)
    assert summary["exportados"] == 1
    package = next((tmp_path / "desk").rglob("PRUEBA_NO_PUBLICAR.mp4")).parent
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["estado"] == "test_passed"
    assert manifest["test_only"] is True and manifest["publication_eligible"] is False
    assert manifest["ready_for_manual_upload"] is False
    assert manifest["rights_status"] == "unknown"
    assert manifest["juez"]["ejecutado"] is True and manifest["juez"]["version"]["puntuacion_min"] == 0.6
    assert manifest["arco"]["hook"] and manifest["arco"]["payoff"]
    assert manifest["video"]["sha256"] and manifest["probe"]["audio"] is True
    assert manifest["probe"]["herramienta"] in {"ffprobe", "ffmpeg"}
    assert (package / "cover.jpg").stat().st_size > 0
    assert manifest["portada"]["segundo"] == 2.0
    assert "PRUEBA PRIVADA" in (package / "caption.txt").read_text(encoding="utf-8")
    assert "PRUEBA PRIVADA" in (package.parent / "README.txt").read_text(encoding="utf-8")
    probed = qa.probe(package / "PRUEBA_NO_PUBLICAR.mp4")
    assert probed["ancho"] == 1080 and probed["alto"] == 1920 and probed["audio"] is True
    after_export = {path.name for path in (clipazos.BASE_DIR / "export_tiktok").glob("*")} if (clipazos.BASE_DIR / "export_tiktok").is_dir() else set()
    assert after_export == before_export


def _git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True, capture_output=True)


def test_sin_repo_la_procedencia_no_dice_limpio(tmp_path, monkeypatch):
    monkeypatch.setattr(clipazos, "BASE_DIR", tmp_path)
    for name in ("prueba_privada.py", "clip_judge.py", "narrative_segments.py", "clipazos.py"):
        (tmp_path / name).write_bytes(b"codigo")
    report = prueba_privada.code_provenance()
    assert report["git_estado"] == "desconocido"
    assert report["dirty"] is None
    assert report["motivo"]
    assert report["runner_en_commit"] is None
    assert len(report["sha256"]["clip_judge.py"]) == 64


def test_procedencia_separa_limpio_untracked_y_tracked(tmp_path, monkeypatch):
    for name in ("prueba_privada.py", "clip_judge.py", "narrative_segments.py", "clipazos.py"):
        (tmp_path / name).write_text("version\n", encoding="utf-8")
    _git_repo(tmp_path)
    subprocess.run(["git", "add", "--", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, check=True, capture_output=True)
    monkeypatch.setattr(clipazos, "BASE_DIR", tmp_path)
    clean = prueba_privada.code_provenance()
    assert clean["git_estado"] == "limpio", clean
    assert clean["dirty"] is False
    assert clean["runner_en_commit"] is True
    assert clean["untracked"] == [] and clean["tracked_modificados"] == []
    (tmp_path / "nota_nueva.py").write_text("nueva\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=no-mostrar\n", encoding="utf-8")
    dirty = prueba_privada.code_provenance()
    assert dirty["dirty"] is True
    assert "nota_nueva.py" in dirty["untracked"]
    assert all(".env" not in path for path in dirty["untracked"])
    assert dirty["privados_omitidos"] >= 1
    (tmp_path / "clip_judge.py").write_text("cambiado\n", encoding="utf-8")
    changed = prueba_privada.code_provenance()
    assert any(path.endswith("clip_judge.py") for path in changed["tracked_modificados"])
    assert changed["archivos_iguales_al_commit"]["clip_judge.py"] is False
    assert changed["runner_en_commit"] is False
    assert changed["sha256"]["prueba_privada.py"] == clean["sha256"]["prueba_privada.py"]
