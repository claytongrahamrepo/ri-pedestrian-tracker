"""Build the GeoJSON + summary files the map reads, from data/incidents.json."""
from datetime import datetime, timezone

from common import DATA, DOCS_DATA, load_json, save_json

# How trustworthy the dot's position is, mirroring the map legend.
PRECISION = {
    "overpass-intersection": "intersection",
    "manual": "intersection",
    "census": "street",
    "nominatim": "street",
    "city-centroid": "centroid",
}


def main():
    incidents = load_json(DATA / "incidents.json", [])

    features = []
    for inc in incidents:
        if inc.get("lat") is None or inc.get("lon") is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [inc["lon"], inc["lat"]]},
                "properties": {
                    "id": inc["id"],
                    "date": inc.get("incident_date"),
                    "date_precision": inc.get("date_precision"),
                    "city": inc.get("city"),
                    "location": inc.get("location_text"),
                    "severity": inc.get("severity", "unknown"),
                    "pedestrians_struck": inc.get("pedestrians_struck", 1),
                    "victim_age": inc.get("victim_age"),
                    "hit_and_run": inc.get("hit_and_run", False),
                    "summary": inc.get("summary", ""),
                    "sources": inc.get("sources", []),
                    "story_date": inc.get("story_date"),
                    "precision": PRECISION.get(inc.get("geocode_method"), "centroid"),
                },
            }
        )

    save_json(DOCS_DATA / "incidents.geojson", {"type": "FeatureCollection", "features": features})
    save_json(
        DOCS_DATA / "summary.json",
        {
            "total": len(incidents),
            "mapped": len(features),
            "fatal": sum(1 for i in incidents if i.get("severity") == "fatal"),
            "injury": sum(1 for i in incidents if i.get("severity") == "injury"),
            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        },
    )
    print(f"Wrote {len(features)} mapped / {len(incidents)} total incidents.")


if __name__ == "__main__":
    main()
