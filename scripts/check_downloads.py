#!/usr/bin/env python3
"""Daily PyPI download stats checker for book-metadata-mcp.

Queries pypistats.org API and appends to a CSV log.
Run via launchd: com.colony.pypi-stats.plist
"""

import csv
import json
import urllib.request
from datetime import datetime
from pathlib import Path

PACKAGE = "book-metadata-mcp"
LOG_FILE = Path(__file__).parent.parent / "download_stats.csv"
API_URL = f"https://pypistats.org/api/packages/{PACKAGE}/overall"


def fetch_stats() -> dict | None:
    """Fetch overall download stats from pypistats API."""
    try:
        req = urllib.request.Request(API_URL)
        req.add_header("User-Agent", "book-metadata-mcp-stats/1.0")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except Exception as e:
        print(f"API error: {e}")
        return None


def main():
    data = fetch_stats()
    if not data:
        print("No data available yet (stats take 24-48h after first publish)")
        return

    # pypistats returns {"data": [{"category": "with_mirrors"|"without_mirrors", "downloads": N, "date": "..."}]}
    rows = data.get("data", [])
    if not rows:
        print("No download data returned")
        return

    # Sum the latest day's downloads (without_mirrors is the clean count)
    latest = {}
    for row in rows:
        cat = row.get("category", "")
        latest[cat] = latest.get(cat, 0) + row.get("downloads", 0)

    total = latest.get("without_mirrors", 0)
    with_mirrors = latest.get("with_mirrors", 0)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Append to CSV
    write_header = not LOG_FILE.exists()
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["date", "downloads", "with_mirrors"])
        writer.writerow([now, total, with_mirrors])

    print(f"{now} — downloads: {total} (with mirrors: {with_mirrors})")


if __name__ == "__main__":
    main()
