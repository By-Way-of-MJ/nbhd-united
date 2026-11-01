"""Build the offline place-timezone table from a local GeoNames dump."""

from __future__ import annotations

import gzip
import json
from datetime import date as date_cls
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.tenants.situation import _normalize_place_text

DEFAULT_SRC = Path("/tmp/geonames")
DEFAULT_OUT = Path(__file__).resolve().parents[2] / "data" / "place_tz.json.gz"


def _read_lines(path: Path):
    if not path.is_file():
        raise CommandError(f"missing GeoNames file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("#") or not line.strip():
                continue
            yield line.rstrip("\n")


def _admin1_names(src: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    for line in _read_lines(src / "admin1CodesASCII.txt"):
        columns = line.split("\t")
        if len(columns) < 3:
            continue
        names[columns[0]] = columns[2]
    return names


def _country_timezones(src: Path) -> dict[str, set[str]]:
    timezones: dict[str, set[str]] = {}
    for line in _read_lines(src / "timeZones.txt"):
        columns = line.split("\t")
        if len(columns) < 2:
            continue
        timezones.setdefault(columns[0], set()).add(columns[1])
    return timezones


def _countries(src: Path) -> tuple[dict[str, list], dict[str, str]]:
    timezones = _country_timezones(src)
    countries: dict[str, list] = {}
    country_names: dict[str, str] = {}
    for line in _read_lines(src / "countryInfo.txt"):
        columns = line.split("\t")
        if len(columns) < 5:
            continue
        country_code = columns[0]
        normalized_name = _normalize_place_text(columns[4])
        if not normalized_name:
            continue
        countries[normalized_name] = [country_code, sorted(timezones.get(country_code, set()))]
        country_names[country_code] = normalized_name
    return countries, country_names


def _cities(src: Path) -> dict[str, list[list]]:
    admin1 = _admin1_names(src)
    cities: dict[str, list[list]] = {}
    seen_geonameids: dict[str, set[str]] = {}
    for line in _read_lines(src / "cities15000.txt"):
        columns = line.split("\t")
        if len(columns) < 18:
            continue
        geonameid, name, asciiname = columns[0], columns[1], columns[2]
        feature_code = columns[7]
        if not feature_code.startswith("PPL"):
            continue
        country_code = columns[8]
        admin1_code = columns[10]
        population_text = columns[14]
        timezone = columns[17]
        population = int(population_text) if population_text.isdigit() else 0
        admin1_name = admin1.get(f"{country_code}.{admin1_code}", "")
        row = [country_code, admin1_name, population, timezone]
        for candidate in (name, asciiname):
            key = _normalize_place_text(candidate)
            if not key:
                continue
            seen = seen_geonameids.setdefault(key, set())
            if geonameid in seen:
                continue
            seen.add(geonameid)
            cities.setdefault(key, []).append(row)
    return cities


class Command(BaseCommand):
    help = "Build apps/tenants/data/place_tz.json.gz from a local GeoNames dump."

    def add_arguments(self, parser):
        parser.add_argument("--src", default=str(DEFAULT_SRC), help="Directory with the GeoNames text files.")
        parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output .json.gz path.")
        parser.add_argument("--date", default=date_cls.today().isoformat(), help="Build date recorded in source.")

    def handle(self, *args, **options):
        src = Path(options["src"])
        out = Path(options["out"])
        build_date = options["date"]
        try:
            date_cls.fromisoformat(build_date)
        except ValueError as exc:
            raise CommandError(f"invalid --date (expected YYYY-MM-DD): {build_date}") from exc

        countries, country_names = _countries(src)
        table = {
            "cities": _cities(src),
            "countries": countries,
            "country_names": country_names,
            "source": f"GeoNames cities15000 + countryInfo + timeZones, CC BY 4.0, built {build_date}",
        }
        payload = json.dumps(table, sort_keys=True, separators=(",", ":")).encode("utf-8")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(gzip.compress(payload, mtime=0))

        self.stdout.write(
            f"wrote {out} ({out.stat().st_size} bytes, {len(table['cities'])} city keys, "
            f"{len(table['countries'])} countries)"
        )
