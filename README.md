# book-metadata-mcp

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
| `BOOK_MCP_USER_AGENT` | `BookMetadataMCP/0.1` | User-Agent for API requests |

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

Tested against 1,920 audiobook titles:

| Metric | Result |
|--------|--------|
| Match rate | **92.2%** |
| Errors | **0** |
| High-confidence matches (90+) | **68%** |
| Has cover URL | **88.5%** |
| Has ISBN | **77.0%** |
| Speed | ~1 book/sec |

Edge case tests: **26/26 passed** (Unicode, short titles, empty inputs, special characters, ISBN lookup, bulk limits).

## License

MIT
