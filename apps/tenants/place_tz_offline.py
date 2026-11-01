"""Offline place-timezone lookups from the committed GeoNames-derived table."""

from __future__ import annotations

import gzip
import json
import logging
from functools import lru_cache
from pathlib import Path

from apps.tenants.situation import (
    PlaceTz,
    _normalize_place_text,
    is_assignable_profile_timezone,
)

logger = logging.getLogger(__name__)

DATA_PATH = Path(__file__).resolve().parent / "data" / "place_tz.json.gz"


@lru_cache(maxsize=1)
def _load_place_tz_table() -> dict | None:
    """Load the table once per process; a load failure disables offline lookups."""
    try:
        with gzip.open(DATA_PATH, "rt", encoding="utf-8") as handle:
            table = json.load(handle)
        if not isinstance(table, dict):
            raise ValueError("place timezone table is not an object")
        return table
    except Exception:
        logger.warning("offline place timezone table unavailable; falling back to network", exc_info=True)
        return None


def offline_place_timezone(label: str) -> PlaceTz | None:
    """Resolve a place label from the offline table, or None when it is absent.

    A known but ambiguous city is ``PlaceTz(kind="unknown")`` so callers do not
    fall back to the network.  A country that spans several zones is
    ``PlaceTz(kind="region")``.  Labels absent from the table (including a city
    name whose qualifier filters out every row) return None.
    """
    try:
        if not isinstance(label, str):
            return None
        name, _, qualifier = label.partition(",")
        name = _normalize_place_text(name.strip())
        qualifier = _normalize_place_text(qualifier.strip())
        if not name:
            return None

        table = _load_place_tz_table()
        if table is None:
            return None

        countries = table.get("countries") or {}
        country = countries.get(name)
        if country is not None:
            timezones = country[1] if len(country) > 1 else []
            if len(timezones) == 1 and is_assignable_profile_timezone(timezones[0]):
                return PlaceTz(kind="zone", tz=timezones[0])
            return PlaceTz(kind="region")

        cities = table.get("cities") or {}
        rows = cities.get(name)
        if not rows:
            return None

        if qualifier:
            country_names = table.get("country_names") or {}
            filtered = []
            for row in rows:
                country_code = str(row[0]).casefold()
                admin1_name = _normalize_place_text(row[1])
                country_name = _normalize_place_text(country_names.get(row[0], ""))
                if qualifier in {admin1_name, country_name, country_code}:
                    filtered.append(row)
            rows = filtered
            if not rows:
                return None

        kept = [(int(row[2]), str(row[3])) for row in rows if is_assignable_profile_timezone(str(row[3]))]
        if not kept:
            return PlaceTz(kind="unknown")

        top_population, top_tz = max(kept, key=lambda place: place[0])
        clear_winner = top_population >= 100_000 and all(
            tz_name == top_tz or population <= top_population / 5 for population, tz_name in kept
        )
        return PlaceTz(kind="zone", tz=top_tz) if clear_winner else PlaceTz(kind="unknown")
    except Exception:
        return None
