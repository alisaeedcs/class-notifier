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
    # Catalog numbers can carry a single cross-list letter, either before the
    # digits (e.g. "C161") or after them (e.g. "102C", "156A"). UCLA's SOC page
    # encodes these two cases differently in the fixed-width 8-char catalog
    # field: a trailing letter packs right after the digits ("0102C   "), but
    # a leading letter sits in a fixed slot with gaps around it ("0161  C ").
    # In both cases the Path field drops whitespace from the subject and
    # appends digits+letter, letter last regardless of which side it's on.
    m = re.match(r"([A-Z]*)(\d+)([A-Z]*)$", catalog.strip().upper())
    if not m:
        raise ValueError(f"Could not parse catalog number: {catalog!r}")
    prefix, digits, suffix = m.group(1), m.group(2), m.group(3)
    letter = prefix or suffix
    numeric_field = digits.zfill(4)
    if prefix:
        catalog_field = numeric_field.ljust(6) + letter.ljust(2)
    else:
        catalog_field = (numeric_field + letter).ljust(8)
    path = re.sub(r"\s+", "", subject.strip().upper()) + numeric_field + letter
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

ORIGIN = "https://sa.ucla.edu"
DETAIL_HREF_RE = re.compile("ClassDetail", re.I)
AS_OF_RE = re.compile(r"Status as of[^<.]*")
TEMPLATE_TAG_RE = re.compile(r"</?template[^>]*>", re.I)


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

        detail = row.find("a", href=DETAIL_HREF_RE)

        out.append({
            "section": section,
            "status": _clean(cols["statusColumn"]),
            "waitlist": _clean(cols["waitlistColumn"]),
            "day": day,
            "time": time_,
            "location": cols["locationColumn"],
            "units": cols["unitsColumn"],
            "instructor": cols["instructorColumn"],
            "detail_url": ORIGIN + detail["href"] if detail else "",
        })
    return out


def enrollment_state(text: str) -> str:
    """Coarse enrollment state, so the summary table and the detail page can be
    compared even though they word things differently ('Open 39 of 40 Enrolled 1
    Spots Left' vs 'Open: 1 of 40 Left')."""
    m = re.match(r"\s*(open|closed|waitlist)", text or "", re.IGNORECASE)
    return m.group(1).upper() if m else ""


def fetch_class_detail(detail_url: str, session: requests.Session = None,
                       referer: str = None) -> dict:
    """Fetch one section's detail page.

    Unlike the summary table, this page reports a section's seat status *and*
    the time that status was last refreshed in the same response, and the
    timestamp is per-section rather than a page-wide banner. That pairing is
    what makes it possible to tell a genuinely new reading from an older one
    replayed by a different server."""
    sess = session or requests.Session()
    sess.headers.update({"User-Agent": UA})

    resp = sess.get(detail_url, headers={"Referer": referer} if referer else {}, timeout=25)
    resp.raise_for_status()

    # The detail page renders into a shadow DOM, so the section's real table sits
    # inside a <template> that html.parser won't expose as elements. Unwrapping
    # the template tags puts it back in the normal tree (and avoids taking on an
    # lxml/html5lib dependency just for this page).
    soup = BeautifulSoup(TEMPLATE_TAG_RE.sub("", resp.text), "html.parser")

    as_of_match = AS_OF_RE.search(resp.text)
    # The header row carries the same class as the data row, so pick the first
    # one that actually has data cells rather than <th> headings.
    cells = []
    for row in soup.find_all("tr", class_="enrl_mtng_info"):
        cells = row.find_all("td")
        if cells:
            break

    return {
        "status": _clean(cells[0].get_text(" ", strip=True)) if len(cells) > 0 else "",
        "waitlist": _clean(cells[1].get_text(" ", strip=True)) if len(cells) > 1 else "",
        "as_of": as_of_match.group(0) if as_of_match else "",
    }


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
