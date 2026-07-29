#!/usr/bin/env python3
"""
Odyssey 70mm IMAX seat-pair monitor (AMC Metreon, via imaxmonitor.com).

Alerts (via ntfy.sh push) the moment TWO ADJACENT bookable seats appear
together in rows E-N, for any showtime in a date window.

Trigger definition (per request):
  - 2 seats next to each other (same row, consecutive columns)
  - rows E through N only  ->  E,F,G,H,J,K,L,M,N  (note: no row "I")
  - type must be "CanReserve" (excludes Wheelchair / Companion seats)
  - any showtime from START_DATE through END_DATE inclusive
  - EXCLUDING the dates in EXCLUDED_DATES

Polling design (so we can check every ~60s without hammering the site):
  Each cycle fetches ONE cheap summary call (all showtimes, with each show's
  availableSeats + lastSeatFetchAt). A show's full seat map is only fetched
  when imaxmonitor has re-scanned it since we last looked (its lastSeatFetchAt
  changed) or its seat count changed. Upstream only re-scans each show every
  ~30-60 min, so steady-state cost is ~1 request/minute.

No dependencies beyond the Python standard library.
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

# ----------------------------------------------------------------------------
# Configuration (override with environment variables)
# ----------------------------------------------------------------------------
API_BASE = "https://imaxmonitor.com/api/trpc"

START_DATE = os.environ.get("START_DATE", "2026-07-28")   # inclusive
END_DATE   = os.environ.get("END_DATE",   "2026-08-15")   # inclusive
EXCLUDED_DATES = set(
    d.strip() for d in os.environ.get(
        "EXCLUDED_DATES", "2026-08-07,2026-08-08,2026-08-09"
    ).split(",") if d.strip()
)

# Rows to consider (letters). Odyssey theater has no row "I".
ALLOWED_ROWS = set(
    r.strip().upper() for r in os.environ.get(
        "ALLOWED_ROWS", "E,F,G,H,J,K,L,M,N"
    ).split(",") if r.strip()
)

ALLOWED_TYPES = {"CanReserve"}          # only real, bookable seats count
GROUP_SIZE = int(os.environ.get("GROUP_SIZE", "2"))

# ntfy push config. Create a hard-to-guess topic; subscribe on both phones.
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC  = os.environ.get("NTFY_TOPIC", "")   # REQUIRED to actually send

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"  # print, never push

# Loop control. LOOP_SECONDS>0 => keep checking every INTERVAL_SECONDS until
# LOOP_SECONDS have elapsed, then exit (GitHub Actions cron re-launches us).
INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", "60"))
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "0"))

UA = {"User-Agent": "seat-monitor/1.1 (personal use)"}
TIMEOUT = 30


# ----------------------------------------------------------------------------
# imaxmonitor tRPC client
# ----------------------------------------------------------------------------
def _trpc_get(procedure, payload):
    inp = {"0": {"json": payload}}
    qs = urllib.parse.urlencode({"batch": "1", "input": json.dumps(inp)})
    url = f"{API_BASE}/{procedure}?{qs}"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data[0]["result"]["data"]["json"]


def get_showtimes():
    return _trpc_get("monitor.showtimes", None)["showtimes"]


def get_seat_map(amc_showtime_id):
    return _trpc_get("monitor.seatMap", {"amcShowtimeId": amc_showtime_id})


# ----------------------------------------------------------------------------
# Matching logic
# ----------------------------------------------------------------------------
def in_window(business_date):
    return (START_DATE <= business_date <= END_DATE
            and business_date not in EXCLUDED_DATES)


_seat_re = re.compile(r"^([A-Z]+)(\d+)$")


def _seat_key(name):
    m = _seat_re.match(name)
    return (m.group(1), int(m.group(2))) if m else (name, 0)


def find_adjacent_pairs(seat_map):
    """List of GROUP_SIZE-adjacent bookable seat groups in allowed rows,
    e.g. [['E14','E15'], ...]."""
    seats = seat_map.get("layout", {}).get("seats", [])
    by_row = {}
    for s in seats:
        if not s.get("available") or s.get("type") not in ALLOWED_TYPES:
            continue
        m = _seat_re.match(s.get("name", ""))
        if not m:
            continue
        row_letter = m.group(1).upper()
        if row_letter not in ALLOWED_ROWS:
            continue
        by_row.setdefault(row_letter, []).append((int(s["column"]), s["name"]))

    groups, seen = [], set()
    for cols in by_row.values():
        cols.sort()  # physical left-to-right
        run = [cols[0]]
        for col, name in cols[1:]:
            run = run + [(col, name)] if col == run[-1][0] + 1 else [(col, name)]
            if len(run) >= GROUP_SIZE:
                grp = tuple(sorted((n for _c, n in run[-GROUP_SIZE:]),
                                   key=_seat_key))
                if grp not in seen:
                    seen.add(grp)
                    groups.append(list(grp))
    return groups


# ----------------------------------------------------------------------------
# State
# ----------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        return set(d.get("alerted", [])), d.get("seen", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return set(), {}


def save_state(alerted, seen):
    with open(STATE_FILE, "w") as f:
        json.dump({"alerted": sorted(alerted), "seen": seen}, f, indent=2)


def send_push(title, message, click_url=None):
    if DRY_RUN or not NTFY_TOPIC:
        print(f"  [push skipped] {title}: {message.splitlines()[0]}")
        return
    headers = dict(UA)
    headers["Title"] = title
    headers["Priority"] = "urgent"
    headers["Tags"] = "ticket"
    if click_url:
        headers["Click"] = click_url
    req = urllib.request.Request(f"{NTFY_SERVER}/{NTFY_TOPIC}",
                                 data=message.encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        resp.read()


# ----------------------------------------------------------------------------
# One polling cycle
# ----------------------------------------------------------------------------
def run_cycle(alerted, seen):
    """Mutates alerted/seen in place. Returns (checked, fetched, new_pushes)."""
    showtimes = get_showtimes()
    window = [s for s in showtimes if in_window(s["businessDate"])]

    fetched = new_pushes = 0
    for s in window:
        sid = str(s["amcShowtimeId"])
        avail = s.get("availableSeats", 0)
        fetch_stamp = s.get("lastSeatFetchAt")
        prev = seen.get(sid)

        # Decide whether the full seat map is worth fetching this cycle.
        changed = (prev is None
                   or prev.get("avail") != avail
                   or prev.get("fetch") != fetch_stamp)
        if not changed:
            continue

        # Not enough seats to form a group: record state, drop stale alerts.
        if avail < GROUP_SIZE:
            _forget_show(alerted, sid)
            seen[sid] = {"avail": avail, "fetch": fetch_stamp}
            continue

        try:
            seat_map = get_seat_map(s["amcShowtimeId"])
        except Exception as e:  # noqa: BLE001
            print(f"  ! seat map fetch failed for {sid}: {e}")
            continue
        fetched += 1

        d = datetime.strptime(s["businessDate"], "%Y-%m-%d")
        when = f"{d.strftime('%a %b ')}{d.day} · {s['displayTime']}"
        pairs = find_adjacent_pairs(seat_map)
        cur_keys = {f"{sid}:{'+'.join(g)}": g for g in pairs}

        # Alert on pairs we haven't announced yet.
        for key, grp in cur_keys.items():
            if key in alerted:
                continue
            row = grp[0][0]
            title = when
            msg = f"{len(grp)} seats: {' + '.join(grp)}"
            print(f"  >> NEW MATCH {when}: {grp}")
            send_push(title, msg, click_url=s["buyUrl"])
            alerted.add(key)
            new_pushes += 1

        # Forget this show's old pairs that are gone, so they can re-alert
        # if they reopen later.
        for key in [k for k in alerted if k.startswith(sid + ":")]:
            if key not in cur_keys:
                alerted.discard(key)

        seen[sid] = {"avail": avail, "fetch": fetch_stamp}
        time.sleep(0.3)  # be polite when several maps changed at once

    return len(window), fetched, new_pushes


def _forget_show(alerted, sid):
    for key in [k for k in alerted if k.startswith(sid + ":")]:
        alerted.discard(key)


def main():
    alerted, seen = load_state()
    deadline = time.time() + LOOP_SECONDS
    cycle = 0
    while True:
        cycle += 1
        checked, fetched, pushes = run_cycle(alerted, seen)
        save_state(alerted, seen)
        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] cycle {cycle}: window={checked} "
              f"seatmaps_fetched={fetched} new_pushes={pushes} "
              f"active_alerts={len(alerted)}")
        if LOOP_SECONDS <= 0 or time.time() + INTERVAL_SECONDS > deadline:
            break
        time.sleep(INTERVAL_SECONDS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
