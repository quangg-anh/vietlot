import os, re, logging
from datetime import datetime
import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)
DEFAULT_URL = "https://vietlott.vn/vi/trung-thuong/ket-qua-trung-thuong/655"

def fetch_candidate_rows():
    url = os.getenv("VIETLOTT_RESULTS_URL", DEFAULT_URL)
    response = requests.get(url, timeout=20, headers={"User-Agent": "Power655ResearchBot/1.0 (manual research; contact admin)"})
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    text = soup.get_text(" ", strip=True)
    # Conservative heuristic only: do not silently treat arbitrary page numbers as results.
    # Current official page markup may be dynamic; review parser output before relying on it.
    found = []
    date_patterns = re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text)
    number_groups = re.findall(r"(?<!\d)(\d{2}(?:\s+\d{2}){6})(?!\d)", text)
    for date_match, group in zip(date_patterns, number_groups):
        day, month, year = map(int, date_match)
        nums = [int(x) for x in group.split()]
        if len(nums) == 7 and all(1 <= x <= 55 for x in nums) and len(set(nums)) == 7:
            found.append({"date": datetime(year, month, day).date(), "numbers": nums, "source_url": url})
    return found

def run_collection(db):
    from .models import Draw
    candidates = fetch_candidate_rows()
    # Parsing a page is not independent verification; mark as unverified until reconciled.
    for item in candidates:
        log.info("Parsed candidate from official page date=%s numbers=%s; verify before import",
                 item["date"], item["numbers"])
    return {"parsed_candidates": len(candidates), "imported": 0,
            "message": "Candidates logged only. This conservative starter does not auto-import unverified scrape results."}
