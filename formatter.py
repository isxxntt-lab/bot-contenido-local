"""Motor de reglas local: keywords, hashtags y plantilla de texto.

No usa modelos ni red. El costo de procesar un post es 0 dólares y 0 tokens.
"""

from __future__ import annotations

import re


# Orden de inserción = prioridad al recortar hashtags.
# La clave se busca como palabra completa, sin distinguir mayúsculas.
KEYWORD_HASHTAGS: dict[str, str] = {
    "open source": "#OpenSource",
    "machine learning": "#MachineLearning",
    "deep learning": "#DeepLearning",
    "artificial intelligence": "#InteligenciaArtificial",
    "inteligencia artificial": "#InteligenciaArtificial",
    "generative ai": "#IAGenerativa",
    "large language model": "#LLM",
    "neural network": "#RedesNeuronales",
    "hugging face": "#HuggingFace",
    "huggingface": "#HuggingFace",
    "sam altman": "#SamAltman",
    "elon musk": "#ElonMusk",
    "cybersecurity": "#Ciberseguridad",
    "data breach": "#Ciberseguridad",
    "openai": "#OpenAI",
    "chatgpt": "#ChatGPT",
    "anthropic": "#Anthropic",
    "claude": "#Claude",
    "deepmind": "#DeepMind",
    "gemini": "#Gemini",
    "google": "#Google",
    "microsoft": "#Microsoft",
    "copilot": "#Copilot",
    "apple": "#Apple",
    "nvidia": "#NVIDIA",
    "tesla": "#Tesla",
    "amazon": "#Amazon",
    "netflix": "#Netflix",
    "meta": "#Meta",
    "llama": "#Llama",
    "robot": "#Robotica",
    "robotics": "#Robotica",
    "startup": "#Startups",
    "chip": "#Chips",
    "gpu": "#Chips",
    "llm": "#LLM",
    "gpt": "#GPT",
    "hack": "#Ciberseguridad",
    "ai": "#IA",
}

# Hashtags de cierre si el texto no cubre el tema por sí solo.
BASE_HASHTAGS = ("#IA", "#Tecnologia")
MAX_HASHTAGS = 8
MAX_KEYWORD_TAGS = 6
SUMMARY_LIMIT = 320

# La primera regla que coincida elige el gancho. Es una plantilla, no un modelo.
TOPIC_HOOKS: list[tuple[tuple[str, ...], str]] = [
    (
        ("regulation", "regulator", "lawsuit", "bill", "safety", "ban", "govern"),
        "La seguridad y las reglas de la IA vuelven a mover el tablero.",
    ),
    (
        ("launch", "launches", "released", "releases", "rolling out", "iphone", "android"),
        "Un producto de IA acaba de dar un paso hacia el uso diario.",
    ),
    (
        ("research", "benchmark", "paper", "training", "weights", "model"),
        "Hay novedades en modelos, entrenamiento o investigación.",
    ),
    (
        ("ceo", "funding", "startup", "billion", "acquisition", "acquires", "revenue"),
        "El negocio alrededor de la IA se mueve otra vez.",
    ),
]
DEFAULT_HOOK = "Esto acaba de publicarse en el radar de tecnología e IA."

POST_TEMPLATE = (
    "{hook}\n"
    "\n"
    "{title}\n"
    "\n"
    "{summary}\n"
    "\n"
    "{source_line}"
    "{link}\n"
    "\n"
    "{hashtags}\n"
)

_KEYWORD_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(rf"\b{re.escape(keyword)}\b", re.IGNORECASE), keyword, tag)
    for keyword, tag in KEYWORD_HASHTAGS.items()
]

_TOPIC_RULES: list[tuple[re.Pattern[str], str]] = []
for keywords, hook in TOPIC_HOOKS:
    joined = "|".join(re.escape(word) for word in keywords)
    _TOPIC_RULES.append((re.compile(rf"\b(?:{joined})\b", re.IGNORECASE), hook))


def shorten(text: str, limit: int = SUMMARY_LIMIT) -> str:
    """Corta en el último espacio antes del límite para no partir una palabra."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(".,;: ")
    return f"{cut}..."


def assign_hashtags(text: str) -> tuple[list[str], list[str]]:
    """
    Busca keywords con límite de palabra y devuelve (hashtags, keywords halladas).

    'apple' no marca 'pineapple', y 'ai' no marca 'available'.
    """
    found_tags: list[str] = []
    matched: list[str] = []
    seen_tags: set[str] = set()

    for pattern, keyword, tag in _KEYWORD_RULES:
        if not pattern.search(text):
            continue
        matched.append(keyword)
        if tag not in seen_tags:
            seen_tags.add(tag)
            found_tags.append(tag)

    tags = found_tags[:MAX_KEYWORD_TAGS]
    for base in BASE_HASHTAGS:
        if base not in tags:
            tags.append(base)
    return tags[:MAX_HASHTAGS], matched


def pick_hook(text: str) -> str:
    """Elige una frase fija según el primer tema que aparezca en el texto."""
    for pattern, hook in _TOPIC_RULES:
        if pattern.search(text):
            return hook
    return DEFAULT_HOOK


def format_post(title: str, link: str, summary: str, source: str = "") -> dict:
    """
    Arma un post de texto plano a partir del título, el enlace y el resumen.

    source es opcional: el scraper lo rellena, pero la función no lo exige.
    """
    title = re.sub(r"\s+", " ", (title or "")).strip()
    link = (link or "").strip()
    summary = shorten(summary or "") or "Sin resumen disponible."
    source = re.sub(r"\s+", " ", (source or "")).strip()

    if not title or not link:
        raise ValueError("format_post necesita título y enlace.")

    haystack = f"{title}\n{summary}"
    hashtags, matched = assign_hashtags(haystack)
    hook = pick_hook(haystack)
    source_line = f"Fuente: {source}\n" if source else ""

    post = POST_TEMPLATE.format(
        hook=hook,
        title=title,
        summary=summary,
        source_line=source_line,
        link=link,
        hashtags=" ".join(hashtags),
    )

    return {
        "title": title,
        "link": link,
        "summary": summary,
        "source": source,
        "hook": hook,
        "hashtags": hashtags,
        "matched_keywords": matched,
        "post": post,
    }


if __name__ == "__main__":
    demo = format_post(
        title="Apple teams up with OpenAI on on-device models",
        link="https://example.com/apple-openai",
        summary="Apple is testing OpenAI models that run on the iPhone, without a cloud API.",
        source="Demo local",
    )
    print(demo["post"])
    print("Keywords:", ", ".join(demo["matched_keywords"]))
