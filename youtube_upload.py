"""Subida de los Shorts de Clipazos ES a YouTube con la API oficial (gratis).

Primera vez:  python youtube_upload.py --autorizar   (abre el navegador para elegir el canal)
Subir a mano: python youtube_upload.py output/clipazos/reporte_20261105.json
Probar sin subir: python youtube_upload.py output/clipazos/reporte_20261105.json --dry-run

Antes de cada subida se repiten las comprobaciones de derechos (rights.py) y de
calidad (qa.py) y la guardia de publicación (publication_guard.py) aparta el hueco
del cupo y relee el kill switch justo antes de llamar a la API.

Necesita credenciales/client_secret.json (cliente OAuth "App de escritorio" de Google Cloud
con YouTube Data API v3 activada). El token se guarda en credenciales/youtube_token.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

import publication_guard
import qa
import rights

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "clipazos.json"
CREDENTIALS_DIR = BASE_DIR / "credenciales"
CLIENT_SECRET = CREDENTIALS_DIR / "client_secret.json"
TOKEN_PATH = CREDENTIALS_DIR / "youtube_token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube"]
GAMING_CATEGORY = "20"
TITLE_LIMIT = 100
DESCRIPTION_LIMIT = 5000


def _scopes_ok(credentials) -> bool:
    granted = set(getattr(credentials, "scopes", None) or [])
    return set(SCOPES).issubset(granted)


def _credentials(interactive: bool):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    credentials = None
    if TOKEN_PATH.is_file():
        credentials = Credentials.from_authorized_user_file(str(TOKEN_PATH))
    if credentials and not _scopes_ok(credentials):
        credentials = None
    if credentials and credentials.valid:
        return credentials
    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except Exception as exc:  # noqa: BLE001 - token revocado o caducado
            if not interactive:
                raise RuntimeError(
                    f"el token de YouTube ya no vale ({exc}); ejecuta: python youtube_upload.py --autorizar"
                ) from exc
            credentials = None
    if not (credentials and credentials.valid):
        if not interactive:
            raise RuntimeError("falta autorizar YouTube; ejecuta: python youtube_upload.py --autorizar")
        if not CLIENT_SECRET.is_file():
            raise RuntimeError(f"no existe {CLIENT_SECRET}")
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
        credentials = flow.run_local_server(port=0, prompt="consent")
    CREDENTIALS_DIR.mkdir(exist_ok=True)
    TOKEN_PATH.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def is_ready() -> bool:
    return TOKEN_PATH.is_file()


def _metadata(short: dict, privacy: str) -> dict:
    tags = short.get("hashtags", [])
    title = short["titulo"]
    if "#shorts" not in title.casefold():
        title = f"{title} #Shorts"
    description = "\n\n".join([short.get("descripcion", ""), " ".join(tags + ["#Shorts"])])
    return {
        "snippet": {
            "title": title[:TITLE_LIMIT],
            "description": description[:DESCRIPTION_LIMIT],
            "tags": [tag.lstrip("#") for tag in tags],
            "categoryId": GAMING_CATEGORY,
            "defaultLanguage": "es",
            "defaultAudioLanguage": "es",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }


def upload_short(short: dict, privacy: str = "private") -> str:
    """Sube un Short del reporte y devuelve el id del vídeo en YouTube."""
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    video = Path(short["video"])
    if not video.is_file():
        raise RuntimeError(f"no existe el vídeo {video}")
    service = build("youtube", "v3", credentials=_credentials(interactive=False), cache_discovery=False)
    request = service.videos().insert(
        part="snippet,status",
        body=_metadata(short, privacy),
        media_body=MediaFileUpload(str(video), mimetype="video/mp4", chunksize=-1, resumable=True),
    )
    response = None
    while response is None:
        _status, response = request.next_chunk()
    return response["id"]


def delete_video(video_id: str) -> None:
    """Borra un vídeo del canal. Hace falta el permiso de gestionar YouTube."""
    from googleapiclient.discovery import build

    service = build("youtube", "v3", credentials=_credentials(interactive=False), cache_discovery=False)
    service.videos().delete(id=video_id).execute()


def _load_config(path: Path | None) -> dict:
    try:
        return json.loads(Path(path or CONFIG_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def final_checks(short: dict, config: dict) -> tuple[bool, str, dict]:
    """Derechos y QA justo antes de subir. Devuelve (ok, motivo, detalle)."""
    decision = rights.check_short(short, config)
    if not decision.allowed:
        return False, f"derechos: {decision.reason}", {"derechos": decision.to_dict()}
    video = Path(short.get("video") or "")
    result = qa.run_qa(video, config, video.with_suffix(".ass"))
    if not result.passed:
        return False, f"QA: {result.reason}", {"derechos": decision.to_dict(), "qa": result.to_dict()}
    return True, "", {"derechos": decision.to_dict(), "qa": result.to_dict()}


def upload_report(
    report_path: Path,
    privacy: str = "private",
    *,
    config: dict | None = None,
    config_path: Path | None = None,
    dry_run: bool = False,
    guard: publication_guard.PublicationGuard | None = None,
    uploader: Callable[[dict, str], str] | None = None,
) -> int:
    """Sube los Shorts del reporte que aún no tengan youtube_id. Devuelve cuántos subió.

    Con dry_run=True hace todas las comprobaciones (derechos, QA, cupos, kill
    switch) pero no llama a la API de YouTube ni escribe el reporte ni la base.
    """
    # Si se pasa la config ya cargada y ninguna ruta, el kill switch se lee de esa config.
    # Sin config, se lee clipazos.json y se relee del disco antes de cada subida.
    if config is None:
        config_path = Path(config_path) if config_path else CONFIG_PATH
        config = _load_config(config_path)
    elif config_path:
        config_path = Path(config_path)
    privacy = privacy if privacy in publication_guard.VALID_PRIVACY else publication_guard.DEFAULT_PRIVACY
    guard = guard or publication_guard.PublicationGuard(config, config_path, dry_run=dry_run)
    uploader = uploader or upload_short
    report = json.loads(report_path.read_text(encoding="utf-8"))
    uploaded = 0
    prefix = "  [dry-run] " if dry_run else "  "

    def save() -> None:
        if not dry_run:
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for short in report.get("shorts", []):
        if short.get("youtube_id") or short.get("no_subir"):
            continue
        name = short.get("video")
        ok, reason, detail = final_checks(short, config)
        if not ok:
            print(f"{prefix}No se sube {name}: {reason}", file=sys.stderr)
            if not dry_run:
                short["publicacion_bloqueada"] = reason
                short["comprobacion_final"] = detail
                save()
            continue
        decision = guard.reserve_publication(short)
        if not decision.allowed:
            print(f"{prefix}No se sube {name}: {decision.reason}", file=sys.stderr)
            if not dry_run:
                short["publicacion_bloqueada"] = decision.reason
                save()
            continue
        # Última lectura del kill switch, justo antes de la llamada a la API.
        killed = guard.kill_switch_reason()
        if killed:
            guard.release_publication(decision.reservation_id, killed)
            print(f"{prefix}No se sube {name}: {killed}", file=sys.stderr)
            continue
        if dry_run:
            print(f"{prefix}Se subiría a YouTube ({privacy}): {name}")
            uploaded += 1
            continue
        try:
            video_id = uploader(short, privacy)
        except Exception as exc:  # noqa: BLE001 - errores de red o de cuota de la API
            guard.release_publication(decision.reservation_id, f"falló la subida: {exc}")
            print(f"  Aviso: no se pudo subir {name} a YouTube ({exc})", file=sys.stderr)
            continue
        guard.confirm_publication(decision.reservation_id, video_id)
        short.pop("publicacion_bloqueada", None)
        short["youtube_id"] = video_id
        short["youtube_url"] = f"https://youtube.com/shorts/{video_id}"
        short["youtube_privacidad"] = privacy
        uploaded += 1
        print(f"  Subido a YouTube ({privacy}): {short['youtube_url']}")
        save()
    return uploaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sube los Shorts de Clipazos ES a YouTube.")
    parser.add_argument("reporte", nargs="?", type=Path, help="reporte_<fecha>.json de output/clipazos")
    parser.add_argument("--autorizar", action="store_true", help="Inicia sesión con el canal de YouTube.")
    parser.add_argument("--privacidad", choices=["private", "unlisted", "public"], default="private")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="clipazos.json con derechos, QA y cupos.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Hace todas las comprobaciones pero no llama a la API de YouTube."
    )
    args = parser.parse_args(argv)
    if args.autorizar:
        _credentials(interactive=True)
        print(f"YouTube autorizado. Token guardado en {TOKEN_PATH}")
        return 0
    if not args.reporte:
        parser.error("indica un reporte o usa --autorizar")
    upload_report(args.reporte, args.privacidad, config_path=args.config, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
