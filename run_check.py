import datetime
import json
import re
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from soc_client import (enrollment_state, fetch_class_detail, fetch_sections,
                        _search_url)
from notify import send_email

CONFIG_PATH = Path(__file__).parent / "watch_config.json"
STATE_PATH = Path(__file__).parent / "state.json"

# How long we'll keep rejecting older-looking readings before accepting one
# anyway. Comfortably longer than the ~90 min of server skew seen in practice.
STALE_GRACE_SECONDS = 3 * 60 * 60

# UCLA publishes section timestamps in Pacific time.
PACIFIC = ZoneInfo("America/Los_Angeles")


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def parse_as_of(text: str):
    """'Status as of 11:43 AM' -> minutes since midnight, or None if unparseable."""
    m = re.search(r"(\d{1,2}):(\d{2})\s*([AP])\.?M", text or "", re.IGNORECASE)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    hour = hour % 12 + (12 if m.group(3).upper() == "P" else 0)
    return hour * 60 + minute


def as_of_epoch(text: str, now: int):
    """Anchor a bare 'Status as of 11:43 AM' to an absolute time.

    UCLA publishes a time of day with no date, in Pacific time. Comparing two of
    those as clock readings is ambiguous - a genuine 13-hour gap is
    indistinguishable from the clock running backwards - so resolve each one
    against the wall clock instead. The timestamp always describes a refresh that
    already happened, so it is the most recent occurrence of that time of day."""
    minutes = parse_as_of(text)
    if minutes is None:
        return None
    now_local = datetime.datetime.fromtimestamp(now, PACIFIC)
    stamped = now_local.replace(hour=minutes // 60, minute=minutes % 60,
                                second=0, microsecond=0)
    if stamped > now_local + datetime.timedelta(minutes=5):  # tolerate slight skew
        stamped -= datetime.timedelta(days=1)
    return int(stamped.timestamp())


def confirm_change(sec, key, state, now, session, referer):
    """Verify a change the summary table reported against the section's own
    detail page, which is the only place UCLA publishes a section's seat status
    and the time that status was refreshed *in the same response*.

    The summary table carries no timestamp of its own, and UCLA serves it from
    several servers whose snapshots can differ by hours, so a section sitting on
    a seat boundary otherwise appears to flap open/closed forever. A reading is
    accepted only when the detail page agrees the seats really moved and its
    timestamp is no older than the last one we accepted for this section."""
    if not sec.get("detail_url"):
        return True  # nothing to check against; take the summary at its word

    try:
        detail = fetch_class_detail(sec["detail_url"], session=session, referer=referer)
    except Exception as e:
        print(f"[{key}] detail fetch failed ({e}) - holding off", file=sys.stderr)
        return False

    if enrollment_state(detail["status"]) != enrollment_state(sec["status"]):
        print(f"[{key}] summary says {sec['status']!r} but detail says "
              f"{detail['status']!r} - holding off", file=sys.stderr)
        return False

    new_epoch = as_of_epoch(detail["as_of"], now)
    prev_epoch = state.get(f"{key}|_as_of_epoch")
    accepted_at = state.get(f"{key}|_accepted_at", 0)
    # Never hold out past the grace window, so a bad timestamp can't wedge a
    # section permanently.
    if (new_epoch is not None and prev_epoch is not None and new_epoch < prev_epoch
            and now - accepted_at < STALE_GRACE_SECONDS):
        print(f"[{key}] stale reading ({detail['as_of']}; already accepted "
              f"{state.get(f'{key}|_as_of')}) - ignoring", file=sys.stderr)
        return False

    if new_epoch is not None:
        state[f"{key}|_as_of"] = detail["as_of"]
        state[f"{key}|_as_of_epoch"] = new_epoch
        state[f"{key}|_accepted_at"] = now
    return True


def main():
    config = load_json(CONFIG_PATH, {"watches": []})
    state = load_json(STATE_PATH, {})

    now = int(time.time())
    changes = []
    session = requests.Session()

    for watch in config["watches"]:
        name = watch["name"]
        allowed_sections = watch.get("sections")

        try:
            sections, status_as_of = fetch_sections(watch["term"], watch["subject"],
                                                    watch["catalog"], session=session)
        except Exception as e:
            print(f"[{name}] fetch failed: {e}", file=sys.stderr)
            continue

        if not sections:
            print(f"[{name}] no sections returned (check config)", file=sys.stderr)
            continue

        if status_as_of:  # informational only; it isn't tied to the seat data
            state[f"{name}|_status_as_of"] = status_as_of

        referer = _search_url(watch["term"], watch["subject"])

        for sec in sections:
            if allowed_sections and sec["section"] not in allowed_sections:
                continue

            key = f"{name}|{sec['section']}"
            new_sig = f"{sec['status']} | {sec['waitlist']}"
            old_sig = state.get(key)

            if old_sig is not None and old_sig != new_sig:
                # Leave the recorded value alone when a change can't be
                # confirmed, so the next run checks it again.
                if not confirm_change(sec, key, state, now, session, referer):
                    continue
                changes.append(
                    f"{name} {sec['section']} ({sec['instructor']}, {sec['day']} {sec['time']}):\n"
                    f"  was: {old_sig}\n"
                    f"  now: {new_sig}"
                )

            state[key] = new_sig

    # Always bump this so state.json changes every run -> keeps a commit flowing,
    # which prevents GitHub from auto-disabling the scheduled workflow after 60
    # days of repo inactivity.
    state["_last_checked"] = now

    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    if changes:
        body = "\n\n".join(changes)
        print("CHANGES DETECTED:\n" + body)
        send_email("UCLA class status changed", body)
    else:
        print("No changes.")


if __name__ == "__main__":
    main()
