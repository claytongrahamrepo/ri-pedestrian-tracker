# Rhode Island Pedestrian Incident Tracker

A self-updating map of pedestrians struck by vehicles in Rhode Island, built
from local news coverage. Runs entirely on free GitHub infrastructure:

- **Discovery** — a daily GitHub Actions job searches Google News RSS and local
  outlet feeds for candidate stories ([scripts/discover.py](scripts/discover.py))
- **Extraction** — [GitHub Models](https://docs.github.com/en/github-models)
  (free LLM inference, authenticated with the built-in Actions token) reads each
  article and pulls out date, location, and severity as JSON
  ([scripts/extract.py](scripts/extract.py))
- **Geocoding** — US Census geocoder for street addresses, Nominatim/OSM for
  intersections and landmarks ([scripts/geocode.py](scripts/geocode.py))
- **Map** — a static [Leaflet](https://leafletjs.com/) page on GitHub Pages
  ([docs/index.html](docs/index.html)), reading GeoJSON committed by the
  workflow

Inspired by [death_by_car](https://diazale.github.io/death_by_car/).

## Setup (one time)

1. Push this repo to GitHub (public, so Pages is free).
2. **Settings → Pages** → Source: *Deploy from a branch* → Branch: `main`,
   folder `/docs`.
3. **Actions** tab → enable workflows if prompted.
4. Run the **Collect incidents** workflow manually (Actions → Collect incidents
   → Run workflow). Set `lookback_days` to ~30 on the first run to backfill a
   month of coverage. The daily schedule takes over after that.

No API keys or secrets are needed — the workflow's built-in `GITHUB_TOKEN`
covers GitHub Models via the `models: read` permission.

## Reviewing the data

The pipeline is designed for a quick weekly skim rather than blind trust:

- Incidents with low extraction confidence, failed geocoding, or a
  city-centroid-only location get `"needs_review": true` in
  [data/incidents.json](data/incidents.json).
- To fix a location, edit the incident's `lat`/`lon` by hand, set
  `geocode_method` to `"manual"`, and set `needs_review` to `false`.
  Incidents marked `"manual"` are never re-geocoded.
- To delete a false positive, remove its entry from `incidents.json`. Its URL
  stays in `seen_urls.json`, so it will not come back.
- [data/rejected.json](data/rejected.json) logs articles the model judged
  irrelevant, with reasons — skim it occasionally to check for missed incidents
  (recall matters more than precision here).

Commit and push edits; the next workflow run rebuilds the map data, or run the
workflow manually to refresh immediately.

## Running locally

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/discover.py                 # no auth needed
GITHUB_TOKEN=<pat> python scripts/extract.py   # PAT with "Models" read permission
python scripts/geocode.py
python scripts/build_geojson.py
python -m http.server -d docs 8000         # view the map
```

For the PAT: GitHub → Settings → Developer settings → Fine-grained tokens →
generate one with the **Models** account permission (read).

## Tuning

Everything adjustable lives in [config.json](config.json): the Google News
queries, outlet RSS feeds, the keyword pre-filter, which model to use, and the
per-run article cap (keeps you inside the GitHub Models free tier — the queue
carries anything extra over to the next day).

## Caveats

News-derived data undercounts: crashes with no coverage never enter the
dataset, and injury crashes are covered less reliably than fatal ones. For
fatalities, [NHTSA FARS](https://www.nhtsa.gov/research-data/fatality-analysis-reporting-system-fars)
and RIDOT crash data are authoritative (but lag by a year or more) — useful as
a validation set or backfill source.
