# Offline place timezone table

`apps/tenants/data/place_tz.json.gz` is a compact, committed lookup table built
from a local GeoNames dump. It lets the server resolve common city and country
labels without sending the place name to a third-party geocoder.

## Data source and licence

- GeoNames `cities15000.txt` (populated places; names, ASCII names, country,
  admin1 code, population, timezone)
- GeoNames `countryInfo.txt` (country names)
- GeoNames `admin1CodesASCII.txt` (admin1 display names)
- GeoNames `timeZones.txt` (country time zones)

Contains data from GeoNames (geonames.org), licensed CC BY 4.0.

## Rebuild

The table records its build date in `source`, so use the same `--date` to
reproduce the committed bytes:

```bash
.venv/bin/python manage.py build_place_tz_table --src /tmp/geonames --date 2026-11-01
```

Without `--date`, the command records the current local date.
