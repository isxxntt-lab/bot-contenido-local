"""Subida de los Shorts de Clipazos ES a YouTube con la API oficial (gratis).

Primera vez:  python youtube_upload.py --autorizar   (abre el navegador para elegir el canal)
Subir a mano: python youtube_upload.py output/clipazos/reporte_20261105.json

Necesita credenciales/client_secret.json (cliente OAuth "App de escritorio" de Google Cloud
con YouTube Data API v3 activada). El token se guarda en credenciales/youtube_token.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_DIR = BASE_DIR / "credenciales"
CLIENT_SECRET = CREDENTIALS_DIR / "client_secret.json"
TOKEN_PATH = CREDENTIALS_DIR / "youtube_token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
GAMING_CATEGORY = "20"
TITLE_LIMIT = 100
DESCRIPTION_LIMIT = 5000


def _credentials(interactive: bool):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    credentials = None
    if TOKEN_PATH.is_file():
        credentials = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
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


def upload_report(report_path: Path, privacy: str = "private") -> int:
    """Sube los Shorts del reporte que aún no tengan youtube_id. Devuelve cuántos subió."""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    uploaded = 0
    for short in report.get("shorts", []):
        if short.get("youtube_id"):
            continue
        try:
            video_id = upload_short(short, privacy)
        except Exception as exc:  # noqa: BLE001 - errores de red o de cuota de la API
            print(f"  Aviso: no se pudo subir {short.get('video')} a YouTube ({exc})", file=sys.stderr)
            continue
        short["youtube_id"] = video_id
        short["youtube_url"] = f"https://youtube.com/shorts/{video_id}"
        short["youtube_privacidad"] = privacy
        uploaded += 1
        print(f"  Subido a YouTube ({privacy}): {short['youtube_url']}")
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return uploaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sube los Shorts de Clipazos ES a YouTube.")
    parser.add_argument("reporte", nargs="?", type=Path, help="reporte_<fecha>.json de output/clipazos")
    parser.add_argument("--autorizar", action="store_true", help="Inicia sesión con el canal de YouTube.")
    parser.add_argument("--privacidad", choices=["private", "unlisted", "public"], default="private")
    args = parser.parse_args(argv)
    if args.autorizar:
        _credentials(interactive=True)
        print(f"YouTube autorizado. Token guardado en {TOKEN_PATH}")
        return 0
    if not args.reporte:
        parser.error("indica un reporte o usa --autorizar")
    upload_report(args.reporte, args.privacidad)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
