#!/usr/bin/env python3
"""
Seabreeze forecast logger
=========================
Downloads a seabreeze.com.au wind-forecast page, saves a complete raw copy,
and appends the decoded numbers to CSV files so you can look back over time.

Needs only Python 3.8+ (standard library, nothing to pip install).

    python seabreeze_logger.py                 fetch now and record
    python seabreeze_logger.py --from-file x   record from a saved .html/.html.gz
                                               (also rebuilds CSVs from raw/ copies)

Output (all inside this script's folder):

    data/runs.csv                 one row per run (when, what the page said)
    data/forecast_wind_wave.csv   7-day wind + wave forecast, as issued each night
    data/forecast_daily.csv       7-day daily min/max, summary, rain chance/amount
    data/forecast_temperature.csv hourly temperature forecast
    data/forecast_rain.csv        3-hourly rain forecast
    data/forecast_tide.csv        tide predictions
    data/obs_wind.csv             observed wind + gust at nearby stations (10 min)
    data/obs_pressure_temp.csv    observed temperature + pressure (10 min)
    data/obs_tide.csv             "live tide" graph
    raw/YYYY/...html.gz           the whole page exactly as downloaded
    raw/YYYY/...json.gz           just the data block the page's graphs are drawn from
    logger.log                    what happened on each run

Forecast files grow by one full 7-day forecast per run, tagged with
`captured_local`, so "what was forecast for Saturday, as of Tuesday night?" is
a filter on two columns.  Observation files are de-duplicated on
(station, time), so overlapping runs never double up.
"""

import argparse
import csv
import gzip
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
URL = "https://www.seabreeze.com.au/weather/wind-forecast/sydney"
KEEP_RAW_HTML = True          # ~60-100 KB per run once gzipped
ATTEMPTS = 3                  # tries per fetch method
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
RAW_DIR = os.path.join(HERE, "raw")
LOG_FILE = os.path.join(HERE, "logger.log")
MARKER = "var json="


def log(msg):
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ----------------------------------------------------------------------------
# Fetching
# ----------------------------------------------------------------------------
def fetch_direct(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
        "Accept-Encoding": "gzip",
        "Cache-Control": "no-cache",
        "Upgrade-Insecure-Requests": "1",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read()
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            body = gzip.decompress(body)
    return body.decode("utf-8", errors="replace")


def find_browser():
    """Edge ships with Windows; Chrome is used if Edge is missing."""
    candidates = []
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        root = os.environ.get(env)
        if root:
            candidates.append(os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"))
            candidates.append(os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    for name in ("msedge", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def fetch_browser(url):
    """Fallback: load the page in a headless (invisible) Edge/Chrome window.
    Used only when the plain download is refused by the site's bot filter."""
    exe = find_browser()
    if not exe:
        raise RuntimeError("no Edge or Chrome found for the browser fallback")
    profile = os.path.join(HERE, ".browser-profile")
    cmd = [
        exe, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", "--window-size=1280,900",
        "--user-data-dir=" + profile, "--user-agent=" + USER_AGENT,
        "--virtual-time-budget=20000", "--dump-dom", url,
    ]
    if os.name != "nt":
        cmd.insert(1, "--no-sandbox")   # needed on Linux servers (e.g. GitHub Actions)
    out = subprocess.run(cmd, capture_output=True, timeout=120)
    return out.stdout.decode("utf-8", errors="replace")


def fetch(url):
    """Returns (html, method). Raises if nothing produced the data block."""
    last_err = None
    for method, fn in (("direct", fetch_direct), ("browser", fetch_browser)):
        for attempt in range(1, ATTEMPTS + 1):
            try:
                html = fn(url)
                if MARKER in html:
                    return html, method
                last_err = "%s: page came back (%d chars) without the data block" % (method, len(html))
            except urllib.error.HTTPError as exc:
                last_err = "%s: HTTP %s" % (method, exc.code)
            except Exception as exc:  # noqa: BLE001 - log and try the next option
                last_err = "%s: %s: %s" % (method, type(exc).__name__, exc)
            log("fetch attempt %d failed (%s)" % (attempt, last_err))
            if attempt < ATTEMPTS:
                time.sleep(20 * attempt)
    raise RuntimeError("could not download the forecast page; last error: %s" % last_err)


# ----------------------------------------------------------------------------
# Decoding
# ----------------------------------------------------------------------------
def extract_json(html):
    start = html.index(MARKER) + len(MARKER)
    obj, _ = json.JSONDecoder().raw_decode(html[start:].lstrip())
    return obj


def parse_stamp(s):
    """'202611120141' -> naive local datetime."""
    return datetime.strptime(s[:12], "%Y%m%d%H%M")


def round_to(dt, minutes):
    step = minutes * 60
    secs = (dt - datetime(2000, 1, 1)).total_seconds()
    return datetime(2000, 1, 1) + timedelta(seconds=round(secs / step) * step)


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


def num(s):
    """Blank and the site's -9999 placeholder become empty cells."""
    s = s.strip()
    if s in ("", "-9999"):
        return ""
    return s


class Graph(object):
    """One comma-packed series from the page.

    Layout: [0] type  [2] station code  [7] window start (hours x 100 after
    local midnight)  [9] station name  [10] fields per record
    [11] header length  [12] window start yyyymmddHHMM  [13] window end.
    After the header come the records; the first field of each is the time,
    again in hours x 100 after that same midnight.
    """

    def __init__(self, text):
        f = text.split(",")
        self.fields = f
        self.type = f[0]
        self.code = f[2]
        self.name = f[9].strip()
        self.width = int(f[10])
        self.hlen = int(f[11])
        self.header = f[:self.hlen]
        body = f[self.hlen:]
        if self.width < 1 or len(body) % self.width:
            raise ValueError("%s/%s: %d data fields is not a multiple of %d"
                             % (self.type, self.code, len(body), self.width))
        self.records = [body[i:i + self.width] for i in range(0, len(body), self.width)]
        start = parse_stamp(f[12])
        self.midnight = round_to(start - timedelta(hours=float(f[7]) / 100.0), 10)

    def when(self, value):
        return round_to(self.midnight + timedelta(hours=float(value) / 100.0), 1)


# ----------------------------------------------------------------------------
# CSV writing
# ----------------------------------------------------------------------------
def append_rows(name, columns, rows, key_cols=None):
    """Append rows to data/<name>.csv. With key_cols, rows whose key is already
    in the file are skipped (used for observations). Returns rows written."""
    if not rows:
        return 0
    path = os.path.join(DATA_DIR, name + ".csv")
    exists = os.path.isfile(path) and os.path.getsize(path) > 0
    if exists:
        with open(path, newline="", encoding="utf-8") as fh:
            have = next(csv.reader(fh), [])
        if have != columns:
            # The column set changed (site or script update): start a new file
            # rather than writing misaligned rows into the old one.
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            os.replace(path, os.path.join(DATA_DIR, "%s.before-%s.csv" % (name, stamp)))
            log("%s.csv had different columns; old file kept as %s.before-%s.csv" % (name, name, stamp))
            exists = False
    seen = set()
    if exists and key_cols:
        idx = [columns.index(c) for c in key_cols]
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            next(reader, None)
            for row in reader:
                if len(row) == len(columns):
                    seen.add(tuple(row[i] for i in idx))
    written = 0
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if not exists:
            writer.writerow(columns)
        for row in rows:
            row = [str(v) for v in row]
            if key_cols:
                key = tuple(row[columns.index(c)] for c in key_cols)
                if key in seen:
                    continue
                seen.add(key)
            writer.writerow(row)
            written += 1
    return written


def lead_hours(valid, captured):
    return "%.1f" % ((valid - captured).total_seconds() / 3600.0)


# ----------------------------------------------------------------------------
# Recording one page
# ----------------------------------------------------------------------------
def record(html, method, captured_utc, save_raw=True):
    page = extract_json(html)
    g = page.get("global", {})
    offset = float(g.get("utcHoursOffset", 0))
    captured = (captured_utc + timedelta(hours=offset)).replace(tzinfo=None)
    cap = fmt(captured)
    location = g.get("location", "")
    issued = g.get("forecastInfoUid", "")
    if len(issued) >= 12:  # UTC stamp of the forecast model run shown on the page
        issued = fmt(parse_stamp(issued) + timedelta(hours=offset))

    os.makedirs(DATA_DIR, exist_ok=True)
    if save_raw:
        year_dir = os.path.join(RAW_DIR, captured.strftime("%Y"))
        os.makedirs(year_dir, exist_ok=True)
        stem = os.path.join(year_dir, "%s_%s" % (g.get("locationSeoName", "page"),
                                                 captured.strftime("%Y-%m-%d_%H%M")))
        with gzip.open(stem + ".json.gz", "wt", encoding="utf-8") as fh:
            json.dump(page, fh)
        if KEEP_RAW_HTML:
            with gzip.open(stem + ".html.gz", "wt", encoding="utf-8") as fh:
                fh.write(html)

    counts = {}
    problems = []
    FKEY = ["captured_local", "location", "valid_local"]  # re-running never doubles up

    def add(table, columns, rows, key_cols=None):
        counts[table] = counts.get(table, 0) + append_rows(table, columns, rows, key_cols)

    for text in page.get("data", []):
        try:
            gr = Graph(text)
        except Exception as exc:  # noqa: BLE001
            problems.append(str(exc))
            continue
        try:
            t = gr.type
            if t == "F":      # time, wind kn, wind dir, wave m, wave dir, wave period s
                add("forecast_wind_wave",
                    ["captured_local", "forecast_updated_local", "location", "valid_local",
                     "lead_hours", "wind_kn", "wind_dir", "wave_height_m", "wave_dir", "wave_period_s"],
                    [[cap, issued, location, fmt(gr.when(r[0])), lead_hours(gr.when(r[0]), captured),
                      num(r[1]), r[2], num(r[3]), r[4], num(r[5])] for r in gr.records],
                    key_cols=FKEY)
                # Daily summaries sit in the header in groups of 12:
                # min, max, day number, summary, icon, rain chance %, rain amount, (5 unused)
                days = []
                i = 30
                while i + 12 <= gr.hlen:
                    d = gr.header[i:i + 12]
                    if not d[2].strip().isdigit():
                        break
                    date = (gr.midnight + timedelta(days=int(d[2]))).strftime("%Y-%m-%d")
                    days.append([cap, issued, location, date, d[2], num(d[0]), num(d[1]),
                                 d[3], d[4], num(d[5]), d[6]])
                    i += 12
                add("forecast_daily",
                    ["captured_local", "forecast_updated_local", "location", "date", "day_ahead",
                     "temp_min_c", "temp_max_c", "summary", "icon", "rain_chance_pct", "rain_amount"],
                    days, key_cols=["captured_local", "location", "date"])
            elif t == "E":
                pass          # same wave height/dir/period as "F"; kept in raw only
            elif t == "C":    # time, temp, (unused), flag: 1 = day's min, 2 = day's max
                add("forecast_temperature",
                    ["captured_local", "forecast_updated_local", "location", "valid_local",
                     "lead_hours", "temp_c", "daily_extreme"],
                    [[cap, issued, location, fmt(gr.when(r[0])), lead_hours(gr.when(r[0]), captured),
                      num(r[1]), {"1": "min", "2": "max"}.get(r[3].strip(), "")] for r in gr.records],
                    key_cols=FKEY)
            elif t == "R":    # time, chance %, two mm values (see README)
                add("forecast_rain",
                    ["captured_local", "forecast_updated_local", "location", "valid_local",
                     "lead_hours", "rain_chance_pct", "rain_mm_a", "rain_mm_b"],
                    [[cap, issued, location, fmt(gr.when(r[0])), lead_hours(gr.when(r[0]), captured),
                      num(r[1]), num(r[2]), num(r[3])] for r in gr.records],
                    key_cols=FKEY)
            elif t == "S":    # time, height m
                add("forecast_tide",
                    ["captured_local", "station", "valid_local", "tide_height_m"],
                    [[cap, gr.name.title(), fmt(gr.when(r[0])), num(r[1])] for r in gr.records],
                    key_cols=["captured_local", "station", "valid_local"])
            elif t == "W":    # time, wind kn, gust kn, direction
                add("obs_wind",
                    ["station", "time_local", "wind_kn", "gust_kn", "wind_dir", "captured_local"],
                    [[gr.name, fmt(gr.when(r[0])), num(r[1]), num(r[2]), r[3], cap] for r in gr.records],
                    key_cols=["station", "time_local"])
            elif t == "P":    # time, temp, pressure
                add("obs_pressure_temp",
                    ["station", "time_local", "temp_c", "pressure_hpa", "captured_local"],
                    [[gr.name, fmt(gr.when(r[0])), num(r[1]), num(r[2]), cap] for r in gr.records],
                    key_cols=["station", "time_local"])
            elif t == "T":    # time, height m
                add("obs_tide",
                    ["station", "time_local", "tide_height_m", "captured_local"],
                    [[gr.name, fmt(gr.when(r[0])), num(r[1]), cap] for r in gr.records],
                    key_cols=["station", "time_local"])
            else:
                problems.append("unknown graph type %r (%s) - kept in raw only" % (t, gr.name))
        except Exception as exc:  # noqa: BLE001
            problems.append("%s/%s: %s: %s" % (gr.type, gr.code, type(exc).__name__, exc))

    append_rows("runs",
                ["captured_local", "captured_utc", "location", "fetch_method",
                 "forecast_updated_local", "forecast_info", "live_info",
                 "sunrise", "sunset", "rows_added", "problems"],
                [[cap, captured_utc.strftime("%Y-%m-%d %H:%M"), location, method, issued,
                  g.get("forecastInfoText", ""), g.get("liveInfoText", ""),
                  g.get("sunriseText", ""), g.get("sunsetText", ""),
                  "; ".join("%s=%d" % kv for kv in sorted(counts.items())),
                  " | ".join(problems)]],
                key_cols=["captured_local", "location"])
    for p in problems:
        log("WARNING: " + p)
    log("recorded %s (%s, via %s): %s" % (location, cap, method,
                                         ", ".join("%s +%d" % kv for kv in sorted(counts.items()))))
    return counts, problems


def read_saved(path):
    opener = gzip.open if path.lower().endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    if path.lower().endswith((".json", ".json.gz")):
        text = MARKER + text   # a saved data block, not a whole page
    return text


def main():
    ap = argparse.ArgumentParser(description="Record a Seabreeze forecast page to CSV.")
    ap.add_argument("--url", default=URL)
    ap.add_argument("--from-file", metavar="PATH",
                    help="record from a saved .html / .html.gz / .json.gz instead of downloading")
    ap.add_argument("--captured", metavar="'YYYY-MM-DD HH:MM'",
                    help="with --from-file: the UTC time that page was captured")
    args = ap.parse_args()
    try:
        if args.from_file:
            html = read_saved(args.from_file)
            when = (datetime.strptime(args.captured, "%Y-%m-%d %H:%M") if args.captured
                    else datetime.fromtimestamp(os.path.getmtime(args.from_file), timezone.utc).replace(tzinfo=None))
            record(html, "file", when, save_raw=False)
        else:
            html, method = fetch(args.url)
            record(html, method, datetime.now(timezone.utc).replace(tzinfo=None))
        return 0
    except Exception as exc:  # noqa: BLE001
        log("FAILED: %s: %s" % (type(exc).__name__, exc))
        log(traceback.format_exc().strip())
        return 1


if __name__ == "__main__":
    sys.exit(main())
