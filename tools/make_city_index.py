"""One-time prep: GeoNames cities15000 → the dense offline place index.

Natural Earth's populated places carry ~7,300 cities worldwide and only 58
German ones — enough for globe labels, far too sparse for "via Pforzheim"
route labels or small-town autocomplete. This tool downloads the GeoNames
`cities15000` dump (every place with population ≥ 15,000 plus admin seats,
~34k rows, refreshed daily), keeps exactly the columns the runtime needs,
and writes the compact asset `agentic_maps/geo/geonames.py` loads:

    var/geo/places-geonames.tsv.gz

Columns (tab-separated, gzip, `#` lines are provenance comments):

    name    ascii   lat     lon     population      country flag

`ascii` is empty when it equals `name` (most rows); `country` is the ISO
3166-1 alpha-2 code; `flag` is `C` for a national capital (PPLC), `A` for a
first-order admin seat (PPLA), empty otherwise.

Dropped feature codes — not standalone "via" cities or no longer places:
PPLX (section of a populated place: Berlin's boroughs must not label a
route "via Neukölln"), PPLH (historical), PPLQ (abandoned), PPLW
(destroyed), PPLR (religious site).

Provenance & licence
--------------------
Source:  GeoNames geographical database, https://www.geonames.org
Dump:    https://download.geonames.org/export/dump/cities15000.zip
Licence: Creative Commons Attribution 4.0 (CC BY 4.0),
         https://creativecommons.org/licenses/by/4.0/ — stated in
         https://download.geonames.org/export/dump/readme.txt
         ("This work is licensed under a Creative Commons Attribution
         4.0 License"; data provided "as is", no warranty).
Credit:  "GeoNames (geonames.org)" — CC BY requires this credit wherever
         the data is shown or redistributed; see docs/imagery-coverage.md
         §1 and the attribution wiring in `rest/maps_api.py` /
         `package/builder.py`.

stdlib only on purpose: an authoring-time tool must run in any venv,
including none.
"""

import argparse
import csv
import datetime
import gzip
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

DUMP_URL = "https://download.geonames.org/export/dump/cities15000.zip"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = _ROOT / "var" / "geo" / "places-geonames.tsv.gz"

# GeoNames feature codes that must not become "via" cities (see docstring).
DROP_CODES = {"PPLX", "PPLH", "PPLQ", "PPLW", "PPLR"}

# cities15000.txt column indices (readme.txt "The main 'geoname' table").
_NAME, _ASCII, _LAT, _LON = 1, 2, 4, 5
_FEATURE_CODE, _COUNTRY, _POPULATION = 7, 8, 14


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "agentic-maps prep tool"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def _rows(text: str) -> list[tuple]:
    rows = []
    for record in csv.reader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE):
        code = record[_FEATURE_CODE]
        if code in DROP_CODES:
            continue
        name = record[_NAME]
        ascii_name = record[_ASCII]
        if ascii_name == name:
            ascii_name = ""      # blank when identical — most rows, keeps the asset lean
        flag = "C" if code == "PPLC" else ("A" if code == "PPLA" else "")
        rows.append((
            name, ascii_name,
            f"{float(record[_LAT]):.5f}", f"{float(record[_LON]):.5f}",
            record[_POPULATION] or "0", record[_COUNTRY], flag,
        ))
    return rows


def build(source_text: str, out_path: Path) -> tuple[int, int]:
    """Write the asset; returns (rows_total, rows_germany)."""
    rows = _rows(source_text)
    buffer = io.StringIO()
    today = datetime.date.today().isoformat()
    buffer.write("# GeoNames cities15000 — compact place index for agentic-maps\n")
    buffer.write(f"# source: {DUMP_URL} (fetched {today})\n")
    buffer.write(f"# licence: CC BY 4.0 — {LICENSE_URL} — credit: GeoNames (geonames.org)\n")
    buffer.write("# columns: name  ascii  lat  lon  population  country  flag(C=capital,A=admin1-seat)\n")
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    writer.writerows(rows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(gzip.compress(buffer.getvalue().encode("utf-8"), 9))
    return len(rows), sum(1 for row in rows if row[5] == "DE")


def main() -> None:
    parser = argparse.ArgumentParser(description="GeoNames cities15000 → places-geonames.tsv.gz")
    parser.add_argument("--source", type=Path, default=None,
                        help="local cities15000 .zip or .txt (skips the download)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    if args.source is not None:
        raw = args.source.read_bytes()
        print(f"using local {args.source}")
    else:
        print(f"downloading {DUMP_URL} …")
        raw = _fetch(DUMP_URL)
        print(f"  {len(raw):,} bytes")

    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            text = archive.read("cities15000.txt").decode("utf-8")
    else:
        text = raw.decode("utf-8")

    total, germany = build(text, args.out)
    size = args.out.stat().st_size
    print(f"wrote {args.out}: {total:,} places ({germany:,} in DE), {size:,} bytes gz")
    if total < 20_000:
        print("WARNING: suspiciously few rows — dump format may have changed", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
