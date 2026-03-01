# book-metadata-mcp

<!-- mcp-name: io.github.vetnet183/book-metadata-mcp -->

An MCP server that searches **Google Books** and **Open Library** to find book metadata, cover art, and publication info. Built for use with Claude Code, Claude Desktop, and any MCP-compatible client.

## Features

- **Multi-source search** — Queries Google Books and Open Library simultaneously, scores results by match quality
- **High-res covers** — Google Books covers up to ~1280px wide (6 sizes), Open Library L-size covers
- **Smart scoring** — Title/author matching with study guide detection (CliffsNotes, SparkNotes automatically deprioritized)
- **Accurate publication years** — Cross-references both sources, picks the earliest credible year (avoids modern reprint dates)
- **Batch mode** — Search up to 20 books at once
- **No API keys required** — Works out of the box with free tiers

## Tools

| Tool | Description |
|------|-------------|
| `search_book` | Search by title, author, or ISBN. Returns scored results from both sources. |
| `get_cover` | Find the best available cover image URL (prioritizes largest size). |
| `get_metadata` | Get merged metadata: author, year, description, ISBN, subjects, page count. |
| `download_cover` | Download cover image and save as JPEG. Requires `Pillow`. |
| `bulk_search` | Search multiple books at once (up to 20 per batch). |

## Installation

### Claude Code

```bash
claude mcp add book-metadata -- uvx book-metadata-mcp
```

### Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "book-metadata": {
      "command": "uvx",
      "args": ["book-metadata-mcp"]
    }
  }
}
```

### From source

```bash
git clone https://github.com/colonylibrary/book-metadata-mcp.git
cd book-metadata-mcp
pip install -e ".[covers]"
```

Then register with Claude Code:

```bash
claude mcp add book-metadata -- book-metadata-mcp
```

## Usage Examples

Once installed, the tools are available to your AI assistant:

> "Search for The Great Gatsby by F. Scott Fitzgerald"

```json
{
  "query": {"title": "The Great Gatsby", "author": "F. Scott Fitzgerald"},
  "result_count": 5,
  "results": [
    {
      "source": "open_library",
      "title": "The Great Gatsby",
      "authors": ["F. Scott Fitzgerald"],
      "published_date": "1920",
      "cover_url": "https://covers.openlibrary.org/b/id/10590366-L.jpg",
      "match_score": 95.0
    }
  ]
}
```

> "Get the cover for Dune by Frank Herbert"

Returns URLs for the largest available cover from Google Books (up to extraLarge ~1280px) with fallback to Open Library.

> "Look up metadata for these books: 1984, Brave New World, Fahrenheit 451"

Batch mode searches all three, returning title, author, year, ISBN, and cover URL for each.

## Configuration

All configuration is optional via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_BOOKS_API_KEY` | *(none)* | Optional Google API key for higher quota (1,000/day without) |
| `GOOGLE_DELAY` | `0.5` | Seconds between Google API calls |
| `OPENLIBRARY_DELAY` | `0.35` | Seconds between Open Library API calls |
| `GOOGLE_CB_THRESHOLD` | `3` | Consecutive 429 failures before circuit breaker trips |
| `GOOGLE_CB_COOLDOWN` | `60` | Seconds to skip Google after circuit breaker trips |
| `BOOK_MCP_USER_AGENT` | `BookMetadataMCP/0.1.3` | User-Agent for API requests |

### Bulk Usage / Rate Limiting

Google Books allows ~1,000 requests/day without an API key. For libraries larger than ~500 books, you'll hit rate limits. The server handles this automatically:

1. **Circuit breaker** — After 3 consecutive 429 responses, Google is skipped for 60 seconds
2. **Graceful fallback** — Open Library continues serving results during Google cooldown
3. **Auto-recovery** — Google is retried after the cooldown period expires
4. **Zero errors** — Rate limiting never causes failures, just temporary loss of Google-specific data

For the best bulk experience, set `GOOGLE_BOOKS_API_KEY` to a [Google Cloud API key](https://console.cloud.google.com/apis/credentials) with Books API enabled (free tier: 1,000/day; paid: higher).

## How Scoring Works

Each result is scored on a 0-100+ scale:

| Factor | Points | Description |
|--------|--------|-------------|
| Title match | 0-50 | Exact match = 50, partial = proportional |
| Author match | 0-30 | Exact = 30, partial/substring = 20 |
| Has cover | 10 | Cover image URL available |
| Has description | 5 | Book description available |
| Has ISBN | 5 | ISBN-10 or ISBN-13 available |
| Google source | 2 | Slight preference for cover quality |
| Study guide | -60 | CliffsNotes, SparkNotes, etc. |
| Inflated title | -15 | Result title much longer than query |

## Data Sources

### Google Books API
- Publisher-verified metadata
- 6 cover image sizes (smallThumbnail through extraLarge)
- Rich descriptions from publishers
- Free: 1,000 requests/day without API key

### Open Library API
- Community-curated metadata
- Original publication years (`first_publish_year`)
- Extensive subject classification
- Free: 3 requests/sec with User-Agent header

## Stress Test Results

Tested against 1,920 audiobook titles (v0.1.3, fresh install, no API key):

| Metric | Result |
|--------|--------|
| Match rate | **92.2%** (1,752 / 1,901 processed) |
| Errors | **0** |
| High-confidence matches (>=70) | **80%** (1,523) |
| Has cover URL | **85%** (1,629) |
| Has ISBN | **75%** (1,444) |
| Has publication year | **99.9%** of matches |
| Speed | 0.5-0.7 books/sec |
| Google 429 handling | Circuit breaker tripped, graceful OL fallback |

All 5 tools pass: `search_book`, `get_cover`, `get_metadata`, `download_cover`, `bulk_search`.
Edge case tests: **26/26 passed** (Unicode, short titles, empty inputs, special characters, ISBN lookup, bulk limits).

## License

MIT
