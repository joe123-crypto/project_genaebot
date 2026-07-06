"""Profile, preference, and preferred-location resolution for Job Scout.

Pure data-shaping helpers (no network). Location functions raise SystemExit on
invalid operator/subscriber input, matching the original inline behavior.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from country_codes import country_code, country_name

from .constants import DEFAULT_JSEARCH_COUNTRY, DEFAULT_JSEARCH_LANGUAGE


def clean_items(values: list[str] | None) -> list[str]:
  output: list[str] = []
  seen: set[str] = set()
  if isinstance(values, str):
    iterable = [values]
  else:
    iterable = values or []
  for value in iterable:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
      continue
    key = text.casefold()
    if key in seen:
      continue
    seen.add(key)
    output.append(text)
  return output


def resolve_preferred_locations(values: list[str] | None) -> dict:
  locations = clean_items(values)
  if not locations:
    raise SystemExit("preferred location is required")

  codes: set[str] = set()
  for location in locations:
    locality, separator, country_value = location.rpartition(",")
    locality = locality.strip()
    country_value = country_value.strip()
    if not separator or not locality or not country_value:
      raise SystemExit(
        f'Preferred location must include the country, for example "Bindura, Zimbabwe": {location}'
      )
    code = country_code(country_value)
    if not code:
      raise SystemExit(f"Preferred location country is not recognized: {country_value}")
    codes.add(code)

  if len(codes) != 1:
    countries = ", ".join(sorted(country_name(code) for code in codes))
    raise SystemExit(
      f"Preferred locations must be in one country because Job Scout uses one country filter: {countries}"
    )

  code = next(iter(codes))
  return {
    "locations": locations,
    "country": code,
    "countryName": country_name(code),
  }


def normalize_stored_locations(
  values: list[str] | None,
  stored_country: str | None = None,
) -> dict:
  locations = clean_items(values)
  if not locations:
    return {"locations": [], "country": None}

  stored_code = None
  if str(stored_country or "").strip():
    stored_code = country_code(stored_country)
    if not stored_code:
      raise SystemExit(f"Stored search country is not recognized: {stored_country}")

  normalized: list[str] = []
  codes: set[str] = set()
  for location in locations:
    if "," in location:
      resolved = resolve_preferred_locations([location])
      code = str(resolved["country"])
      normalized.append(location)
    else:
      inferred_code = country_code(location)
      if inferred_code:
        code = inferred_code
        normalized.append(f"Nationwide, {country_name(code)}")
      else:
        if not stored_code:
          raise SystemExit(
            f"Stored preferred location needs a valid country before it can be normalized: {location}"
          )
        code = stored_code
        normalized.append(f"{location}, {country_name(code)}")
    codes.add(code)

  if len(codes) != 1:
    countries = ", ".join(sorted(country_name(code) for code in codes))
    raise SystemExit(
      f"Preferred locations must be in one country because Job Scout uses one country filter: {countries}"
    )

  code = next(iter(codes))
  if stored_code and stored_code != code:
    raise SystemExit(
      f"Stored search country {stored_code} conflicts with preferred location country {code}"
    )
  return {"locations": clean_items(normalized), "country": code}


def resolved_profile_country(
  locations: list[str],
  explicit_country: str | None = None,
  fallback_country: str | None = None,
) -> str:
  if locations:
    resolved = resolve_preferred_locations(locations)
    derived = str(resolved["country"])
    if explicit_country:
      supplied = country_code(explicit_country)
      if not supplied:
        raise SystemExit(f"Search country is not recognized: {explicit_country}")
      if supplied != derived:
        raise SystemExit(
          f"Search country {supplied} conflicts with preferred location country {derived}"
        )
    return derived

  supplied_value = explicit_country or fallback_country or DEFAULT_JSEARCH_COUNTRY
  supplied = country_code(supplied_value)
  if not supplied:
    raise SystemExit(f"Search country is not recognized: {supplied_value}")
  return supplied


def resolve_profile_locations(
  stored_locations: list[str] | None,
  submitted_locations: list[str] | None = None,
  explicit_country: str | None = None,
  stored_country: str | None = None,
) -> dict:
  stored = normalize_stored_locations(stored_locations, stored_country)
  submitted_values = clean_items(submitted_locations)
  submitted = resolve_preferred_locations(submitted_values) if submitted_values else None

  codes = {
    str(resolved["country"])
    for resolved in (stored, submitted)
    if resolved and resolved.get("country")
  }
  if len(codes) != 1 and codes:
    countries = ", ".join(sorted(country_name(code) for code in codes))
    raise SystemExit(
      f"Preferred locations must be in one country because Job Scout uses one country filter: {countries}"
    )

  locations = clean_items([
    *stored["locations"],
    *(submitted["locations"] if submitted else []),
  ])
  if codes:
    country = resolved_profile_country(locations, explicit_country)
  else:
    country = resolved_profile_country([], explicit_country, stored_country)
  return {"locations": locations, "country": country}


def profile_values(profile: dict, key: str) -> list[str]:
  value = profile.get(key, [])
  if isinstance(value, list):
    return value
  if value is None:
    return []
  return [str(value)]


def preference_code(profile: dict, key: str, default: str) -> str:
  text = re.sub(r"[^A-Za-z]", "", str(profile.get(key) or default)).lower()
  return text if re.fullmatch(r"[a-z]{2}", text) else default


def max_applications(profile: dict, default: int = 5) -> int:
  try:
    value = int(profile.get("maxApplicationsPerRun", default))
  except (TypeError, ValueError):
    value = default
  return max(0, min(value, 5))


def load_profile_json(value: str | None) -> dict:
  if not value:
    return {}
  if value.startswith("@"):
    return json.loads(Path(value[1:]).read_text(encoding="utf-8"))
  return json.loads(value)


def subscriber_preferences(subscriber: dict) -> dict:
  preferences = subscriber.get("preferences")
  return preferences if isinstance(preferences, dict) else {}


def subscriber_user_id(subscriber: dict) -> str:
  for key in ("userId", "uid", "id"):
    value = str(subscriber.get(key) or "").strip()
    if value:
      return value
  raise SystemExit("subscriber is missing userId")


def user_key(user_id: str) -> str:
  text = str(user_id or "").strip()
  if not text:
    raise SystemExit("user-id is required")
  return hashlib.sha256(f"job-scout-user:{text}".encode("utf-8")).hexdigest()[:16]


def jsearch_location(location: str) -> str:
  """Use the country alone for nationwide profiles without changing stored preferences."""
  text = str(location or "").strip()
  locality, separator, country = text.rpartition(",")
  if separator and locality.strip().casefold() == "nationwide" and country.strip():
    return country.strip()
  return text


def search_queries(preferences: dict) -> list[str]:
  roles = clean_items(profile_values(preferences, "targetRoles") or profile_values(preferences, "roles"))
  try:
    from scrapers import base as scraper_base
    roles = scraper_base.jsearch_roles(roles)
  except Exception:
    pass
  locations = clean_items([jsearch_location(location) for location in profile_values(preferences, "locations")])
  language = preference_code(preferences, "language", DEFAULT_JSEARCH_LANGUAGE)
  location_connector = "à" if language == "fr" else "in"
  if not roles:
    return []
  if not locations:
    locations = ["Algeria"]
  queries: list[str] = []
  seen: set[str] = set()
  for role in roles:
    for location in locations:
      query = f"{role} {location_connector} {location}".strip()
      key = query.casefold()
      if key in seen:
        continue
      seen.add(key)
      queries.append(query)
  return queries
