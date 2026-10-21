"""Orquestador del bot de contenido local.

Flujo: RSS -> reglas de formatter.py -> consola y carpeta output/.
No envía el texto a ningún servicio externo.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from formatter import format_post
from scraper import fetch_latest_news


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


def build_records(articles: list[dict]) -> list[dict]:
    """Pasa cada noticia por el motor de reglas. Omite las que vengan rotas."""
    records: list[dict] = []
    for article in articles:
        try:
            record = format_post(
                title=article.get("title", ""),
                link=article.get("link", ""),
                summary=article.get("summary", ""),
                source=article.get("source", ""),
            )
        except ValueError as exc:
            print(f"Aviso: noticia omitida ({exc})", file=sys.stderr)
            continue

        record["published"] = article.get("published", "")
        records.append(record)
    return records


def save_results(records: list[dict], output_dir: Path) -> tuple[Path, Path]:
    """Guarda un JSON estructurado y un .txt listo para copiar."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.now().isoformat(timespec="seconds")

    payload = {
        "generated_at": generated_at,
        "processor": "reglas locales en formatter.py; sin API y sin tokens",
        "count": len(records),
        "posts": records,
    }

    json_path = output_dir / f"posts_{stamp}.json"
    txt_path = output_dir / f"posts_{stamp}.txt"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    blocks = [
        f"Generado: {generated_at}",
        f"Posts: {len(records)}",
        "Procesador: reglas locales, sin API",
    ]
    for index, record in enumerate(records, 1):
        blocks.append(f"\n{'=' * 60}\nPOST {index}\n{'=' * 60}\n")
        blocks.append(record["post"].rstrip())
        if record["matched_keywords"]:
            blocks.append(
                "\nKeywords: " + ", ".join(record["matched_keywords"])
            )
    txt_path.write_text("\n".join(blocks).rstrip() + "\n", encoding="utf-8")

    # Copia estable para abrir siempre el último resultado sin buscar la hora.
    (output_dir / "latest.json").write_text(json_path.read_text(encoding="utf-8"), encoding="utf-8")
    (output_dir / "latest.txt").write_text(txt_path.read_text(encoding="utf-8"), encoding="utf-8")
    return json_path, txt_path


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
    records = build_records(articles)
    if not records:
        print("Ninguna noticia pudo formatearse.")
        return 1

    json_path, txt_path = save_results(records, args.output_dir)

    print()
    for index, record in enumerate(records, 1):
        print(f"{'=' * 60}")
        print(f"POST {index}")
        print(f"{'=' * 60}")
        print(record["post"])

    print(f"JSON: {json_path}")
    print(f"TXT:  {txt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
