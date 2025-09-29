import os
import pandas as pd
import requests
from ics import Calendar, Event

# =====================================================
# CONFIG
# =====================================================
DAYFIRST = True                     # interpret dates as DD/MM/YYYY
DEFAULT_TIMED_DURATION_HOURS = 1    # fallback if end missing/invalid
OUTPUT_DIR = "public/calendars"
OUTPUT_INDEX = "public/index.html"
OUTPUT_JSON = "public/calendars.json"

csv_url = os.getenv("CSV_URL")
if not csv_url:
    raise ValueError("❌ CSV_URL environment variable is not set (add a repo secret named CSV_URL).")

# =====================================================
# HELPERS
# =====================================================
def parse_dt(v):
    """Parse date/time safely with dayfirst=True."""
    if pd.isna(v):
        return None
    dt = pd.to_datetime(v, errors="coerce", dayfirst=DAYFIRST, utc=False)
    if pd.isna(dt):
        return None
    return dt

def is_midnight(d):
    return d is not None and (getattr(d, "hour", 0), getattr(d, "minute", 0), getattr(d, "second", 0)) == (0, 0, 0)

# =====================================================
# LOAD SHEET
# =====================================================
print(f"📥 Downloading CSV from {csv_url}")
resp = requests.get(csv_url)
resp.raise_for_status()

df = pd.read_csv(pd.compat.StringIO(resp.text))

# Expecting at least: Calendar, Title, Start, End, Description, Location
expected_cols = ["Calendar", "Title", "Start", "End", "Description", "Location"]
missing = [c for c in expected_cols if c not in df.columns]
if missing:
    raise ValueError(f"❌ Missing required columns in sheet: {missing}")

os.makedirs(OUTPUT_DIR, exist_ok=True)

# Group by Calendar
calendars = {}

for _, row in df.iterrows():
    cal_name = str(row["Calendar"]).strip()
    title = str(row["Title"]).strip() if not pd.isna(row["Title"]) else "(No Title)"
    start = row["Start"]
    end = row["End"]
    desc = row.get("Description", "")
    loc = row.get("Location", "")

    start_dt = parse_dt(start)
    end_dt = parse_dt(end) if not pd.isna(end) else None

    if start_dt is None and end_dt is None:
        continue  # skip empty rows

    ev = Event()
    ev.name = title
    if not pd.isna(desc): ev.description = str(desc)
    if not pd.isna(loc): ev.location = str(loc)

    # ----- TIMING FIXES -----
    if start_dt is not None and is_midnight(start_dt) and (end_dt is None or is_midnight(end_dt)):
        # All-day
        ev.begin = start_dt.date()
        ev.make_all_day()
        if end_dt is None or end_dt.date() <= start_dt.date():
            ev.end = (start_dt + pd.Timedelta(days=1)).date()
        else:
            ev.end = end_dt.date()
    else:
        # Timed
        if start_dt is None and end_dt is not None:
            start_dt = end_dt - pd.Timedelta(hours=DEFAULT_TIMED_DURATION_HOURS)
        if end_dt is None or end_dt <= start_dt:
            end_dt = start_dt + pd.Timedelta(hours=DEFAULT_TIMED_DURATION_HOURS)
        ev.begin = start_dt
        ev.end = end_dt
    # ------------------------

    if cal_name not in calendars:
        calendars[cal_name] = Calendar()
    calendars[cal_name].events.add(ev)

# =====================================================
# WRITE FILES
# =====================================================
index_html = ["<h1>Calendar Feeds</h1><ul>"]
json_entries = []

for cal_name, cal in calendars.items():
    safe_name = cal_name.lower().replace(" ", "-")
    ics_path = os.path.join(OUTPUT_DIR, f"{safe_name}.ics")

    with open(ics_path, "w", encoding="utf-8") as f:
        f.writelines(cal.serialize_iter())

    rel_path = f"calendars/{safe_name}.ics"
    index_html.append(f"<li><a href='{rel_path}'>{cal_name}</a></li>")
    json_entries.append({"name": cal_name, "path": rel_path})

index_html.append("</ul>")
with open(OUTPUT_INDEX, "w", encoding="utf-8") as f:
    f.write("\n".join(index_html))

import json
with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
    json.dump(json_entries, f, indent=2)

print("✅ Build complete")
