#!/usr/bin/env python3
"""Algeria Exhibitions taxonomy (category/location) lookups for the dispatcher.

Fetches the canonical category and location terms from the site's WP-JSON API so
subscriber category filters can be validated against what actually exists. Kept in
one module because the paging/retry and slug-normalization logic is a single
self-contained concern. Std-lib only.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

# The listener cron owns the shared HTTP user-agent; resolve it relative to this
# file (bin/, lib/, cron/ are siblings under workspace/) so it works deployed and
# in a worktree.
_LISTENER_DIR = Path(__file__).resolve().parents[1] / "algeria-exhibitions-listener"
if str(_LISTENER_DIR) not in sys.path:
    sys.path.insert(0, str(_LISTENER_DIR))
from check_new_events import UA  # noqa: E402


WP_API_BASE = "https://www.algeriaexhibitions.dz/en/wp-json/wp/v2"
TAXONOMY_PAGE_SIZES = (50, 10, None)
TAXONOMY_TIMEOUT_SECONDS = 4


def normalize_taxonomy_slug(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("-", " ")).strip().lower()


def taxonomy_url(taxonomy: str, page_size: int | None, page: int) -> str:
    params = {"_fields": "id,name,slug,count", "page": str(page)}
    if page_size:
        params["per_page"] = str(page_size)
    return f"{WP_API_BASE}/{taxonomy}?{urlencode(params)}"


def header_int(headers, name: str, default: int) -> int:
    if headers is None:
        return default
    try:
        return int(headers.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def taxonomy_http_get(url: str) -> tuple[str | None, object, str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TAXONOMY_TIMEOUT_SECONDS) as resp:
            return resp.read().decode("utf-8", "ignore"), resp.headers, ""
    except urllib.error.HTTPError as err:
        return None, None, f"HTTP {err.code}: {err.reason}"
    except Exception as err:
        return None, None, f"{type(err).__name__}: {err}"


def fetch_taxonomy_page(taxonomy: str, page_size: int | None, page: int) -> tuple[list[dict], object]:
    body, headers, error = taxonomy_http_get(taxonomy_url(taxonomy, page_size, page))
    if body is None:
        label = f"per_page={page_size}" if page_size else "default page size"
        detail = f": {error}" if error else ""
        raise RuntimeError(f"could not fetch {taxonomy} terms with {label}, page {page}{detail}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as err:
        raise RuntimeError(f"invalid Algeria Exhibitions {taxonomy} term payload") from err
    if not isinstance(payload, list):
        raise RuntimeError(f"unexpected Algeria Exhibitions {taxonomy} term payload")
    return payload, headers


def fetch_taxonomy_terms(taxonomy: str) -> list[dict]:
    errors = []
    for page_size in TAXONOMY_PAGE_SIZES:
        try:
            first_page, headers = fetch_taxonomy_page(taxonomy, page_size, 1)
            total_pages = max(1, header_int(headers, "X-WP-TotalPages", 1))
            terms = list(first_page)
            for page in range(2, total_pages + 1):
                page_terms, _ = fetch_taxonomy_page(taxonomy, page_size, page)
                terms.extend(page_terms)
            return terms
        except RuntimeError as err:
            label = f"per_page={page_size}" if page_size else "default"
            errors.append(f"{label}: {err}")
    raise RuntimeError(f"could not verify Algeria Exhibitions {taxonomy} terms ({'; '.join(errors)})")


def catalog_options_from_taxonomy() -> dict:
    categories = []
    for term in fetch_taxonomy_terms("eventcat"):
        slug = normalize_taxonomy_slug(term.get("slug"))
        if not slug:
            continue
        categories.append({"name": slug, "count": int(term.get("count") or 0)})

    locations = []
    location_error = ""
    try:
        location_terms = fetch_taxonomy_terms("location")
    except RuntimeError as err:
        location_terms = []
        location_error = str(err)
    for term in location_terms:
        slug = normalize_taxonomy_slug(term.get("slug"))
        if not slug:
            continue
        name = "SAFEX, Algiers" if slug == "safex" else re.sub(r"\s+", " ", str(term.get("name") or slug)).strip()
        locations.append({"name": name, "count": int(term.get("count") or 0)})

    return {
        "categories": sorted(categories, key=lambda item: (-item["count"], item["name"])),
        "locations": sorted(locations, key=lambda item: (-item["count"], item["name"])),
        "source": "taxonomy",
        "location_error": location_error,
    }


def fetch_catalog_options() -> dict:
    return catalog_options_from_taxonomy()
