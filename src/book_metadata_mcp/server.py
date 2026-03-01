#!/usr/bin/env python3
"""Book Metadata MCP Server — Multi-source book lookup for AI assistants.

Provides 5 tools over stdio transport:
  1. search_book      — Waterfall search: Google Books + Open Library
  2. get_cover        — Get best available cover image URL (largest first)
  3. get_metadata     — Full metadata: author, year, description, ISBN, subjects
  4. download_cover   — Download cover image bytes, save to disk
  5. bulk_search      — Search multiple books at once (batch mode)

Source priority:
  1. Google Books API  — Publisher-verified data, 6 cover sizes up to ~1280px
  2. Open Library API  — Free, comprehensive metadata, community-maintained

Both sources are free and require no API keys for basic usage.

Rate limiting:
  Google Books allows ~1,000 requests/day without an API key. For bulk usage,
  a circuit breaker automatically skips Google after repeated 429 errors and
  falls back to Open Library, retrying Google after a cooldown period.
  Set GOOGLE_BOOKS_API_KEY for higher quotas.
"""

import io
import json
import logging
import os
import re
import time
from pathlib import Path

import requests
from mcp.server.fastmcp import FastMCP

# ── Configuration ─────────────────────────────────────────────────────────────

GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"
GOOGLE_API_KEY = os.environ.get("GOOGLE_BOOKS_API_KEY", "")

OPENLIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"
OPENLIBRARY_WORKS_URL = "https://openlibrary.org/works"
OPENLIBRARY_COVERS_URL = "https://covers.openlibrary.org/b"

# Rate limiting (seconds between requests)
GOOGLE_DELAY = float(os.environ.get("GOOGLE_DELAY", "0.5"))
OPENLIBRARY_DELAY = float(os.environ.get("OPENLIBRARY_DELAY", "0.35"))

# Circuit breaker for Google Books 429 rate limiting.
# After GOOGLE_CB_THRESHOLD consecutive 429 failures, Google is skipped for
# GOOGLE_CB_COOLDOWN seconds. This prevents wasting ~7s per book on retries
# that will fail during bulk operations without an API key.
GOOGLE_CB_THRESHOLD = int(os.environ.get("GOOGLE_CB_THRESHOLD", "3"))
GOOGLE_CB_COOLDOWN = float(os.environ.get("GOOGLE_CB_COOLDOWN", "60"))

_google_cb = {
    "consecutive_429s": 0,
    "cooldown_until": 0.0,
    "total_trips": 0,
}

# HTTP session
SESSION = requests.Session()
SESSION.headers["User-Agent"] = os.environ.get(
    "BOOK_MCP_USER_AGENT",
    "BookMetadataMCP/0.1.3 (https://github.com/vetnet183/book-metadata-mcp)",
)
SESSION.timeout = 15

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── MCP Server ────────────────────────────────────────────────────────────────

mcp = FastMCP("Book Metadata")

# ── Google Books ──────────────────────────────────────────────────────────────


def _google_search(title: str, author: str = "", isbn: str = "", limit: int = 3) -> list[dict]:
    """Search Google Books API. Returns list of normalized results.

    Uses a circuit breaker to avoid wasting time on retries when rate-limited.
    After GOOGLE_CB_THRESHOLD consecutive 429 responses, Google is skipped for
    GOOGLE_CB_COOLDOWN seconds and Open Library handles requests alone.
    """
    # Circuit breaker: skip Google if in cooldown
    now = time.time()
    if _google_cb["cooldown_until"] > now:
        return []

    parts = []
    if isbn:
        parts.append(f"isbn:{isbn}")
    if title:
        parts.append(f"intitle:{title}")
    if author:
        parts.append(f"inauthor:{author}")

    if not parts:
        return []

    query = "+".join(parts)
    params: dict = {"q": query, "maxResults": limit, "printType": "books"}
    if GOOGLE_API_KEY:
        params["key"] = GOOGLE_API_KEY

    data = None
    got_429 = False
    for attempt in range(3):
        try:
            resp = SESSION.get(GOOGLE_BOOKS_URL, params=params)
            if resp.status_code == 429:
                got_429 = True
                wait = 2 ** attempt
                log.info("Google Books 429 — retrying in %ds", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            # Success — reset circuit breaker
            if _google_cb["consecutive_429s"] > 0:
                _google_cb["consecutive_429s"] = 0
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(1)
                continue
            log.warning("Google Books search failed: %s", e)
            return []

    if data is None:
        # All retries exhausted (likely persistent 429)
        if got_429:
            _google_cb["consecutive_429s"] += 1
            if _google_cb["consecutive_429s"] >= GOOGLE_CB_THRESHOLD:
                _google_cb["cooldown_until"] = time.time() + GOOGLE_CB_COOLDOWN
                _google_cb["total_trips"] += 1
                log.warning(
                    "Google Books circuit breaker OPEN — skipping for %ds "
                    "(trip #%d, %d consecutive 429s). Using Open Library only.",
                    GOOGLE_CB_COOLDOWN, _google_cb["total_trips"],
                    _google_cb["consecutive_429s"],
                )
                _google_cb["consecutive_429s"] = 0
        return []

    results = []
    for item in data.get("items", []):
        info = item.get("volumeInfo", {})

        # Extract ISBNs
        isbns = {}
        for ident in info.get("industryIdentifiers", []):
            isbns[ident.get("type", "")] = ident.get("identifier", "")

        # Get image links
        image_links = info.get("imageLinks", {})

        # Build cover URL with zoom trick for largest available
        cover_url = None
        thumb = image_links.get("thumbnail") or image_links.get("smallThumbnail")
        if thumb:
            cover_url = re.sub(r"zoom=\d", "zoom=0", thumb)
            cover_url = cover_url.replace("http://", "https://")

        results.append({
            "source": "google_books",
            "google_id": item.get("id"),
            "title": info.get("title", ""),
            "subtitle": info.get("subtitle"),
            "authors": info.get("authors", []),
            "published_date": info.get("publishedDate"),
            "description": info.get("description"),
            "page_count": info.get("pageCount"),
            "categories": info.get("categories", []),
            "language": info.get("language"),
            "isbn_13": isbns.get("ISBN_13"),
            "isbn_10": isbns.get("ISBN_10"),
            "cover_url": cover_url,
            "thumbnail": image_links.get("thumbnail"),
            "preview_link": info.get("previewLink"),
        })

    time.sleep(GOOGLE_DELAY)
    return results


def _google_get_full_covers(google_id: str) -> dict:
    """Fetch full volume detail to get all 6 cover sizes. Respects circuit breaker."""
    # Circuit breaker: skip if in cooldown
    if _google_cb["cooldown_until"] > time.time():
        return {}

    params: dict = {}
    if GOOGLE_API_KEY:
        params["key"] = GOOGLE_API_KEY

    got_429 = False
    for attempt in range(3):
        try:
            resp = SESSION.get(f"{GOOGLE_BOOKS_URL}/{google_id}", params=params)
            if resp.status_code == 429:
                got_429 = True
                wait = 2 ** attempt
                log.info("Google Books 429 — retrying in %ds (attempt %d)", wait, attempt + 1)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            if _google_cb["consecutive_429s"] > 0:
                _google_cb["consecutive_429s"] = 0
            data = resp.json()
            return data.get("volumeInfo", {}).get("imageLinks", {})
        except Exception as e:
            if attempt < 2:
                time.sleep(1)
                continue
            log.warning("Google Books detail fetch failed for %s: %s", google_id, e)
            return {}

    if got_429:
        _google_cb["consecutive_429s"] += 1
        if _google_cb["consecutive_429s"] >= GOOGLE_CB_THRESHOLD:
            _google_cb["cooldown_until"] = time.time() + GOOGLE_CB_COOLDOWN
            _google_cb["total_trips"] += 1
            log.warning(
                "Google Books circuit breaker OPEN — skipping for %ds (trip #%d)",
                GOOGLE_CB_COOLDOWN, _google_cb["total_trips"],
            )
            _google_cb["consecutive_429s"] = 0
    return {}


# ── Open Library ──────────────────────────────────────────────────────────────


def _openlibrary_search(title: str, author: str = "", isbn: str = "", limit: int = 3) -> list[dict]:
    """Search Open Library API. Returns list of normalized results."""
    params: dict = {"limit": limit}
    fields = "key,title,author_name,author_key,first_publish_year,cover_i,isbn,edition_count,number_of_pages_median,subject"
    params["fields"] = fields

    if isbn:
        params["isbn"] = isbn
    elif title and author:
        params["title"] = title
        params["author"] = author
    elif title:
        params["title"] = title
    else:
        return []

    try:
        resp = SESSION.get(OPENLIBRARY_SEARCH_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning("Open Library search failed: %s", e)
        return []

    results = []
    for doc in data.get("docs", []):
        cover_id = doc.get("cover_i")
        isbn_list = doc.get("isbn", [])
        work_key = doc.get("key", "")

        cover_url = None
        if cover_id:
            cover_url = f"{OPENLIBRARY_COVERS_URL}/id/{cover_id}-L.jpg"

        first_year = doc.get("first_publish_year")

        results.append({
            "source": "open_library",
            "work_key": work_key,
            "title": doc.get("title", ""),
            "authors": doc.get("author_name", []),
            "published_date": str(first_year) if first_year else None,
            "description": None,
            "page_count": doc.get("number_of_pages_median"),
            "categories": (doc.get("subject", []) or [])[:10],
            "isbn_13": next((i for i in isbn_list if len(i) == 13 and i.isdigit()), None),
            "isbn_10": next((i for i in isbn_list if len(i) == 10), None),
            "cover_url": cover_url,
            "cover_id": cover_id,
            "edition_count": doc.get("edition_count"),
            "openlibrary_url": f"https://openlibrary.org{work_key}" if work_key else None,
        })

    time.sleep(OPENLIBRARY_DELAY)
    return results


def _openlibrary_get_description(work_key: str) -> str | None:
    """Fetch work description from Open Library."""
    if not work_key:
        return None
    try:
        resp = SESSION.get(f"https://openlibrary.org{work_key}.json")
        resp.raise_for_status()
        work = resp.json()
        desc = work.get("description")
        if isinstance(desc, dict):
            return desc.get("value")
        elif isinstance(desc, str):
            return desc
    except Exception:
        pass
    time.sleep(OPENLIBRARY_DELAY)
    return None


# ── Matching & Scoring ────────────────────────────────────────────────────────


def _normalize(s: str) -> str:
    """Normalize string for comparison."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _title_overlap(query_title: str, result_title: str) -> float:
    """Calculate word overlap ratio between query title and result title."""
    q_words = set(_normalize(query_title).split()) if " " in query_title else {_normalize(query_title)}
    r_words = set(_normalize(result_title).split()) if " " in result_title else {_normalize(result_title)}

    q_norm = _normalize(query_title)
    r_norm = _normalize(result_title)

    if q_norm == r_norm:
        return 1.0
    if q_norm in r_norm or r_norm in q_norm:
        return 0.8

    if not q_words or not r_words:
        return 0.0
    overlap = len(q_words & r_words)
    return overlap / max(len(q_words), 1)


def _is_study_guide(result: dict) -> bool:
    """Detect study guides, companion books, and derivative works."""
    title = (result.get("title") or "").lower()
    desc = (result.get("description") or "").lower()
    authors = [a.lower() for a in result.get("authors", [])]

    guide_authors = {"cliffsnotes", "sparknotes", "bookrags", "shmoop",
                     "gradesaver", "litcharts", "createspace"}
    for a in authors:
        if any(g in _normalize(a) for g in guide_authors):
            return True

    guide_title_words = ["study guide", "cliffsnotes", "cliff's notes",
                         "sparknotes", "companion", "for dummies",
                         "barron's", "maxnotes", "reader's guide",
                         "critical companion", "bookrags", "analysis of"]
    for phrase in guide_title_words:
        if phrase in title:
            return True

    guide_desc = ["study guide", "get your \"a\"", "get your a in gear",
                  "cliff'?s? notes", "spark notes", "book summary",
                  "chapter summaries", "exam prep"]
    for phrase in guide_desc:
        if phrase in desc:
            return True

    return False


def _score_result(result: dict, query_title: str, query_author: str) -> float:
    """Score a result based on match quality. Higher = better."""
    score = 0.0

    # Title match (0-50 points)
    title_sim = _title_overlap(query_title, result.get("title", ""))
    score += title_sim * 50

    # Author match (0-30 points)
    if query_author:
        author_norm = _normalize(query_author)
        for a in result.get("authors", []):
            if author_norm == _normalize(a):
                score += 30
                break
            elif author_norm in _normalize(a) or _normalize(a) in author_norm:
                score += 20
                break

    # Has cover (10 points)
    if result.get("cover_url"):
        score += 10

    # Has description (5 points)
    if result.get("description"):
        score += 5

    # Has ISBN (5 points)
    if result.get("isbn_13") or result.get("isbn_10"):
        score += 5

    # Source bonus: Google Books gets slight preference for cover quality
    if result.get("source") == "google_books":
        score += 2

    # PENALTY: Study guides, companion books, derivative works
    if _is_study_guide(result):
        score -= 60

    # PENALTY: Title significantly longer than query (likely derivative work)
    result_title = result.get("title", "")
    if query_title and len(result_title) > len(query_title) * 2.5 and len(query_title) < 30:
        score -= 15

    return score


def _extract_year(date_str: str | None) -> int | None:
    """Extract year from various date formats."""
    if not date_str:
        return None
    match = re.search(r"\b(\d{4})\b", date_str)
    if match:
        year = int(match.group(1))
        if 1000 <= year <= 2030:
            return year
    return None


def _best_year(results: list[dict], best: dict) -> int | None:
    """Pick the best publication year across sources.

    Open Library's first_publish_year is usually the original publication date.
    Google Books often returns modern reprint/edition dates.
    We take the earliest credible year across ALL results from both sources.
    """
    years = []
    for r in results:
        y = _extract_year(r.get("published_date"))
        if y:
            years.append(y)
    return min(years) if years else None


# ── Waterfall Search Engine ───────────────────────────────────────────────────


def _waterfall_search(
    title: str,
    author: str = "",
    isbn: str = "",
    limit: int = 3,
) -> list[dict]:
    """Search across all sources. Returns results sorted by match quality."""
    all_results = []

    google_results = _google_search(title, author, isbn, limit=limit)
    all_results.extend(google_results)

    ol_results = _openlibrary_search(title, author, isbn, limit=limit)
    all_results.extend(ol_results)

    for r in all_results:
        r["_score"] = _score_result(r, title, author)

    all_results.sort(key=lambda x: x.get("_score", 0), reverse=True)
    return all_results


# ── MCP Tools ─────────────────────────────────────────────────────────────────


@mcp.tool()
def search_book(
    title: str,
    author: str = "",
    isbn: str = "",
) -> str:
    """Search for a book across Google Books and Open Library.

    Waterfall strategy: queries both sources, scores results by
    title/author match quality, and returns the best matches.

    Google Books provides publisher-verified data and high-res covers.
    Open Library provides comprehensive metadata and community curation.

    Args:
        title: Book title to search for (e.g. 'The Great Gatsby')
        author: Author name for better matching (e.g. 'F. Scott Fitzgerald')
        isbn: ISBN-10 or ISBN-13 for exact matching
    """
    results = _waterfall_search(title, author, isbn, limit=5)

    if not results:
        return json.dumps({"error": f"No results found for '{title}' by '{author}'"})

    output = []
    for r in results[:5]:
        entry = {k: v for k, v in r.items() if not k.startswith("_")}
        entry["match_score"] = round(r.get("_score", 0), 1)
        output.append(entry)

    return json.dumps({
        "query": {"title": title, "author": author, "isbn": isbn},
        "result_count": len(output),
        "results": output,
    }, indent=2)


@mcp.tool()
def get_cover(
    title: str,
    author: str = "",
    isbn: str = "",
    prefer_source: str = "google",
) -> str:
    """Get the best available cover image URL for a book.

    Returns URLs for the largest available cover images from each source.
    Google Books covers go up to ~1280px wide; Open Library covers are L size.

    Args:
        title: Book title
        author: Author name (recommended for accuracy)
        isbn: ISBN for exact matching
        prefer_source: 'google' (default, best quality) or 'openlibrary'
    """
    results = _waterfall_search(title, author, isbn, limit=3)

    if not results:
        return json.dumps({"error": f"No cover found for '{title}'"})

    covers = []
    for r in results:
        if r.get("cover_url"):
            cover_entry = {
                "source": r["source"],
                "url": r["cover_url"],
                "title": r.get("title"),
                "authors": r.get("authors", []),
                "match_score": round(r.get("_score", 0), 1),
            }

            if r["source"] == "google_books" and r.get("google_id"):
                full_links = _google_get_full_covers(r["google_id"])
                if full_links:
                    for size in ["extraLarge", "large", "medium"]:
                        if size in full_links:
                            url = full_links[size].replace("http://", "https://")
                            cover_entry["url"] = url
                            cover_entry["size"] = size
                            break
                    cover_entry["all_sizes"] = {
                        k: v.replace("http://", "https://")
                        for k, v in full_links.items()
                    }

            covers.append(cover_entry)

    if prefer_source == "google":
        covers.sort(key=lambda c: (c["source"] != "google_books", -c["match_score"]))
    else:
        covers.sort(key=lambda c: (c["source"] != "open_library", -c["match_score"]))

    return json.dumps({
        "query": {"title": title, "author": author},
        "covers": covers,
        "best_cover": covers[0] if covers else None,
    }, indent=2)


@mcp.tool()
def get_metadata(
    title: str,
    author: str = "",
    isbn: str = "",
) -> str:
    """Get comprehensive book metadata from the best available source.

    Returns a merged metadata record combining the best data from
    Google Books and Open Library. Includes description, publication
    year, ISBNs, subjects, and page count.

    For publication year, the server picks the earliest credible year
    across sources — Open Library tracks original publication dates
    while Google Books often returns modern reprint dates.

    Args:
        title: Book title
        author: Author name (recommended for accuracy)
        isbn: ISBN for exact matching
    """
    results = _waterfall_search(title, author, isbn, limit=3)

    if not results:
        return json.dumps({"error": f"No metadata found for '{title}'"})

    best = results[0]

    # Enrich with Open Library description if needed
    if not best.get("description") and best.get("source") == "google_books":
        for r in results:
            if r.get("source") == "open_library" and r.get("work_key"):
                desc = _openlibrary_get_description(r["work_key"])
                if desc:
                    best["ol_description"] = desc
                break

    if not best.get("description") and best.get("work_key"):
        desc = _openlibrary_get_description(best["work_key"])
        if desc:
            best["description"] = desc

    metadata = {
        "title": best.get("title"),
        "authors": best.get("authors", []),
        "publish_year": _best_year(results, best),
        "published_date": best.get("published_date"),
        "description": best.get("description") or best.get("ol_description"),
        "page_count": best.get("page_count"),
        "isbn_13": best.get("isbn_13"),
        "isbn_10": best.get("isbn_10"),
        "categories": best.get("categories", []),
        "language": best.get("language"),
        "cover_url": best.get("cover_url"),
        "source": best.get("source"),
        "match_score": round(best.get("_score", 0), 1),
    }

    # Supplementary data from other sources
    supplementary = {}
    for r in results[1:3]:
        src = r.get("source")
        if src and src != best.get("source"):
            supp = {}
            if r.get("description") and not metadata["description"]:
                supp["description"] = r["description"]
            if r.get("isbn_13") and not metadata["isbn_13"]:
                supp["isbn_13"] = r["isbn_13"]
            if r.get("categories") and not metadata["categories"]:
                supp["categories"] = r["categories"]
            if r.get("cover_url") and not metadata["cover_url"]:
                supp["cover_url"] = r["cover_url"]
            if supp:
                supplementary[src] = supp

    if supplementary:
        metadata["supplementary"] = supplementary

    return json.dumps(metadata, indent=2)


@mcp.tool()
def download_cover(
    title: str,
    author: str = "",
    isbn: str = "",
    save_path: str = "",
) -> str:
    """Download the best available cover image and save to disk.

    Searches for the book, finds the highest quality cover,
    downloads it, and saves as JPEG. Returns the file path and
    image dimensions.

    Args:
        title: Book title
        author: Author name (recommended)
        isbn: ISBN for exact matching
        save_path: Full path to save the image (e.g. '/tmp/cover.jpg').
                   If empty, saves to /tmp/book_cover_{title_slug}.jpg
    """
    try:
        from PIL import Image
    except ImportError:
        return json.dumps({
            "error": "Pillow is required for download_cover. Install with: pip install Pillow"
        })

    cover_json = get_cover(title, author, isbn)
    cover_data = json.loads(cover_json)

    best = cover_data.get("best_cover")
    if not best:
        return json.dumps({"error": f"No cover found for '{title}'"})

    url = best["url"]

    try:
        resp = SESSION.get(url, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        return json.dumps({"error": f"Download failed: {e}", "url": url})

    content_type = resp.headers.get("Content-Type", "")
    if "image" not in content_type and len(resp.content) < 1000:
        return json.dumps({
            "error": "Downloaded content is not an image",
            "content_type": content_type,
            "size": len(resp.content),
        })

    try:
        img = Image.open(io.BytesIO(resp.content))
        width, height = img.size
    except Exception as e:
        return json.dumps({"error": f"Invalid image data: {e}"})

    if not save_path:
        slug = re.sub(r"[^a-z0-9]+", "_", title.lower())[:50]
        save_path = f"/tmp/book_cover_{slug}.jpg"

    out_path = Path(save_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(out_path, format="JPEG", quality=95, optimize=True)

    return json.dumps({
        "saved": str(out_path),
        "width": width,
        "height": height,
        "file_size_kb": round(out_path.stat().st_size / 1024, 1),
        "source": best["source"],
        "original_url": url,
        "title_matched": best.get("title"),
        "authors_matched": best.get("authors", []),
    }, indent=2)


@mcp.tool()
def bulk_search(
    books: str,
) -> str:
    """Search for multiple books at once.

    Takes a JSON array of {title, author} objects and returns
    the best match for each. Useful for batch metadata lookups.

    Rate-limited to respect API quotas.

    Args:
        books: JSON array of objects with 'title' and optional 'author' fields.
               Example: [{"title": "1984", "author": "Orwell"}, {"title": "Dune"}]
    """
    try:
        book_list = json.loads(books)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON: {e}"})

    if not isinstance(book_list, list):
        return json.dumps({"error": "Expected a JSON array of {title, author} objects"})

    if len(book_list) > 20:
        return json.dumps({"error": f"Max 20 books per batch, got {len(book_list)}"})

    results = []
    for i, book in enumerate(book_list):
        t = book.get("title", "")
        a = book.get("author", "")
        book_isbn = book.get("isbn", "")

        if not t and not book_isbn:
            results.append({"query": book, "error": "No title or ISBN provided"})
            continue

        matches = _waterfall_search(t, a, book_isbn, limit=1)
        if matches:
            best = matches[0]
            results.append({
                "query": {"title": t, "author": a},
                "match": {
                    "title": best.get("title"),
                    "authors": best.get("authors", []),
                    "publish_year": _best_year(matches, best),
                    "isbn_13": best.get("isbn_13"),
                    "cover_url": best.get("cover_url"),
                    "source": best.get("source"),
                    "match_score": round(best.get("_score", 0), 1),
                },
            })
        else:
            results.append({"query": {"title": t, "author": a}, "match": None})

        if i < len(book_list) - 1:
            time.sleep(0.5)

    return json.dumps({
        "total": len(book_list),
        "found": sum(1 for r in results if r.get("match")),
        "results": results,
    }, indent=2)


# ── Entry Point ───────────────────────────────────────────────────────────────


def main():
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
