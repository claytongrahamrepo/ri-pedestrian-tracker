"""Extract structured incident data from candidate articles using GitHub Models.

Consumes data/candidates.json, fetches each article, and asks the model
whether it describes a pedestrian struck by a vehicle in Rhode Island. Relevant
incidents are merged into data/incidents.json (deduplicating stories about the
same crash); irrelevant articles are logged to data/rejected.json.

Auth: set GITHUB_TOKEN. In GitHub Actions the built-in token works when the
workflow has `models: read` permission. Locally, use a fine-grained PAT with
the "Models" read permission.
"""
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from common import CONFIG, DATA, USER_AGENT, load_json, save_json

API_URL = "https://models.github.ai/inference/chat/completions"
MAX_ARTICLE_CHARS = 6000
MAX_FETCH_ATTEMPTS = 3

SYSTEM_PROMPT = """\
You extract structured data about pedestrian traffic incidents from local news
articles. Respond with a single JSON object, no other text:

{
  "relevant": bool,      // true only if the article reports a pedestrian (a
                         // person on foot, incl. in a wheelchair or on a
                         // scooter) being struck by a motor vehicle, and the
                         // crash itself happened in Rhode Island, USA.
                         // A Rhode Island resident or native struck in another
                         // state is NOT relevant — what matters is where the
                         // crash occurred. Note that Seekonk, Attleboro, Fall
                         // River, and Dartmouth are in Massachusetts, not RI.
                         // Cyclists, vehicle-only crashes, and general
                         // road-safety stories are NOT relevant.
  "state": string,       // two-letter state where the crash occurred ("RI",
                         // "MA", ...)
  "incident_date": "YYYY-MM-DD" | null,  // date of the crash itself. Resolve
                         // relative phrases ("Tuesday night", "last week")
                         // against the article publish date you are given.
  "date_precision": "day" | "approximate" | "unknown",
  "city": string | null,          // RI city/town, e.g. "Providence"
  "location_text": string | null, // most specific location mentioned:
                         // street address, intersection, or landmark. Spell
                         // out street types in full ("Broad Street and
                         // Thurbers Avenue", never "Broad St & Thurbers Ave")
  "severity": "fatal" | "injury" | "uninjured" | "unknown",
  "pedestrians_struck": int,
  "victim_age": int | null,
  "hit_and_run": bool,
  "summary": string,     // one factual sentence, no names
  "confidence": "high" | "medium" | "low"
}

If relevant is false, only "relevant" and a brief "summary" of why are needed.
A follow-up story about a previously reported crash IS relevant (it will be
deduplicated later)."""


BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Free text-extraction proxy for outlets that 403 datacenter IPs (WPRI,
# GoLocalProv block GitHub Actions runners directly).
READER_PREFIX = "https://r.jina.ai/"


def fetch_article_text(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (401, 403, 451):
            print("  . direct fetch blocked, trying reader proxy")
            return fetch_via_reader(url)
        raise
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "aside"]):
        tag.decompose()
    container = soup.find("article") or soup.body or soup
    paragraphs = [p.get_text(" ", strip=True) for p in container.find_all("p")]
    text = "\n".join(p for p in paragraphs if len(p) > 30)
    return text[:MAX_ARTICLE_CHARS] if len(text) > 200 else None


def fetch_via_reader(url: str) -> str | None:
    resp = requests.get(READER_PREFIX + url, timeout=40)
    resp.raise_for_status()
    # The reader returns markdown full of nav junk. Strip inline links, then
    # keep the metadata header plus prose-length lines only — menu items are
    # short, article paragraphs are long.
    kept = []
    for i, ln in enumerate(resp.text.splitlines()):
        if i < 5 and ln.startswith(("Title:", "URL Source:", "Published Time:")):
            kept.append(ln)
            continue
        prev = None
        while prev != ln:  # nested [![alt](img)](page) needs repeated passes
            prev = ln
            ln = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", ln)
        ln = ln.strip(" *#")
        if len(ln) >= 80:
            kept.append(ln)
    text = "\n".join(kept)
    return text[:MAX_ARTICLE_CHARS] if len(text) > 200 else None


def call_model(token: str, candidate: dict, article_text: str) -> dict | None:
    user_msg = (
        f"Article URL: {candidate['url']}\n"
        f"Published: {candidate.get('published') or 'unknown'}\n"
        f"Headline: {candidate.get('title', '')}\n\n"
        f"{article_text}"
    )
    resp = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "model": CONFIG["model"],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        },
        timeout=90,
    )
    if resp.status_code == 429:
        return {"_rate_limited": True, "_retry_after": resp.headers.get("Retry-After")}
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return json.loads(content)


def incident_id(rec: dict) -> str:
    key = f"{rec.get('incident_date')}|{rec.get('city')}|{(rec.get('location_text') or '')[:40]}"
    return hashlib.sha1(key.lower().encode()).hexdigest()[:12]


def location_similarity(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    ta = set(a.lower().replace(",", " ").split())
    tb = set(b.lower().replace(",", " ").split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def find_duplicate(incidents: list, rec: dict) -> dict | None:
    """Same city and incident date within a day => likely the same crash."""
    if not rec.get("incident_date") or not rec.get("city"):
        return None
    try:
        rec_date = datetime.fromisoformat(rec["incident_date"])
    except ValueError:
        return None
    for inc in incidents:
        if (inc.get("city") or "").lower() != rec["city"].lower():
            continue
        if not inc.get("incident_date"):
            continue
        try:
            days = abs((datetime.fromisoformat(inc["incident_date"]) - rec_date).days)
        except ValueError:
            continue
        if days > 1:
            continue
        sim = location_similarity(inc.get("location_text"), rec.get("location_text"))
        if sim >= 0.5 or not inc.get("location_text") or not rec.get("location_text"):
            return inc
    return None


def merge_into(inc: dict, rec: dict, url: str):
    if url not in inc["sources"]:
        inc["sources"].append(url)
    # Prefer the more specific / more severe information.
    if not inc.get("location_text") and rec.get("location_text"):
        inc["location_text"] = rec["location_text"]
    if inc.get("severity") != "fatal" and rec.get("severity") == "fatal":
        inc["severity"] = "fatal"
    for field in ("victim_age",):
        if inc.get(field) is None and rec.get(field) is not None:
            inc[field] = rec[field]


def main():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN is not set; cannot call GitHub Models.")

    candidates = load_json(DATA / "candidates.json", [])
    incidents = load_json(DATA / "incidents.json", [])
    rejected = load_json(DATA / "rejected.json", [])
    if not candidates:
        print("No candidates queued.")
        return

    max_per_run = int(os.environ.get("MAX_ARTICLES", CONFIG["max_articles_per_run"]))
    remaining = []
    processed = new_count = 0

    for i, cand in enumerate(candidates):
        if processed >= max_per_run:
            remaining.extend(candidates[i:])
            break
        print(f"[{processed + 1}/{max_per_run}] {cand['url'][:100]}")

        try:
            text = fetch_article_text(cand["url"])
        except Exception as e:
            print(f"  ! fetch failed: {e}")
            text = None
        if text is None:
            cand["attempts"] += 1
            if cand["attempts"] < MAX_FETCH_ATTEMPTS:
                remaining.append(cand)
            else:
                rejected.append({"url": cand["url"], "reason": "unfetchable"})
            continue

        try:
            result = call_model(token, cand, text)
        except Exception as e:
            print(f"  ! model call failed: {e}")
            remaining.append(cand)
            continue
        if result.get("_rate_limited"):
            print("  ! rate limited by GitHub Models; stopping for this run.")
            remaining.append(cand)
            remaining.extend(candidates[i + 1 :])
            break

        processed += 1
        if not result.get("relevant") or result.get("state", "RI") != "RI":
            reason = result.get("summary", "not relevant")
            if result.get("relevant"):
                reason = f"crash occurred in {result.get('state')}, not RI"
            rejected.append({"url": cand["url"], "reason": reason})
            print(f"  - not relevant: {reason[:80]}")
            continue

        dup = find_duplicate(incidents, result)
        if dup:
            merge_into(dup, result, cand["url"])
            print(f"  = merged into existing incident {dup['id']}")
            continue

        incidents.append(
            {
                "id": incident_id(result),
                "incident_date": result.get("incident_date"),
                "date_precision": result.get("date_precision", "unknown"),
                "city": result.get("city"),
                "location_text": result.get("location_text"),
                "severity": result.get("severity", "unknown"),
                "pedestrians_struck": result.get("pedestrians_struck", 1),
                "victim_age": result.get("victim_age"),
                "hit_and_run": bool(result.get("hit_and_run")),
                "summary": result.get("summary", ""),
                "confidence": result.get("confidence", "medium"),
                "sources": [cand["url"]],
                "lat": None,
                "lon": None,
                "geocode_method": None,
                "geocode_attempts": 0,
                "needs_review": result.get("confidence") == "low",
                "added": datetime.now(timezone.utc).date().isoformat(),
            }
        )
        new_count += 1
        print(f"  + {result.get('severity')}: {result.get('summary', '')[:80]}")
        time.sleep(2)  # stay well under free-tier request rates

    save_json(DATA / "candidates.json", remaining)
    save_json(DATA / "incidents.json", incidents)
    save_json(DATA / "rejected.json", rejected)
    print(f"\n{new_count} new incident(s), {len(remaining)} candidate(s) left in queue.")


if __name__ == "__main__":
    main()
