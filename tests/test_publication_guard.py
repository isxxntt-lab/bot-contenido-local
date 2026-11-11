from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import publication_guard as pg
from conftest import ROOT, allowed_config


def short(tmp_path: Path, name: str, streamer: str = "Ana", clips: list[str] | None = None) -> dict:
    video = tmp_path / f"{name}.mp4"
    video.write_bytes(name.encode() * 50)
    return {"video": str(video), "streamer": streamer, "clips": clips if clips is not None else [f"twitch-{name}"]}


def fixed(moment: str):
    return lambda: datetime.fromisoformat(moment)


def test_defaults_seguros_y_privacidad():
    settings = pg.settings_from({})
    assert settings["max_por_dia"] == 1 and settings["max_por_ejecucion"] == 1
    assert settings["kill_switch"] is False
    assert pg.resolve_privacy({}) == "private"
    assert pg.resolve_privacy({"subida_youtube": {"activa": True}}) == "private"
    assert pg.resolve_privacy({"subida_youtube": {"privacidad": "rarísima"}}) == "private"
    assert pg.resolve_privacy({"subida_youtube": {"privacidad": "public"}}) == "public"
    assert pg.settings_from({"publication": {"max_por_dia": "x", "kill_switch": "si"}})["max_por_dia"] == 1
    assert pg.settings_from({"publication": {"kill_switch": "si"}})["kill_switch"] is False


def test_clipazos_json_mantiene_la_privacidad_elegida():
    config = json.loads((ROOT / "clipazos.json").read_text(encoding="utf-8"))
    assert config["subida_youtube"]["privacidad"] == "public"
    assert {"publication", "qa"} <= set(config)
    # El bloque rights era del formato de streamers; quedó en el JSON archivado.
    assert "rights" not in config
    assert config["publication"]["db_path"] == "data/publicaciones.sqlite3"


def test_reserva_confirma_y_no_duplica(tmp_path):
    guard = pg.PublicationGuard(allowed_config(tmp_path))
    first = short(tmp_path, "a")
    decision = guard.reserve_publication(first)
    assert decision.allowed and decision.reservation_id > 0
    guard.confirm_publication(decision.reservation_id, "yt1")
    again = pg.PublicationGuard(allowed_config(tmp_path)).reserve_publication(first)
    assert not again.allowed and again.code == "duplicado"
    # Mismo clip dentro de otro vídeo distinto: también es duplicado.
    other = short(tmp_path, "b", streamer="Beto", clips=["twitch-a"])
    assert pg.PublicationGuard(allowed_config(tmp_path)).reserve_publication(other).code == "duplicado"


def test_liberar_no_gasta_cupo(tmp_path):
    config = allowed_config(tmp_path, max_por_dia=1)
    guard = pg.PublicationGuard(config)
    decision = guard.reserve_publication(short(tmp_path, "a"))
    assert decision.allowed
    assert guard.reserve_publication(short(tmp_path, "b", streamer="Beto")).code == "cupo_dia"
    guard.release_publication(decision.reservation_id, "falló la red")
    assert guard.reserve_publication(short(tmp_path, "b", streamer="Beto")).allowed


def test_cupos_dia_ejecucion_y_streamer(tmp_path):
    config = allowed_config(tmp_path, max_por_dia=5, max_por_ejecucion=2, max_por_streamer_dia=1)
    guard = pg.PublicationGuard(config)
    assert guard.reserve_publication(short(tmp_path, "a", "Ana")).allowed
    assert guard.reserve_publication(short(tmp_path, "b", "Ana")).code == "cupo_streamer"
    assert guard.reserve_publication(short(tmp_path, "c", "Beto")).allowed
    assert guard.reserve_publication(short(tmp_path, "d", "Carla")).code == "cupo_ejecucion"
    # Otra ejecución (otro run_id) sí puede, hasta el cupo diario.
    assert pg.PublicationGuard(config).reserve_publication(short(tmp_path, "d", "Carla")).allowed
    config_day = allowed_config(tmp_path, max_por_dia=3, max_por_ejecucion=9, max_por_streamer_dia=9)
    assert pg.PublicationGuard(config_day).reserve_publication(short(tmp_path, "e", "Dani")).code == "cupo_dia"


def test_reserva_colgada_sigue_contando_tras_reinicio(tmp_path):
    config = allowed_config(tmp_path, max_por_dia=1)
    pg.PublicationGuard(config).reserve_publication(short(tmp_path, "a"))
    # "Reinicio": guardia nueva, misma base.
    restarted = pg.PublicationGuard(config)
    assert restarted.check_publication_allowed(short(tmp_path, "b", "Beto")).code == "cupo_dia"
    assert [row["status"] for row in restarted.rows()] == ["reserved"]


def test_dias_en_madrid(tmp_path):
    config = allowed_config(tmp_path, max_por_dia=1)
    # 9 nov 22:30 UTC = 9 nov 23:30 en Madrid; 23:30 UTC = 10 nov 00:30 en Madrid.
    late = pg.PublicationGuard(config, now=fixed("2026-11-09T22:30:00+00:00"))
    assert late.reserve_publication(short(tmp_path, "a")).details["dia"] == "2026-11-09"
    next_day = pg.PublicationGuard(config, now=fixed("2026-11-09T23:30:00+00:00"))
    decision = next_day.reserve_publication(short(tmp_path, "b", "Beto"))
    assert decision.allowed and decision.details["dia"] == "2026-11-10"


def test_horario_de_verano_sin_tzdata():
    assert pg.madrid_offset(datetime(2026, 7, 1, 12, tzinfo=timezone.utc)).seconds == 7200
    assert pg.madrid_offset(datetime(2026, 11, 9, 12, tzinfo=timezone.utc)).seconds == 3600
    # Cambio de hora: 29 mar 2026 01:00 UTC y 25 oct 2026 01:00 UTC.
    assert pg.madrid_offset(datetime(2026, 3, 29, 0, 59, tzinfo=timezone.utc)).seconds == 3600
    assert pg.madrid_offset(datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)).seconds == 7200
    assert pg.madrid_offset(datetime(2026, 10, 25, 0, 59, tzinfo=timezone.utc)).seconds == 7200
    assert pg.madrid_offset(datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)).seconds == 3600


def test_kill_switch_en_config_y_en_archivo(tmp_path):
    config = allowed_config(tmp_path)
    config_path = tmp_path / "clipazos.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    guard = pg.PublicationGuard(config, config_path)
    assert guard.kill_switch_reason() == ""
    # Se relee del disco: cambiar el JSON después de crear la guardia cuenta.
    config["publication"]["kill_switch"] = True
    config_path.write_text(json.dumps(config), encoding="utf-8")
    decision = guard.reserve_publication(short(tmp_path, "a"))
    assert decision.code == "kill_switch"
    config["publication"]["kill_switch"] = False
    config_path.write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "STOP_YOUTUBE").write_text("", encoding="utf-8")
    assert "STOP_YOUTUBE" in guard.kill_switch_reason()
    (tmp_path / "STOP_YOUTUBE").unlink()
    config_path.write_text("{roto", encoding="utf-8")
    assert "seguridad" in guard.kill_switch_reason()


def test_dry_run_no_escribe_la_base(tmp_path):
    config = allowed_config(tmp_path, max_por_ejecucion=1)
    guard = pg.PublicationGuard(config, dry_run=True)
    assert guard.reserve_publication(short(tmp_path, "a")).allowed
    assert guard.reserve_publication(short(tmp_path, "b", "Beto")).code == "cupo_ejecucion"
    assert not Path(config["publication"]["db_path"]).exists()


def test_dos_procesos_no_se_pasan_del_cupo(tmp_path):
    config = allowed_config(tmp_path, max_por_dia=1, max_por_ejecucion=5, max_por_streamer_dia=5)
    config_path = tmp_path / "clipazos.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    script = (
        "import json,sys,time; sys.path.insert(0, sys.argv[1]); import publication_guard as pg;"
        "from pathlib import Path; cfg=json.loads(Path(sys.argv[2]).read_text());"
        "v=Path(sys.argv[3]); v.write_bytes(sys.argv[4].encode()*40);"
        "time.sleep(max(0.0, float(sys.argv[5])-time.time()));"
        "d=pg.PublicationGuard(cfg).reserve_publication({'video':str(v),'streamer':'s'+sys.argv[4],'clips':['c'+sys.argv[4]]});"
        "print('OK' if d.allowed else d.code)"
    )
    import time

    start = time.time() + 1.5
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(ROOT), str(config_path), str(tmp_path / f"p{index}.mp4"), str(index), str(start)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for index in range(4)
    ]
    results = []
    for process in processes:
        out, err = process.communicate(timeout=60)
        assert process.returncode == 0, err
        results.append(out.strip())
    assert results.count("OK") == 1, results
    assert results.count("cupo_dia") == 3, results
