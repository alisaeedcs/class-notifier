"""Client for UCLA Registrar's public Schedule of Classes (SOC) section-status API.

Reverse-engineered from the public sa.ucla.edu SOC search UI: the results page
makes an XHR to Results/GetCourseSummary with a JSON `model` describing the
course, and returns an HTML fragment (not the full page) containing the
section table.
"""
import base64
import json
import re
import sys

import requests
from bs4 import BeautifulSoup

BASE = "https://sa.ucla.edu/ro/public/soc"

DEFAULT_FILTER_FLAGS = {
    "enrollment_status": "O,W,C,X,T,S",
    "advanced": "y",
    "meet_days": "M,T,W,R,F",
    "start_time": "8:00 am",
    "end_time": "9:00 pm",
    "meet_locations": None,
    "meet_units": None,
    "instructor": None,
    "class_career": None,
    "impacted": None,
    "enrollment_restrictions": None,
    "enforced_requisites": None,
    "individual_studies": None,
    "summer_session": None,
}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


def build_model(term: str, subject: str, catalog: str) -> dict:
    subject_field = subject.strip().upper().ljust(7)
    m = re.match(r"(\d+)(\D*)", catalog.strip())
    if not m:
        raise ValueError(f"Could not parse catalog number: {catalog!r}")
    digits, suffix = m.group(1), m.group(2)
    numeric_field = digits.zfill(4) + suffix
    catalog_field = numeric_field.ljust(8)
    path = subject.strip().upper() + numeric_field
    token = base64.b64encode((catalog_field + path).encode()).decode()
    return {
        "Term": term,
        "SubjectAreaCode": subject_field,
        "CatalogNumber": catalog_field,
        "IsRoot": True,
        "SessionGroup": "%",
        "ClassNumber": "%",
        "SequenceNumber": None,
        "Path": path,
        "MultiListedClassFlag": "n",
        "Token": token,
    }


def _search_url(term: str, subject: str) -> str:
    subj_q = subject.strip().upper().ljust(7).replace(" ", "+")
    return (f"{BASE}/Results?SubjectAreaName={subject.strip()}"
            f"&t={term}&sBy=subject&subj={subj_q}&catlg=&cls_no="
            f"&undefined=Go&btnIsInIndex=btn_inIndex")


COLUMN_CLASSES = ["sectionColumn", "statusColumn", "waitlistColumn", "dayColumn",
                   "timeColumn", "locationColumn", "unitsColumn", "instructorColumn"]


def _clean(text: str) -> str:
    text = re.sub(r"(?<=[a-z0-9\)])(?=[A-Z])", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for row in soup.find_all("div", class_="data_row"):
        cols = {name: row.find(class_=name) for name in COLUMN_CLASSES}
        cols = {name: (tag.get_text(" ", strip=True) if tag else "") for name, tag in cols.items()}

        section_raw = cols["sectionColumn"]
        # e.g. "Lec 1 Lec 1" (label duplicated) -> "Lec 1"
        sec_match = re.match(r"(\S+\s+\d+)", section_raw)
        section = sec_match.group(1) if sec_match else section_raw

        day = cols["dayColumn"]
        time_ = cols["timeColumn"]
        if day and time_.startswith(day):
            time_ = time_[len(day):].strip()
        time_ = re.sub(r"\s*-\s*", "-", time_)

        out.append({
            "section": section,
            "status": _clean(cols["statusColumn"]),
            "waitlist": _clean(cols["waitlistColumn"]),
            "day": day,
            "time": time_,
            "location": cols["locationColumn"],
            "units": cols["unitsColumn"],
            "instructor": cols["instructorColumn"],
        })
    return out


def fetch_sections(term: str, subject: str, catalog: str, session: requests.Session = None) -> tuple[list[dict], str]:
    """Returns (sections, status_as_of). status_as_of is the "Status as of H:MM PM"
    banner text from the search page, informational only (the page itself can be
    CDN-cached, so this timestamp isn't reliable enough to sync polling against)."""
    sess = session or requests.Session()
    sess.headers.update({"User-Agent": UA})

    search_url = _search_url(term, subject)
    warmup = sess.get(search_url, timeout=20)  # warm up session cookies
    as_of_match = re.search(r"Status as of[^<.]*", warmup.text)
    status_as_of = as_of_match.group(0) if as_of_match else ""

    model = build_model(term, subject, catalog)
    params = {
        "model": json.dumps(model, separators=(",", ":")),
        "FilterFlags": json.dumps(DEFAULT_FILTER_FLAGS, separators=(",", ":")),
        "_": "0",
    }
    resp = sess.get(f"{BASE}/Results/GetCourseSummary", params=params,
                     headers={"Accept": "*/*", "X-Requested-With": "XMLHttpRequest",
                              "Referer": search_url},
                     timeout=20)
    resp.raise_for_status()
    return parse_html(resp.text), status_as_of


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python3 soc_client.py <SUBJECT> <CATALOG> <TERM>")
        print("Example: python3 soc_client.py STATS 102C 26F")
        sys.exit(1)
    subject, catalog, term = sys.argv[1], sys.argv[2], sys.argv[3]
    sections, status_as_of = fetch_sections(term, subject, catalog)
    if status_as_of:
        print(status_as_of)
    if not sections:
        print("No sections found (check subject/catalog/term).")
    for s in sections:
        print(f"{s['section']:8} {s['status']:30} {s['waitlist']:20} "
              f"{s['day']:6} {s['time']:20} {s['instructor']}")
