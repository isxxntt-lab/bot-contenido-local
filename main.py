"""Orquestador del bot de contenido local.

Flujo: RSS -> reglas de formatter.py -> vídeo.

Render híbrido: si N8N_WEBHOOK_URL está definido, cada noticia se envía al
workflow de n8n (b-roll de Pexels, render con JSON2Video y aviso por Telegram).
Si no lo está, se usa el montaje local de video_editor.py con edge-tts,
sin clave y sin cargo.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from formatter import format_post
from n8n_client import N8NClient, N8NError, construir_payload
from scraper import fetch_latest_news
from video_editor import LATEST_VIDEO, render_news_video, select_article


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"


def _utf8_stdio() -> None:
    """Evita que la consola de Windows sustituya tildes y eñes."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except Exception:
            pass


def process_articles(articles: list[dict]) -> list[dict]:
    """Formatea cada noticia y la muestra en consola. Omite las que vengan rotas."""
    records: list[dict] = []
    total = len(articles)

    for index, article in enumerate(articles, 1):
        try:
            record = format_post(
                title=article.get("title", ""),
                link=article.get("link", ""),
                summary=article.get("summary", ""),
                source=article.get("source", ""),
            )
        except ValueError as exc:
            print(f"Aviso: noticia {index} omitida ({exc})", file=sys.stderr)
            continue

        record["published"] = article.get("published", "")
        records.append(record)

        print(f"\n{'=' * 60}")
        print(f"POST {len(records)} de {total}")
        print(f"{'=' * 60}")
        print(record["post"], end="" if record["post"].endswith("\n") else "\n")

    return records


def save_posts(records: list[dict], output_dir: Path) -> tuple[Path, Path]:
    """Crea output/ si hace falta y guarda posts_YYYYMMDD.json y posts_YYYYMMDD.txt."""
    output_dir.mkdir(parents=True, exist_ok=True)

    generated_at = datetime.now().isoformat(timespec="seconds")
    stamp = datetime.now().strftime("%Y%m%d")
    json_path = output_dir / f"posts_{stamp}.json"
    txt_path = output_dir / f"posts_{stamp}.txt"

    payload = {
        "generated_at": generated_at,
        "processor": "reglas locales en formatter.py; sin API y sin tokens",
        "count": len(records),
        "posts": records,
    }
    json_text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    blocks = [
        f"Generado: {generated_at}",
        f"Posts: {len(records)}",
        "Procesador: reglas locales, sin API",
    ]
    for index, record in enumerate(records, 1):
        blocks.append(f"\n{'=' * 60}\nPOST {index}\n{'=' * 60}\n")
        blocks.append(record["post"].rstrip())
        if record.get("matched_keywords"):
            blocks.append("\nKeywords: " + ", ".join(record["matched_keywords"]))
    txt_text = "\n".join(blocks).rstrip() + "\n"

    json_path.write_text(json_text, encoding="utf-8")
    txt_path.write_text(txt_text, encoding="utf-8")
    return json_path, txt_path


def enviar_render_n8n(records: list[dict]) -> int:
    """Envía cada noticia al workflow de n8n y devuelve cuántas se aceptaron.

    Un envío fallido no corta el flujo: se avisa por stderr y se continúa con
    el resto. n8n responde en el acto; el render llega después por Telegram.
    """
    aceptados = 0
    cliente = N8NClient()
    for record in records:
        guion = (
            f"{record['hook']} {record['title']}. {record['summary']} "
            "Sígueme para más noticias de IA."
        )
        payload = construir_payload(
            noticia={
                "titulo": record["title"],
                "fuente": record.get("source", ""),
                "url": record["link"],
                "resumen": record["summary"],
            },
            guion=guion,
            hashtags=record["hashtags"],
            hook=record["hook"],
            broll_keywords=record.get("matched_keywords") or ["inteligencia artificial"],
        )
        try:
            cliente.enviar(payload)
            aceptados += 1
        except N8NError as exc:
            print(f"Aviso: n8n rechazó '{record['title']}' ({exc})", file=sys.stderr)
    return aceptados


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bot de contenido local: RSS de IA y posts por reglas, sin APIs de pago.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=3,
        help="Cuántas noticias tomar de cada fuente RSS (por defecto: 3).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help="Carpeta donde se guardan el JSON y el TXT.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    args = parse_args(argv)
    if args.limit < 1:
        print("El límite debe ser al menos 1.", file=sys.stderr)
        return 1

    print("Descargando feeds RSS...")
    articles = fetch_latest_news(limit_per_feed=args.limit)
    if not articles:
        print("No se obtuvo ninguna noticia. Revisa la conexión y vuelve a lanzar el bot.")
        return 1

    print(f"Noticias útiles: {len(articles)}. Aplicando reglas locales...")
    records = process_articles(articles)
    if not records:
        print("Ninguna noticia pudo formatearse.")
        return 1

    json_path, txt_path = save_posts(records, args.output_dir)
    print(f"\nPosts guardados: {len(records)}")
    print(f"JSON: {json_path}")
    print(f"TXT:  {txt_path}")

    if os.getenv("N8N_WEBHOOK_URL"):
        print("\nRender híbrido activo: enviando noticias a n8n...")
        aceptados = enviar_render_n8n(records)
        print(f"Trabajos aceptados por n8n: {aceptados} de {len(records)}.")
        print("Los vídeos llegan por Telegram cuando termine cada render.")
        return 0

    article = select_article(records)
    video_path = args.output_dir / LATEST_VIDEO.name
    print(f"\nMontando vídeo de: {article.get('title', '')}")
    try:
        rendered = render_news_video(article, video_path)
    except Exception as exc:
        print(f"No se pudo generar el vídeo: {exc}", file=sys.stderr)
        return 1

    print(f"Vídeo: {rendered['video']} ({rendered['video'].stat().st_size} bytes)")
    print(f"Audio: {rendered['audio']}")
    print(f"Palabras sincronizadas: {rendered['cues']}")
    print(f"Fondo: {rendered['background'] or 'degradado generado'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
