"""Offline place-timezone lookups from the committed GeoNames-derived table."""

from __future__ import annotations

import gzip
import json
import logging
from functools import lru_cache
from pathlib import Path

from apps.tenants.situation import (
    PlaceTz,
    _clear_winner_zone,
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

    Countries and first-level administrative regions (states/provinces) are
    administrative matches, just like the network lookup's ADM1 handling.  A
    known but ambiguous city is ``PlaceTz(kind="unknown")`` so callers do not
    fall back to the network; a name shared by several administrative regions
    or zones is ``PlaceTz(kind="region")``.  Labels absent from the table
    (including a city name whose qualifier filters out every row) return None.
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
        countries_by_name = table.get("country_names") or {}
        admins = table.get("admins") or {}
        cities = table.get("cities") or {}

        admin_tzs: set[str] = set()
        admin_found = False

        country = countries.get(name)
        if country is not None:
            country_code = str(country[0]) if country else ""
            country_name = _normalize_place_text(countries_by_name.get(country_code, ""))
            if not qualifier or qualifier in {country_name, country_code.casefold()}:
                admin_found = True
                admin_tzs |= {str(tz) for tz in (country[1] if len(country) > 1 else [])}

        for entry in admins.get(name, []):
            country_code = str(entry[0])
            country_name = _normalize_place_text(countries_by_name.get(country_code, ""))
            if not qualifier or qualifier in {country_name, country_code.casefold()}:
                admin_found = True
                admin_tzs |= {str(tz) for tz in (entry[1] if len(entry) > 1 else [])}

        rows = cities.get(name) or []
        if qualifier:
            filtered = []
            for row in rows:
                country_code = str(row[0]).casefold()
                admin1_name = _normalize_place_text(row[1])
                country_name = _normalize_place_text(countries_by_name.get(row[0], ""))
                if qualifier in {admin1_name, country_name, country_code}:
                    filtered.append(row)
            rows = filtered

        kept = [(int(row[2]), str(row[3])) for row in rows if is_assignable_profile_timezone(str(row[3]))]
        city_tz = _clear_winner_zone(kept)

        if admin_found:
            candidates = {tz for tz in admin_tzs if is_assignable_profile_timezone(tz)}
            if city_tz:
                candidates.add(city_tz)
            if rows and not city_tz:
                # Same-name places disagree, but a city row is only a real
                # clash when its zone is outside the administrative match.
                kept_zones = {tz_name for _, tz_name in kept}
                if kept_zones and not kept_zones.issubset(candidates):
                    top_population, top_tz = max(kept, key=lambda place: place[0])
                    # A populated city matching the admin candidates still has
                    # a same-name rival in another zone, so item 3's ambiguity
                    # wins over the administrative fallback.
                    if top_population >= 100_000 and top_tz in candidates:
                        return PlaceTz(kind="unknown")
                    return PlaceTz(kind="region")
            if len(candidates) == 1:
                return PlaceTz(kind="zone", tz=next(iter(candidates)))
            return PlaceTz(kind="region") if candidates else PlaceTz(kind="unknown")

        if not rows:
            return None
        return PlaceTz(kind="zone", tz=city_tz) if city_tz else PlaceTz(kind="unknown")
    except Exception:
        return None
