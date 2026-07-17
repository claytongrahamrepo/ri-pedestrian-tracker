"""Geocode incidents that don't have coordinates yet.

Street addresses go to the US Census geocoder; everything else (intersections,
landmarks) goes to Nominatim (OpenStreetMap). Results outside Rhode Island's
bounding box are discarded. Incidents that fail 3 times are flagged
needs_review — fix those by hand-editing lat/lon in data/incidents.json
(hand-set coordinates are never overwritten because geocoding only runs on
incidents whose lat is null).
"""
import re
import time

import requests

from common import DATA, USER_AGENT, load_json, save_json

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Overpass rejects browser-like User-Agents (406); identify as a tool, and
# fall back to a mirror when the main instance is busy (504).
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OVERPASS_UA = "ri-pedestrian-tracker/0.1 (news-based safety map; contact via GitHub issues)"

STREET_ABBREVIATIONS = {
    "st": "Street", "ave": "Avenue", "rd": "Road", "blvd": "Boulevard",
    "dr": "Drive", "ln": "Lane", "ct": "Court", "pl": "Place",
    "hwy": "Highway", "pkwy": "Parkway", "sq": "Square", "ter": "Terrace",
}

# Rhode Island bounding box (includes Block Island).
RI_BOUNDS = {"lat": (41.0, 42.05), "lon": (-71.95, -71.05)}
MAX_ATTEMPTS = 3


def in_rhode_island(lat: float, lon: float) -> bool:
    return RI_BOUNDS["lat"][0] <= lat <= RI_BOUNDS["lat"][1] and RI_BOUNDS["lon"][0] <= lon <= RI_BOUNDS["lon"][1]


def geocode_census(address: str) -> tuple[float, float] | None:
    resp = requests.get(
        CENSUS_URL,
        params={"address": address, "benchmark": "Public_AR_Current", "format": "json"},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    matches = resp.json().get("result", {}).get("addressMatches", [])
    if matches:
        coords = matches[0]["coordinates"]
        return coords["y"], coords["x"]
    return None


def geocode_nominatim(query: str) -> tuple[float, float] | None:
    resp = requests.get(
        NOMINATIM_URL,
        params={"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "us"},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json()
    if results:
        return float(results[0]["lat"]), float(results[0]["lon"])
    return None


def looks_like_street_address(text: str) -> bool:
    return bool(re.match(r"^\d+\s+\w", text.strip()))


def expand_street_name(name: str) -> str:
    words = name.strip().split()
    return " ".join(STREET_ABBREVIATIONS.get(w.lower().rstrip("."), w) for w in words)


def split_intersection(text: str) -> tuple[str, str] | None:
    parts = re.split(r"\s+(?:and|&|at)\s+", text, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 2 and all(2 <= len(p.split()) <= 5 for p in parts):
        return expand_street_name(parts[0]), expand_street_name(parts[1])
    return None


def geocode_overpass_intersection(street1: str, street2: str, city: str) -> tuple[float, float] | None:
    """Find the OSM node shared by two named streets — handles intersections,
    which neither Census nor Nominatim geocodes."""
    query = f"""
    [out:json][timeout:25];
    area["name"="{city}"]["boundary"="administrative"]->.a;
    way(area.a)["highway"]["name"~"^{re.escape(street1)}$",i]->.w1;
    way(area.a)["highway"]["name"~"^{re.escape(street2)}$",i]->.w2;
    node(w.w1)(w.w2);
    out;
    """
    for url in OVERPASS_URLS:
        try:
            resp = requests.post(
                url, data={"data": query}, headers={"User-Agent": OVERPASS_UA}, timeout=40
            )
            resp.raise_for_status()
        except Exception as e:
            print(f"  ! overpass {url.split('/')[2]}: {e}")
            continue
        elements = resp.json().get("elements", [])
        if elements:
            lats = [e["lat"] for e in elements]
            lons = [e["lon"] for e in elements]
            return sum(lats) / len(lats), sum(lons) / len(lons)
        return None  # query succeeded, streets just don't intersect in OSM
    return None


def geocode_incident(inc: dict, allow_centroid: bool = True) -> tuple[float, float, str] | None:
    location = inc.get("location_text") or ""
    city = inc.get("city") or ""
    if not city and not location:
        return None

    attempts = []
    if location and looks_like_street_address(location):
        attempts.append(("census", lambda: geocode_census(f"{location}, {city}, RI")))
    if location:
        streets = split_intersection(location)
        if streets and city:
            attempts.append(
                ("overpass-intersection",
                 lambda: geocode_overpass_intersection(streets[0], streets[1], city))
            )
        attempts.append(
            ("nominatim", lambda: geocode_nominatim(f"{location}, {city}, Rhode Island"))
        )
    if city and allow_centroid:
        # Last resort: city centroid, flagged for review.
        attempts.append(("city-centroid", lambda: geocode_nominatim(f"{city}, Rhode Island")))

    for method, fn in attempts:
        try:
            result = fn()
        except Exception as e:
            print(f"  ! {method} error: {e}")
            result = None
        time.sleep(1.1)  # Nominatim rate limit: 1 req/sec
        if result and in_rhode_island(*result):
            return result[0], result[1], method
    return None


def wants_geocoding(inc: dict) -> bool:
    if inc.get("geocode_method") == "manual":  # hand-set coords, never touch
        return False
    if inc.get("geocode_attempts", 0) >= MAX_ATTEMPTS:
        return False
    if inc.get("lat") is None:
        return True
    # Centroid placements are provisional: keep retrying for a precise fix
    # (e.g. Overpass was down when this incident was first geocoded).
    return inc.get("geocode_method") == "city-centroid" and bool(inc.get("location_text"))


def main():
    incidents = load_json(DATA / "incidents.json", [])
    todo = [i for i in incidents if wants_geocoding(i)]
    print(f"{len(todo)} incident(s) to geocode.")

    for inc in todo:
        label = f"{inc.get('location_text') or '(no location)'}, {inc.get('city')}"
        has_centroid = inc.get("lat") is not None
        result = geocode_incident(inc, allow_centroid=not has_centroid)
        inc["geocode_attempts"] = inc.get("geocode_attempts", 0) + 1
        if result:
            inc["lat"], inc["lon"], inc["geocode_method"] = result
            inc["needs_review"] = (
                inc["geocode_method"] == "city-centroid" or inc.get("confidence") == "low"
            )
            print(f"  {label} -> {inc['lat']:.5f}, {inc['lon']:.5f} ({inc['geocode_method']})")
        elif not has_centroid and inc["geocode_attempts"] >= MAX_ATTEMPTS:
            inc["needs_review"] = True
            print(f"  {label} -> failed (final attempt)")
        else:
            print(f"  {label} -> no precise fix (attempt {inc['geocode_attempts']})")

    save_json(DATA / "incidents.json", incidents)


if __name__ == "__main__":
    main()
