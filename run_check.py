import json
import sys
import time
from pathlib import Path

from soc_client import fetch_sections
from notify import send_email

CONFIG_PATH = Path(__file__).parent / "watch_config.json"
STATE_PATH = Path(__file__).parent / "state.json"


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def main():
    config = load_json(CONFIG_PATH, {"watches": []})
    state = load_json(STATE_PATH, {})

    changes = []

    for watch in config["watches"]:
        name = watch["name"]
        allowed_sections = watch.get("sections")

        try:
            sections, status_as_of = fetch_sections(watch["term"], watch["subject"], watch["catalog"])
        except Exception as e:
            print(f"[{name}] fetch failed: {e}", file=sys.stderr)
            continue

        if status_as_of:
            state[f"{name}|_status_as_of"] = status_as_of

        if not sections:
            print(f"[{name}] no sections returned (check config)", file=sys.stderr)
            continue

        for sec in sections:
            if allowed_sections and sec["section"] not in allowed_sections:
                continue

            key = f"{name}|{sec['section']}"
            new_sig = f"{sec['status']} | {sec['waitlist']}"
            old_sig = state.get(key)

            if old_sig is not None and old_sig != new_sig:
                changes.append(
                    f"{name} {sec['section']} ({sec['instructor']}, {sec['day']} {sec['time']}):\n"
                    f"  was: {old_sig}\n"
                    f"  now: {new_sig}"
                )

            state[key] = new_sig

    # Always bump this so state.json changes every run -> keeps a commit flowing,
    # which prevents GitHub from auto-disabling the scheduled workflow after 60
    # days of repo inactivity.
    state["_last_checked"] = int(time.time())

    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    if changes:
        body = "\n\n".join(changes)
        print("CHANGES DETECTED:\n" + body)
        send_email("UCLA class status changed", body)
    else:
        print("No changes.")


if __name__ == "__main__":
    main()
