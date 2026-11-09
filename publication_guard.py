"""Guardia de publicación de Clipazos ES en YouTube.

Decide si un Short se puede subir *ahora* y lo deja apuntado en una base SQLite:

- kill switch: ``publication.kill_switch`` en clipazos.json o el archivo
  ``STOP_YOUTUBE`` en la carpeta del proyecto. Se relee del disco justo antes de
  cada llamada a YouTube. Solo frena las subidas: la descarga y la edición siguen.
- Cupos: máximo por día, por ejecución y por streamer y día. Los días se cuentan
  en hora de Madrid (Europe/Madrid).
- Sin duplicados: un clip (o el mismo MP4) que ya está subido o reservado no se
  vuelve a subir.
- Reservas en SQLite con transacciones ``BEGIN IMMEDIATE``: dos procesos a la vez
  no pueden pasarse del cupo y el estado sobrevive a reinicios.

Flujo: ``check_publication_allowed`` (solo mira) → ``reserve_publication``
(aparta el hueco) → subida → ``confirm_publication`` si salió bien o
``release_publication`` si falló (el hueco no se gasta).

Una reserva que se queda colgada (el proceso murió a mitad de subida) sigue
contando, porque no se sabe si el vídeo llegó a YouTube. Se libera a mano:
    python publication_guard.py --estado
    python publication_guard.py --liberar <id>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "clipazos.json"

VALID_PRIVACY = ("private", "unlisted", "public")
DEFAULT_PRIVACY = "private"

# Valores seguros para cuando falta el bloque "publication" o alguna clave.
DEFAULTS: dict = {
    "kill_switch": False,
    "kill_switch_file": "STOP_YOUTUBE",
    "max_por_dia": 1,
    "max_por_ejecucion": 1,
    "max_por_streamer_dia": 1,
    "zona_horaria": "Europe/Madrid",
    "db_path": "data/publicaciones.sqlite3",
}

_ACTIVE = ("reserved", "published")


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------


def resolve_privacy(config: dict | None) -> str:
    """Privacidad de subida. Sin clave o con un valor raro: private."""
    upload = (config or {}).get("subida_youtube") or {}
    value = upload.get("privacidad") if isinstance(upload, dict) else None
    if isinstance(value, str) and value.strip().lower() in VALID_PRIVACY:
        return value.strip().lower()
    return DEFAULT_PRIVACY


def _as_limit(value, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def settings_from(config: dict | None) -> dict:
    """Bloque "publication" con los huecos rellenos por valores seguros."""
    raw = (config or {}).get("publication")
    raw = raw if isinstance(raw, dict) else {}
    settings = dict(DEFAULTS)
    settings["kill_switch"] = raw.get("kill_switch", DEFAULTS["kill_switch"]) is True
    for key in ("max_por_dia", "max_por_ejecucion", "max_por_streamer_dia"):
        settings[key] = _as_limit(raw.get(key, DEFAULTS[key]), DEFAULTS[key])
    for key in ("kill_switch_file", "zona_horaria", "db_path"):
        value = raw.get(key, DEFAULTS[key])
        settings[key] = value if isinstance(value, str) and value.strip() else DEFAULTS[key]
    return settings


def _resolve(path_text: str) -> Path:
    path = Path(path_text)
    return path if path.is_absolute() else BASE_DIR / path


# ---------------------------------------------------------------------------
# Fecha en Madrid
# ---------------------------------------------------------------------------


def _last_sunday_utc(year: int, month: int) -> datetime:
    day = datetime(year, month, 31, 1, 0, tzinfo=timezone.utc)
    while day.weekday() != 6:
        day -= timedelta(days=1)
    return day


def madrid_offset(moment_utc: datetime) -> timedelta:
    """Hora de Madrid sin tzdata: CET (+1) y CEST (+2) entre los últimos domingos
    de marzo y de octubre a la 01:00 UTC (regla de la UE)."""
    moment_utc = moment_utc.astimezone(timezone.utc)
    start = _last_sunday_utc(moment_utc.year, 3)
    end = _last_sunday_utc(moment_utc.year, 10)
    return timedelta(hours=2) if start <= moment_utc < end else timedelta(hours=1)


def local_day(moment: datetime, zone: str = "Europe/Madrid") -> date:
    """Día natural de ``moment`` en la zona indicada (Europe/Madrid por defecto)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        return moment.astimezone(ZoneInfo(zone)).date()
    except Exception:  # noqa: BLE001 - en Windows sin el paquete tzdata no hay base de zonas
        utc = moment.astimezone(timezone.utc)
        return (utc + madrid_offset(utc)).date()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Claves de un Short
# ---------------------------------------------------------------------------


def file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def short_keys(short: dict) -> tuple[list[str], str | None]:
    """Claves de duplicado: cada clip del Short y el hash del MP4."""
    keys = [
        f"clip:{str(key).strip().casefold()}"
        for key in short.get("clips") or []
        if str(key).strip()
    ]
    digest = None
    if short.get("video"):
        digest = file_sha256(Path(short["video"]))
    if digest:
        keys.append(f"sha256:{digest}")
    elif short.get("video"):
        keys.append(f"video:{Path(short['video']).name.casefold()}")
    return list(dict.fromkeys(keys)), digest


# ---------------------------------------------------------------------------
# Guardia
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    code: str = "ok"
    reservation_id: int | None = None
    details: dict = field(default_factory=dict)

    def __bool__(self) -> bool:  # pragma: no cover - azúcar
        return self.allowed


class PublicationGuard:
    """Una instancia por ejecución: el cupo «por ejecución» usa su ``run_id``."""

    def __init__(
        self,
        config: dict | None = None,
        config_path: Path | None = None,
        *,
        dry_run: bool = False,
        now: Callable[[], datetime] = _utc_now,
        run_id: str | None = None,
    ) -> None:
        self.config = config if config is not None else {}
        self.config_path = Path(config_path) if config_path else None
        self.dry_run = dry_run
        self.now = now
        self.run_id = run_id or uuid.uuid4().hex
        self._simulated: list[dict] = []
        self.db_path = _resolve(settings_from(self._fresh_config()[0] or self.config)["db_path"])

    # -- configuración releída del disco ---------------------------------
    def _fresh_config(self) -> tuple[dict | None, str]:
        if self.config_path is None:
            return self.config, ""
        try:
            return json.loads(self.config_path.read_text(encoding="utf-8")), ""
        except (OSError, ValueError) as exc:
            return None, f"no se pudo releer {self.config_path.name} ({exc})"

    def current_settings(self) -> tuple[dict, str]:
        config, error = self._fresh_config()
        return settings_from(config if config is not None else self.config), error

    def kill_switch_reason(self) -> str:
        """Motivo para no subir por kill switch, o "" si se puede. Lee el disco."""
        settings, error = self.current_settings()
        if error:
            return f"kill switch por seguridad: {error}"
        if settings["kill_switch"]:
            return "kill switch activo (publication.kill_switch = true)"
        stop_file = _resolve(settings["kill_switch_file"])
        if stop_file.exists():
            return f"kill switch activo (existe {stop_file.name})"
        return ""

    # -- base de datos ------------------------------------------------------
    def _connect(self, readonly: bool = False) -> sqlite3.Connection | None:
        if readonly:
            if not self.db_path.is_file():
                return None
            uri = f"file:{self.db_path.as_posix()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
            connection.execute("PRAGMA busy_timeout=30000")
            self._ensure_schema(connection)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        # Diario de rollback (no WAL): más simple con dos procesos y en carpetas sincronizadas.
        for attempt in range(20):
            try:
                PublicationGuard._create_tables(connection)
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or attempt == 19:
                    raise
                time.sleep(0.1 * (attempt + 1))

    @staticmethod
    def _create_tables(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS publications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                streamer TEXT NOT NULL,
                run_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('reserved', 'published', 'released')),
                video TEXT,
                video_sha256 TEXT,
                youtube_id TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS publication_keys (
                publication_id INTEGER NOT NULL REFERENCES publications(id),
                key TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_publications_day ON publications(day, status);
            CREATE INDEX IF NOT EXISTS idx_publication_keys_key ON publication_keys(key);
            """
        )

    # -- reglas -------------------------------------------------------------
    def _evaluate(self, connection, short: dict, settings: dict) -> Decision:
        killed = self.kill_switch_reason()
        if killed:
            return Decision(False, killed, "kill_switch")
        streamer = str(short.get("streamer") or "desconocido").strip().casefold()
        keys, digest = short_keys(short)
        if not keys:
            return Decision(False, "el Short no tiene clips ni vídeo con los que comprobar duplicados", "sin_claves")
        today = local_day(self.now(), settings["zona_horaria"]).isoformat()

        def count(sql: str, params: tuple) -> int:
            if connection is None:
                return 0
            return int(connection.execute(sql, params).fetchone()[0])

        marks = ",".join("?" for _ in _ACTIVE)
        day_count = count(f"SELECT COUNT(*) FROM publications WHERE day = ? AND status IN ({marks})", (today, *_ACTIVE))
        run_count = count(f"SELECT COUNT(*) FROM publications WHERE run_id = ? AND status IN ({marks})", (self.run_id, *_ACTIVE))
        streamer_count = count(
            f"SELECT COUNT(*) FROM publications WHERE day = ? AND streamer = ? AND status IN ({marks})",
            (today, streamer, *_ACTIVE),
        )
        duplicate = None
        if connection is not None:
            key_marks = ",".join("?" for _ in keys)
            row = connection.execute(
                f"SELECT k.key, p.id, p.status FROM publication_keys k JOIN publications p ON p.id = k.publication_id "
                f"WHERE k.key IN ({key_marks}) AND p.status IN ({marks}) LIMIT 1",
                (*keys, *_ACTIVE),
            ).fetchone()
            if row is not None:
                duplicate = f"{row['key']} (publicación {row['id']}, {row['status']})"
        for item in self._simulated:
            day_count += item["day"] == today
            run_count += 1
            streamer_count += item["day"] == today and item["streamer"] == streamer
            if duplicate is None and set(keys) & set(item["keys"]):
                duplicate = f"{sorted(set(keys) & set(item['keys']))[0]} (simulado en este --dry-run)"

        details = {
            "dia": today,
            "hoy": day_count,
            "ejecucion": run_count,
            "streamer_hoy": streamer_count,
            "claves": keys,
            "sha256": digest,
            "streamer": streamer,
        }
        if duplicate:
            return Decision(False, f"clip ya subido o reservado: {duplicate}", "duplicado", details=details)
        if day_count >= settings["max_por_dia"]:
            return Decision(False, f"cupo diario lleno ({day_count}/{settings['max_por_dia']} el {today})", "cupo_dia", details=details)
        if run_count >= settings["max_por_ejecucion"]:
            return Decision(False, f"cupo de esta ejecución lleno ({run_count}/{settings['max_por_ejecucion']})", "cupo_ejecucion", details=details)
        if streamer_count >= settings["max_por_streamer_dia"]:
            return Decision(
                False,
                f"cupo de @{streamer} para hoy lleno ({streamer_count}/{settings['max_por_streamer_dia']})",
                "cupo_streamer",
                details=details,
            )
        return Decision(True, "", "ok", details=details)

    def check_publication_allowed(self, short: dict) -> Decision:
        """Solo comprueba. No escribe nada."""
        settings, _error = self.current_settings()
        connection = self._connect(readonly=True)
        try:
            return self._evaluate(connection, short, settings)
        finally:
            if connection is not None:
                connection.close()

    def reserve_publication(self, short: dict) -> Decision:
        """Aparta un hueco de forma atómica. En --dry-run se simula en memoria."""
        settings, _error = self.current_settings()
        if self.dry_run:
            decision = self.check_publication_allowed(short)
            if decision.allowed:
                self._simulated.append(
                    {"day": decision.details["dia"], "streamer": decision.details["streamer"], "keys": decision.details["claves"]}
                )
                decision.reservation_id = -len(self._simulated)
            return decision
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                decision = self._evaluate(connection, short, settings)
                if decision.allowed:
                    stamp = self.now().astimezone(timezone.utc).isoformat()
                    cursor = connection.execute(
                        "INSERT INTO publications (day, streamer, run_id, status, video, video_sha256, created_at, updated_at) "
                        "VALUES (?, ?, ?, 'reserved', ?, ?, ?, ?)",
                        (
                            decision.details["dia"],
                            decision.details["streamer"],
                            self.run_id,
                            str(short.get("video") or ""),
                            decision.details["sha256"],
                            stamp,
                            stamp,
                        ),
                    )
                    decision.reservation_id = int(cursor.lastrowid)
                    connection.executemany(
                        "INSERT INTO publication_keys (publication_id, key) VALUES (?, ?)",
                        [(decision.reservation_id, key) for key in decision.details["claves"]],
                    )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            return decision
        finally:
            connection.close()

    def _set_status(self, reservation_id: int, status: str, youtube_id: str | None, reason: str | None) -> None:
        if reservation_id is None or reservation_id < 0 or self.dry_run:
            return
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM publications WHERE id = ?", (reservation_id,)).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise KeyError(f"no existe la reserva {reservation_id}")
            if row["status"] == "published" and status == "released":
                connection.execute("ROLLBACK")
                raise ValueError(f"la publicación {reservation_id} ya está confirmada; no se libera")
            connection.execute(
                "UPDATE publications SET status = ?, youtube_id = COALESCE(?, youtube_id), reason = ?, updated_at = ? WHERE id = ?",
                (status, youtube_id, reason, self.now().astimezone(timezone.utc).isoformat(), reservation_id),
            )
            connection.execute("COMMIT")
        finally:
            connection.close()

    def confirm_publication(self, reservation_id: int, youtube_id: str) -> None:
        self._set_status(reservation_id, "published", youtube_id, None)

    def release_publication(self, reservation_id: int, reason: str = "") -> None:
        """Devuelve el hueco: la subida falló o no se llegó a intentar."""
        self._set_status(reservation_id, "released", None, reason or "liberada")

    def rows(self, day: str | None = None) -> list[dict]:
        connection = self._connect(readonly=True)
        if connection is None:
            return []
        try:
            if day:
                found = connection.execute("SELECT * FROM publications WHERE day = ? ORDER BY id", (day,))
            else:
                found = connection.execute("SELECT * FROM publications ORDER BY id")
            return [dict(row) for row in found.fetchall()]
        finally:
            connection.close()


# Interfaz funcional equivalente, por comodidad.
def check_publication_allowed(guard: PublicationGuard, short: dict) -> Decision:
    return guard.check_publication_allowed(short)


def reserve_publication(guard: PublicationGuard, short: dict) -> Decision:
    return guard.reserve_publication(short)


def confirm_publication(guard: PublicationGuard, reservation_id: int, youtube_id: str) -> None:
    guard.confirm_publication(reservation_id, youtube_id)


def release_publication(guard: PublicationGuard, reservation_id: int, reason: str = "") -> None:
    guard.release_publication(reservation_id, reason)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Estado de la guardia de publicación de YouTube.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--estado", action="store_true", help="Muestra kill switch, cupos y publicaciones de hoy.")
    parser.add_argument("--liberar", type=int, metavar="ID", help="Libera una reserva colgada (no confirmada).")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    guard = PublicationGuard(config, args.config)
    if args.liberar is not None:
        guard.release_publication(args.liberar, "liberada a mano")
        print(f"Reserva {args.liberar} liberada.")
        return 0
    settings, _error = guard.current_settings()
    today = local_day(_utc_now(), settings["zona_horaria"]).isoformat()
    killed = guard.kill_switch_reason()
    print(f"Base: {guard.db_path}")
    print(f"Kill switch: {killed or 'apagado'}")
    print(
        f"Cupos: {settings['max_por_dia']}/día, {settings['max_por_ejecucion']}/ejecución, "
        f"{settings['max_por_streamer_dia']}/streamer y día ({settings['zona_horaria']})"
    )
    print(f"Privacidad: {resolve_privacy(config)}")
    for row in guard.rows(today):
        print(f"  #{row['id']} {row['status']:9} @{row['streamer']} {row['youtube_id'] or ''} {row['reason'] or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
