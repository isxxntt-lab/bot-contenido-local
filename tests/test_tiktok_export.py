"""Exportación para TikTok con vídeos sintéticos de FFmpeg (sin red ni TikTok)."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone

import numpy as np
import pytest

import clipazos
import tiktok_export
from conftest import ROOT, allowed_config, make_video
from video_editor import _ffmpeg_exe

NOW = datetime(2026, 11, 9, 15, 30, tzinfo=timezone.utc)


def short_for(source, **extra) -> dict:
    short = {
        "titulo": "Lo mejor de Ana hoy",
        "descripcion": "Créditos a los streamers:\n#1 @Ana (Twitch): https://clips.twitch.tv/abc",
        "hashtags": ["#clipazos", "#twitch", "#ana"],
        "video": str(source),
        "duracion": 4.0,
        "clips": ["twitch-abc"],
        "fuentes": [{"platform": "Twitch", "streamer": "Ana", "title": "¡NO PUEDE SER!", "game": "Minecraft",
                     "url": "https://clips.twitch.tv/abc", "key": "twitch-abc"}],
        "seleccion": [{"clip": "twitch-abc", "aceptado": True, "puntuacion": 0.92,
                       "motivos": ["hook: voz desde el principio"], "criterios": {"hook": 1.0}}],
        "cortes": [{"clip": "twitch-abc", "inicio_clip": 3.2, "fin_clip": 7.2, "pico_short": 2.0}],
        "picos_short": [2.0],
        "qa": {"ok": True, "motivos": [], "metricas": {}, "omitido": False},
        "streamer": "Ana",
    }
    short.update(extra)
    return short


def gray_frame(path, second=None, size=(108, 192)):
    command = [_ffmpeg_exe(), "-v", "error"]
    if second is not None:
        command += ["-ss", f"{second:.3f}"]
    command += ["-i", str(path), "-frames:v", "1", "-vf", f"scale={size[0]}:{size[1]}",
                "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    raw = subprocess.run(command, capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(size[1], size[0]).astype(float)


def image_size(path):
    info = subprocess.run([_ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stderr
    import re

    found = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", info)
    return int(found[1]), int(found[2])


def test_exporta_carpeta_con_los_cuatro_archivos(tmp_path, videos):
    config = allowed_config(tmp_path)
    manifest = tiktok_export.export_short(short_for(videos["ok"]), config, root=tmp_path / "exp", now=NOW)
    folder = tmp_path / "exp" / manifest["id"]
    assert manifest["id"] == "20261109_ana_lo-mejor-de-ana-hoy"
    assert sorted(p.name for p in folder.iterdir()) == ["caption.txt", "cover.jpg", "manifest.json", "video.mp4"]
    on_disk = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    for key in ("id", "streamer", "plataforma", "url_fuente", "cortes", "duracion", "juez", "qa",
                "portada", "fecha", "archivos", "youtube"):
        assert key in on_disk, key
    assert on_disk["fecha"].startswith("2026-11-09T16:30:00+01:00")
    assert on_disk["juez"][0]["puntuacion"] == 0.92 and on_disk["juez"][0]["motivos"]
    assert on_disk["url_fuente"] == "https://clips.twitch.tv/abc"
    assert on_disk["subido_a_tiktok"] is False
    assert on_disk["youtube"]["estado"] == "pendiente"
    assert {name for name in on_disk["archivos"]} == {"video.mp4", "caption.txt", "cover.jpg", "manifest.json"}
    assert on_disk["video"]["metodo"] == "copia"  # el Short ya es H.264 + AAC 1080x1920 con faststart
    assert (folder / "video.mp4").read_bytes() == videos["ok"].read_bytes()


def test_caption_con_credito_y_hashtags(tmp_path, videos):
    manifest = tiktok_export.export_short(short_for(videos["ok"]), {}, root=tmp_path, now=NOW)
    caption = (tmp_path / manifest["id"] / "caption.txt").read_text(encoding="utf-8")
    assert caption.startswith("Lo mejor de Ana hoy")
    assert "Créditos al streamer original:" in caption
    assert "@Ana (Twitch) · https://clips.twitch.tv/abc" in caption
    assert "«¡NO PUEDE SER!»" in caption and "(Minecraft)" in caption
    tags = caption.strip().splitlines()[-1].split()
    assert tags[0] == "#clipazos" and "#fyp" in tags and "#ana" in tags and "#twitch" in tags
    assert len(tags) == len(set(tags)) <= 10
    assert len(caption) <= tiktok_export.CAPTION_LIMIT


def test_portada_sale_del_propio_video(tmp_path, videos):
    manifest = tiktok_export.export_short(short_for(videos["ok"]), {}, root=tmp_path, now=NOW)
    cover = tmp_path / manifest["id"] / "cover.jpg"
    assert image_size(cover) == (1080, 1920)
    second = manifest["portada"]["segundo"]
    assert abs(second - 2.0) <= 2.0  # alrededor del pico
    picked = gray_frame(cover)
    same = np.abs(picked - gray_frame(videos["ok"], second)).mean()
    other = np.abs(picked - gray_frame(videos["ok"], (second + 2.0) % 3.5)).mean()
    assert same < 6 and same < other


def test_portada_prefiere_fotograma_con_cara(tmp_path, monkeypatch):
    video = tmp_path / "cara.mp4"
    subprocess.run(
        [_ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:size=1080x1920:rate=24:duration=6",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
         "-vf", "drawbox=x=400:y=600:w=300:h=300:color=white:t=fill:enable='between(t,4.4,5.2)'",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         "-movflags", "+faststart", str(video)],
        check=True, capture_output=True,
    )

    class FakeYuNet:
        """Hace de YuNet: «ve» una cara cuando aparece el recuadro blanco."""

        def detect(self, frame):
            if frame.mean() > frame[0:20, 0:20].mean() + 5:
                return 1, np.array([[100, 150, 75, 75] + [0] * 10 + [0.95]], dtype=np.float32)
            return 1, None

    monkeypatch.setattr(tiktok_export, "_face_detector", lambda: FakeYuNet())
    short = short_for(video, duracion=6.0, picos_short=[3.5])
    manifest = tiktok_export.export_short(short, {}, root=tmp_path, now=NOW)
    assert manifest["portada"]["metodo"] in {"cara_cerca_del_pico", "cara"}
    assert 4.4 <= manifest["portada"]["segundo"] <= 5.2
    assert manifest["portada"]["cara"] > 0


def test_sin_cara_usa_el_pico(tmp_path, videos, monkeypatch):
    monkeypatch.setattr(tiktok_export, "_face_detector", lambda: None)
    manifest = tiktok_export.export_short(short_for(videos["ok"]), {}, root=tmp_path, now=NOW)
    assert manifest["portada"]["metodo"] == "pico"
    assert manifest["portada"]["detector_caras"] == "no_disponible"


def test_video_horizontal_se_optimiza_para_tiktok(tmp_path, videos):
    manifest = tiktok_export.export_short(short_for(videos["horizontal"]), {}, root=tmp_path, now=NOW)
    assert manifest["video"]["metodo"] == "recodificado"
    info = tiktok_export.inspect_video(tmp_path / manifest["id"] / "video.mp4")
    assert (info["ancho"], info["alto"], info["video_codec"], info["audio_codec"]) == (1080, 1920, "h264", "aac")
    assert info["faststart"] is True


def test_si_falla_la_optimizacion_copia_el_original(tmp_path, videos, monkeypatch):
    monkeypatch.setattr(tiktok_export, "_tiktok_ready", lambda info: False)
    manifest = tiktok_export.export_short(short_for(videos["horizontal"]), {}, root=tmp_path, now=NOW)
    assert manifest["video"]["metodo"] == "copia" and "no se optimizó" in manifest["video"]["aviso"]
    exported = tmp_path / manifest["id"] / "video.mp4"
    assert exported.read_bytes() == videos["horizontal"].read_bytes()
    assert not list((tmp_path / manifest["id"]).glob("*.part.mp4"))


@pytest.mark.parametrize(
    "change, reason",
    [
        ({"qa": {"ok": False, "motivos": ["negro"]}}, "QA"),
        ({"seleccion": [{"clip": "x", "aceptado": False}]}, "juez"),
        ({"video": "no_existe.mp4"}, "no existe"),
    ],
)
def test_no_exporta_si_no_esta_aprobado(tmp_path, videos, change, reason):
    with pytest.raises(tiktok_export.ExportError, match=reason):
        tiktok_export.export_short(short_for(videos["ok"], **change), {}, root=tmp_path / "exp", now=NOW)
    assert not (tmp_path / "exp").exists()


def test_manifest_documenta_bloqueo_de_youtube_por_derechos(tmp_path, videos):
    config = {"rights": {"default_policy": "block"}, "subida_youtube": {"activa": True}}
    manifest = tiktok_export.export_short(short_for(videos["ok"]), config, root=tmp_path, now=NOW)
    assert manifest["youtube"]["estado"] == "bloqueado"
    assert manifest["youtube"]["bloqueado_por"] == "derechos"


def test_dry_run_marca_carpeta_y_no_se_sube(tmp_path, videos):
    manifest = tiktok_export.export_short(short_for(videos["ok"]), allowed_config(tmp_path), dry_run=True,
                                          root=tmp_path, now=NOW)
    assert manifest["id"].endswith("_dryrun") and manifest["dry_run"] is True
    assert manifest["youtube"]["estado"] == "dry_run_no_se_sube"


def test_refresca_estado_de_youtube_tras_subir(tmp_path, videos):
    short = short_for(videos["ok"])
    manifest = tiktok_export.export_short(short, allowed_config(tmp_path), root=tmp_path, now=NOW)
    path = manifest["archivos"]["manifest.json"]
    short.update(youtube_id="abc123", youtube_url="https://youtube.com/shorts/abc123", youtube_privacidad="public",
                 tiktok_export={"estado": "ok", "manifest": path})
    report = tmp_path / "reporte.json"
    report.write_text(json.dumps({"shorts": [short]}), encoding="utf-8")
    assert tiktok_export.refresh_youtube(report, {}) == 1
    updated = json.loads(open(path, encoding="utf-8").read())
    assert updated["youtube"] == {"estado": "subido", "id": "abc123",
                                  "url": "https://youtube.com/shorts/abc123", "privacidad": "public"}


def test_config_real_tiene_bloque_tiktok():
    config = json.loads((ROOT / "clipazos.json").read_text(encoding="utf-8"))
    block = config["tiktok_export"]
    assert block["enabled"] is True and block["carpeta"] == "export_tiktok" and block["hashtags_base"]
    assert config["subida_youtube"]["privacidad"] == "public"
    assert "export_tiktok/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


# --- Integración con clipazos.make_short -------------------------------------------------


ALLOWED = {"rights": {"streamers": {"ana": "allowed"}}, "qa": {"duracion_min": 1}}


def _prepare_make_short(monkeypatch, videos):
    def fake_moment(candidate, *args, **kwargs):
        moment = clipazos.Moment(candidate, ROOT / "x.mp4", 0.5, 3.5, [], peak=1.5, cuts=[(0.5, 4.0)])
        moment.segment = {"end": 4.0, "first_word_start": 0.6, "last_word_end": 3.9, "notes": []}
        moment.judgement = {"aceptado": True, "puntuacion": 0.9, "motivos": [], "criterios": {}}
        return moment

    def fake_render(moments, _title, _channel, destination):
        shutil.copy(videos["ok"], destination)
        shutil.copy(videos["ok"].with_suffix(".ass"), destination.with_suffix(".ass"))
        return destination

    monkeypatch.setattr(clipazos, "build_moment", fake_moment)
    monkeypatch.setattr(clipazos, "render_short", fake_render)
    clipazos._SELECTION_LOG.clear()


def _candidate():
    return clipazos.Candidate("Twitch", "Ana", "Momentazo", 1000, "https://clips.twitch.tv/m", "twitch-m")


def test_make_short_exporta_si_aprueba_juez_y_qa(tmp_path, videos, monkeypatch):
    _prepare_make_short(monkeypatch, videos)
    post = clipazos.make_short([_candidate()], ALLOWED, [[("X", False)]], "Lo mejor de Ana", tmp_path / "s.mp4", [])
    assert post["tiktok_export"]["estado"] == "ok"
    folder = tmp_path / "export_tiktok"
    exported = list(folder.iterdir())
    assert len(exported) == 1 and (exported[0] / "cover.jpg").is_file()
    assert post["picos_short"] == [1.5] and post["cortes"][0]["trozos_clip"] == [[0.5, 4.0]]
    manifest = json.loads((exported[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["cortes"][0]["inicio_clip"] == 0.5


def test_fallo_de_export_no_rompe_el_flujo(tmp_path, videos, monkeypatch):
    _prepare_make_short(monkeypatch, videos)

    def broken(*args, **kwargs):
        raise OSError("disco lleno")

    monkeypatch.setattr(tiktok_export, "export_short", broken)
    post = clipazos.make_short([_candidate()], ALLOWED, [[("X", False)]], "T", tmp_path / "s.mp4", [])
    assert post is not None and post["qa"]["ok"] is True
    assert post["tiktok_export"] == {"estado": "error", "motivo": "disco lleno"}
    assert "no_subir" not in post


def test_no_exporta_si_qa_falla(tmp_path, videos, monkeypatch):
    _prepare_make_short(monkeypatch, videos)
    called = []
    monkeypatch.setattr(tiktok_export, "export_short", lambda *a, **k: called.append(1))
    config = {**ALLOWED, "qa": {"duracion_min": 30}}
    post = clipazos.make_short([_candidate()], config, [[("X", False)]], "T", tmp_path / "s.mp4", [])
    assert post["no_subir"] is True and not called and "tiktok_export" not in post


def test_exporta_en_dry_run(tmp_path, videos, monkeypatch):
    _prepare_make_short(monkeypatch, videos)
    monkeypatch.setattr(clipazos, "_RUN_SUFFIX", clipazos.DRY_RUN_SUFFIX)
    post = clipazos.make_short([_candidate()], ALLOWED, [[("X", False)]], "T", tmp_path / "s.mp4", [])
    assert post["tiktok_export"]["carpeta"].endswith("_dryrun")


def test_export_desactivado(tmp_path, videos, monkeypatch):
    _prepare_make_short(monkeypatch, videos)
    config = {**ALLOWED, "tiktok_export": {"enabled": False}}
    post = clipazos.make_short([_candidate()], config, [[("X", False)]], "T", tmp_path / "s.mp4", [])
    assert "tiktok_export" not in post and not (tmp_path / "export_tiktok").exists()


# --- Copia a la carpeta visible (Escritorio) -----------------------------------------------


def test_copia_la_carpeta_completa_a_la_visible(tmp_path, videos):
    manifest = tiktok_export.export_short(short_for(videos["ok"]), {}, root=tmp_path / "exp", now=NOW)
    visible = tmp_path / "visible" / manifest["id"]
    assert manifest["copia_visible"] == {"estado": "ok", "carpeta": str(visible)}
    assert sorted(p.name for p in visible.iterdir()) == ["caption.txt", "cover.jpg", "manifest.json", "video.mp4"]
    assert (visible / "video.mp4").read_bytes() == (tmp_path / "exp" / manifest["id"] / "video.mp4").read_bytes()
    copied = json.loads((visible / "manifest.json").read_text(encoding="utf-8"))
    assert copied["copia_visible"]["estado"] == "ok"


def test_fallo_de_copia_visible_no_rompe_el_export(tmp_path, videos, monkeypatch):
    blocker = tmp_path / "soy_un_archivo"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(tiktok_export, "visible_root", lambda config=None: blocker)
    manifest = tiktok_export.export_short(short_for(videos["ok"]), {}, root=tmp_path / "exp", now=NOW)
    assert manifest["copia_visible"]["estado"] == "error" and manifest["copia_visible"]["motivo"]
    folder = tmp_path / "exp" / manifest["id"]
    assert sorted(p.name for p in folder.iterdir()) == ["caption.txt", "cover.jpg", "manifest.json", "video.mp4"]


def test_dry_run_tambien_copia_a_la_visible(tmp_path, videos):
    manifest = tiktok_export.export_short(short_for(videos["ok"]), {}, dry_run=True, root=tmp_path / "exp", now=NOW)
    assert (tmp_path / "visible" / manifest["id"] / "video.mp4").is_file()
    assert manifest["id"].endswith("_dryrun")


def test_refresco_de_youtube_actualiza_tambien_la_copia_visible(tmp_path, videos):
    short = short_for(videos["ok"])
    manifest = tiktok_export.export_short(short, {}, root=tmp_path / "exp", now=NOW)
    short.update(youtube_id="xyz", youtube_url="https://youtube.com/shorts/xyz", youtube_privacidad="public",
                 tiktok_export={"estado": "ok", "manifest": manifest["archivos"]["manifest.json"]})
    report = tmp_path / "reporte.json"
    report.write_text(json.dumps({"shorts": [short]}), encoding="utf-8")
    assert tiktok_export.refresh_youtube(report, {}) == 1
    copied = json.loads((tmp_path / "visible" / manifest["id"] / "manifest.json").read_text(encoding="utf-8"))
    assert copied["youtube"]["estado"] == "subido"


def test_resolucion_de_la_carpeta_visible(tmp_path, monkeypatch):
    resolve = tiktok_export._real_visible_root
    desktop = tmp_path / "Escritorio"
    assert resolve({"tiktok_export": {"carpeta_visible": "{DESKTOP}/Clipazos TikTok"}}) == desktop / "Clipazos TikTok"
    monkeypatch.setenv("CARPETA_PRUEBA", str(tmp_path / "otra"))
    assert resolve({"tiktok_export": {"carpeta_visible": "$CARPETA_PRUEBA/clips"}}) == tmp_path / "otra" / "clips"
    assert resolve({"tiktok_export": {"carpeta_visible": ""}}) is None
    assert resolve({"tiktok_export": {"carpeta_visible": None}}) is None
    assert resolve({"tiktok_export": {"carpeta_visible": "relativa"}}) == tiktok_export.BASE_DIR / "relativa"


def test_desactivada_no_copia(tmp_path, videos, monkeypatch):
    monkeypatch.setattr(tiktok_export, "visible_root", tiktok_export._real_visible_root)
    config = {"tiktok_export": {"carpeta_visible": ""}}
    manifest = tiktok_export.export_short(short_for(videos["ok"]), config, root=tmp_path / "exp", now=NOW)
    assert manifest["copia_visible"] == {"estado": "desactivada"}


def test_config_real_apunta_al_escritorio():
    config = json.loads((ROOT / "clipazos.json").read_text(encoding="utf-8"))
    assert config["tiktok_export"]["carpeta_visible"].startswith("{DESKTOP}")
