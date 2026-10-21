"""Extracción local de noticias desde feeds RSS públicos.

Solo descarga XML público con feedparser. No llama a modelos ni APIs de pago.
"""

from __future__ import annotations

import html
import re
import sys
from html.parser import HTMLParser

import feedparser


# Nombre fijo para que el origen quede claro aunque el feed no traiga título.
RSS_FEEDS = [
    {
        "name": "TechCrunch",
        "url": "https://techcrunch.com/category/artificial-intelligence/feed/",
    },
    {
        "name": "The Verge",
        "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    },
    {
        "name": "WIRED",
        "url": "https://www.wired.com/feed/tag/ai/latest/rss",
    },
]

# Frases que los propios medios añaden al final del resumen y no aportan al post.
_BOILERPLATE = re.compile(
    r"(?:The post .+? appeared first on .+?\.?|Continue reading\.?|Read more\.?)\s*$",
    re.IGNORECASE,
)

# Los RSS traen comillas tipográficas que en Windows se ven rotas en la consola.
_PUNCTUATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2026": "...",
    }
)


class _HTMLText(HTMLParser):
    """Convierte HTML de un resumen RSS en texto plano."""

    _SKIP_TAGS = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in self._SKIP_TAGS:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._SKIP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data:
            self._parts.append(data)

    def text(self) -> str:
        return " ".join(self._parts)


def clean_text(raw: str) -> str:
    """Quita HTML, entidades y espacios de más. Devuelve una sola línea limpia."""
    if not raw:
        return ""

    parser = _HTMLText()
    try:
        parser.feed(raw)
        parser.close()
        text = parser.text()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", raw)

    text = html.unescape(text).translate(_PUNCTUATION).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = _BOILERPLATE.sub("", text).strip()
    return text


def _raw_summary(entry) -> str:
    summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
    if summary:
        return summary
    content = getattr(entry, "content", None) or []
    if content:
        return content[0].get("value", "")
    return ""


def _entry_to_article(entry, source_name: str) -> dict | None:
    title = clean_text(getattr(entry, "title", ""))
    link = (getattr(entry, "link", "") or "").strip()
    if not title or not link:
        return None

    summary = clean_text(_raw_summary(entry)) or "Sin resumen disponible."
    published = (
        getattr(entry, "published", "")
        or getattr(entry, "updated", "")
        or ""
    ).strip()

    return {
        "source": source_name,
        "title": title,
        "link": link,
        "summary": summary,
        "published": published,
    }


def fetch_latest_news(limit_per_feed: int = 3) -> list[dict]:
    """
    Descarga los titulares más recientes de cada fuente RSS.

    Devuelve diccionarios con source, title, link, summary y published.
    Si una fuente falla, se avisa por stderr y se sigue con las demás.
    """
    if limit_per_feed < 1:
        raise ValueError("limit_per_feed debe ser al menos 1.")

    articles: list[dict] = []
    seen: set[str] = set()

    for feed_info in RSS_FEEDS:
        parsed = feedparser.parse(feed_info["url"])
        entries = list(getattr(parsed, "entries", []) or [])

        if not entries:
            reason = getattr(parsed, "bozo_exception", None)
            detail = f" ({reason})" if reason else ""
            print(
                f"Aviso: sin entradas en {feed_info['name']}{detail}",
                file=sys.stderr,
            )
            continue

        source_name = feed_info["name"]
        taken = 0
        for entry in entries:
            article = _entry_to_article(entry, source_name)
            if article is None:
                continue

            # La query string suele ser seguimiento; el enlace canónico basta para deduplicar.
            key = article["link"].split("?", 1)[0].rstrip("/").casefold()
            if key in seen:
                continue

            seen.add(key)
            articles.append(article)
            taken += 1
            if taken >= limit_per_feed:
                break

    return articles


if __name__ == "__main__":
    print("Buscando las últimas noticias de IA...")
    news = fetch_latest_news(limit_per_feed=2)
    if not news:
        print("No se obtuvo ninguna noticia.")
        raise SystemExit(1)

    for idx, item in enumerate(news, 1):
        print(f"\n[{idx}] {item['title']}")
        print(f"Fuente: {item['source']}")
        print(f"Enlace: {item['link']}")
        print(f"Resumen: {item['summary']}")
