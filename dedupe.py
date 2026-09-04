import re
from urllib.parse import urlsplit, urlunsplit
from rapidfuzz.fuzz import token_set_ratio


def normalize_title(title: str) -> str:
    title = (title or "").lower()
    title = re.sub(r"\s+-\s+[^-]{2,60}$", "", title)
    title = re.sub(r"[^a-z0-9\s]", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def canonical_url(url: str) -> str:
    if not url:
        return ""
    try:
        p = urlsplit(url)
        # Drop tracking query strings/fragments. Google News redirect URLs are kept as-is
        # except for fragments because their path identifies the article.
        query = p.query if "news.google.com" in p.netloc else ""
        return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/"), query, ""))
    except Exception:
        return url


def dedupe_exact(items):
    seen = set()
    out = []
    for item in items:
        key = canonical_url(item.get("url", "")) or normalize_title(item.get("title", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def dedupe_near(items, threshold: int = 87):
    """Keep the higher-priority version of near-identical headlines."""
    ranked = sorted(
        items,
        key=lambda x: (int(x.get("priority", 0)), x.get("published_at", "")),
        reverse=True,
    )
    kept = []
    kept_titles = []
    for item in ranked:
        title = normalize_title(item.get("title", ""))
        if not title:
            continue
        if any(token_set_ratio(title, old) >= threshold for old in kept_titles):
            continue
        kept.append(item)
        kept_titles.append(title)
    return kept
