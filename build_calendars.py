# build_calendars.py
# Google Sheet (CSV_URL secret) -> public/calendars/<slug>.ics + calendars.json + index.html
#
# 🎨 COLOR LEGEND (defined in :root CSS variables below)
# Apple – #f5f5f7
# Apple text - #000000
# Google – #ea4335
# Google text - #ffffff
# Outlook – #0078d4
# Outlook text - #ffffff
# Background – #f6f4e8
# Card – #881228
# Copy link button – #ffffff
# Copy link text – #000000
# Chevron – #000000
# Dropdown text - #000000
# Title text - #ffffff
# Sub headline - #f5f5f7

import os
import re
import json
from io import StringIO
from hashlib import md5

import requests
import pandas as pd
from ics import Calendar, Event

# ------------------ Config ------------------
DAYFIRST = True                     # interpret dates as DD/MM/YYYY
DEFAULT_TIMED_DURATION_HOURS = 1    # fallback if end missing/invalid
OUT_DIR = "public"
ICS_DIR = os.path.join(OUT_DIR, "calendars")
MANIFEST_PATH = os.path.join(OUT_DIR, "calendars.json")
INDEX_HTML_PATH = os.path.join(OUT_DIR, "index.html")

CSV_URL = os.getenv("CSV_URL")
if not CSV_URL:
    raise ValueError("❌ CSV_URL environment variable is not set (add a repo secret named CSV_URL).")

# ------------------ Helpers -----------------
_slug_re = re.compile(r"[^a-z0-9]+")

def slugify(name: str) -> str:
    s = (name or "").strip().lower()
    s = _slug_re.sub("-", s).strip("-")
    return s or "calendar"

def clean_str(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    return "" if s.lower() == "nan" else s

def parse_dt(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    dt = pd.to_datetime(v, errors="coerce", dayfirst=DAYFIRST, utc=False)
    if pd.isna(dt):
        return None
    return dt

def is_midnight(dt) -> bool:
    return dt is not None and (getattr(dt, "hour", 0), getattr(dt, "minute", 0), getattr(dt, "second", 0)) == (0, 0, 0)

def make_uid(title, start, end, extra=""):
    s = f"{title}|{start}|{end}|{extra}"
    return md5(s.encode("utf-8")).hexdigest() + "@dynamic-cal"

def first_col(df, names):
    """Return the first matching column name from a list of candidates, else None."""
    cols = {c.strip().lower(): c for c in df.columns}
    for n in names:
        key = n.strip().lower()
        if key in cols:
            return cols[key]
    return None

def combine_date_time(date_val, time_val):
    """Combine separate date and time cells into a single Timestamp (or just date if no time)."""
    d = parse_dt(date_val)
    if d is None:
        return None
    if time_val is None or (isinstance(time_val, float) and pd.isna(time_val)) or str(time_val).strip() == "":
        return d.normalize()  # midnight
    t = pd.to_datetime(str(time_val), errors="coerce", dayfirst=DAYFIRST)
    if pd.isna(t):
        return d.normalize()
    return pd.Timestamp(year=d.year, month=d.month, day=d.day, hour=t.hour, minute=t.minute, second=t.second)

# ------------------ Load CSV ----------------
print(f"📥 Downloading CSV from {CSV_URL}")
resp = requests.get(CSV_URL, timeout=30)
try:
    resp.raise_for_status()
except requests.HTTPError as e:
    raise SystemExit(
        f"❌ Failed to fetch CSV ({e}).\n"
        "➡️ Ensure your Google Sheet is 'Published to the web' and the URL ends with '&output=csv'."
    )

df = pd.read_csv(StringIO(resp.text))

print(f"ℹ️ Loaded {len(df)} rows from sheet.")
print("ℹ️ Columns from sheet:", list(df.columns))

# Flexible header resolution
col_calendar = first_col(df, ["Calendar", "Calendar Name", "Feed"])
col_title    = first_col(df, ["Title", "Event", "Name"])
col_start    = first_col(df, ["Start"])
col_start_d  = first_col(df, ["Start Date"])
col_start_t  = first_col(df, ["Start Time"])
col_end      = first_col(df, ["End"])
col_end_d    = first_col(df, ["End Date"])
col_end_t    = first_col(df, ["End Time"])
col_loc      = first_col(df, ["Location", "Place", "Room"])
col_desc     = first_col(df, ["Description", "Details", "Notes"])
col_url      = first_col(df, ["URL", "Link"])
col_uid      = first_col(df, ["UID", "Uid"])
col_allday   = first_col(df, ["All Day", "All-day", "AllDay"])
col_transp   = first_col(df, ["Transparent"])

missing_keys = []
if not col_calendar: missing_keys.append("Calendar")
if not col_title:    missing_keys.append("Title/Event/Name")
if not (col_start or col_start_d):
    missing_keys.append("Start OR (Start Date + optional Start Time)")
if missing_keys:
    raise SystemExit("❌ Required columns missing: " + ", ".join(missing_keys))

for c in [col_calendar, col_title, col_loc, col_desc, col_url, col_uid]:
    if c:
        df[c] = df[c].apply(clean_str)

os.makedirs(ICS_DIR, exist_ok=True)
manifest = []
counts = {}
cal_order = list(dict.fromkeys(df[col_calendar].tolist()))
total_events = 0
per_calendar_debug = []

for cal_name in cal_order:
    if not cal_name:
        continue
    subset = df[df[col_calendar] == cal_name]
    if subset.empty:
        continue

    from ics.grammar.parse import ContentLine

    cal = Calendar()
    cal.extra.append(ContentLine(name="X-WR-CALNAME", params={}, value=cal_name))

    created = 0

    for _, r in subset.iterrows():
        title = clean_str(r.get(col_title))
        if not title:
            continue

        start_dt = None
        end_dt = None
        if col_start:
            start_dt = parse_dt(r.get(col_start))
        elif col_start_d:
            start_dt = combine_date_time(r.get(col_start_d), r.get(col_start_t))

        if col_end:
            end_dt = parse_dt(r.get(col_end))
        elif col_end_d:
            end_dt = combine_date_time(r.get(col_end_d), r.get(col_end_t))

        if start_dt is None and end_dt is None:
            continue

        allday_flag = False
        if col_allday:
            v = str(r.get(col_allday)).strip().lower()
            allday_flag = v in ("true", "1", "yes", "y")

        if not allday_flag:
            if (start_dt is not None and is_midnight(start_dt)) and (end_dt is None or is_midnight(end_dt)):
                allday_flag = True

        ev = Event()
        ev.name = title

        if allday_flag:
            if start_dt is None and end_dt is not None:
                start_dt = end_dt
            ev.begin = start_dt.date()
            ev.make_all_day()
            if end_dt is None or end_dt.date() <= start_dt.date():
                ev.end = (start_dt + pd.Timedelta(days=1)).date()
            else:
                ev.end = end_dt.date()
        else:
            if start_dt is None and end_dt is not None:
                start_dt = end_dt - pd.Timedelta(hours=DEFAULT_TIMED_DURATION_HOURS)
            if end_dt is None or end_dt <= start_dt:
                end_dt = start_dt + pd.Timedelta(hours=DEFAULT_TIMED_DURATION_HOURS)
            ev.begin = start_dt
            ev.end = end_dt

        if col_loc:  ev.location    = clean_str(r.get(col_loc)) or None
        if col_desc: ev.description = clean_str(r.get(col_desc)) or None
        if col_url:  ev.url         = clean_str(r.get(col_url)) or None

        uid = clean_str(r.get(col_uid)) if col_uid else ""
        ev.uid = uid or make_uid(title, start_dt, end_dt, ev.location or "")

        # Transparent flag (TRUE = free, FALSE/blank = busy)
        if col_transp:
            v = str(r.get(col_transp)).strip().lower()
            ev.transparent = v in ("true", "1", "yes", "y")

        cal.events.add(ev)
        created += 1
        total_events += 1

    slug = slugify(cal_name)
    rel_ics = f"/calendars/{slug}.ics"
    if rel_ics.endswith("}"):
        rel_ics = rel_ics[:-1]
    ics_path = os.path.join(OUT_DIR, rel_ics.lstrip("/"))

    per_calendar_debug.append((cal_name, created))
    with open(ics_path, "w", encoding="utf-8") as f:
        f.writelines(cal.serialize_iter())

    manifest.append({"name": cal_name, "slug": slug, "ics": rel_ics})
    counts[cal_name] = created
    print(f"✅ Wrote {ics_path} ({created} events)")

print("—— Summary ——")
print(f"Calendars found: {len(per_calendar_debug)}")
for name, cnt in per_calendar_debug:
    print(f"  • {name}: {cnt} events")
print(f"Total events across all calendars: {total_events}")

if total_events == 0:
    print("❌ No events were generated. Please verify your sheet.")
    raise SystemExit(1)

with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)

# ------------------ Landing page ------------
index_html = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Subscribe to Calendars</title>
<style>
:root{
  /* Palette (your choices) */
  --bg:#f6f4e8;
  --card:#881228;
  --text:#ffffff;
  --muted:#f5f5f7;

  --apple-bg:#f5f5f7;   --apple-text:#000000;
  --google-bg:#ea4335;  --google-text:#ffffff;
  --outlook-bg:#0078d4; --outlook-text:#ffffff;

  --copy-bg:#ffffff;    --copy-text:#000000;
  --dd-text:#000000;    --chev:#000000;

  /* Sizes & spacing */
  --radius:16px;
  --gap:12px;
  --control-h:48px;
  --copy-w:96px;     /* fixed width for Copy link */
}

/* Page */
*{box-sizing:border-box}
html,body{height:100%}
body{
  margin:0;
  font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,sans-serif;
  background:var(--bg);
  color:var(--text);
  display:flex;
  justify-content:center;
  align-items:flex-start;
  padding:40px 16px;
}

/* Card */
.card{
  width:min(1200px,100%);
  background:var(--card);
  border-radius:var(--radius);
  box-shadow:0 18px 60px rgba(0,0,0,.25);
  padding:28px;
}

/* Title + sub */
h1{margin:0 0 6px;font-size:clamp(22px,3.4vw,36px);letter-spacing:.3px}
.lead{margin:0 0 18px;color:var(--muted);font-size:clamp(14px,2.3vw,18px)}
.lead em{font-style:italic}

/* Layout rows */
.row{display:flex;flex-wrap:wrap;gap:var(--gap);align-items:center}

/* Top row: dropdown + copy aligned on one line */
.topline{
  display:flex;align-items:center;gap:var(--gap);
  margin-bottom:14px;
}

/* Dropdown — wide and readable */
select.calsel{
  color:var(--dd-text);
  background:#fff;
  height:var(--control-h);
  border-radius:12px;
  border:2px solid #0b66e4; /* subtle highlight to match earlier look */
  padding:0 2.4rem 0 14px;  /* space for chevron */
  font-size:16px;
  min-width:600px;          /* << keep it comfortably wide */
  width:clamp(540px, 52vw, 760px); /* responsive width, wide on large screens */

  /* Chevron */
  appearance:none;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24' fill='%23000'><path d='M7 10l5 5 5-5'/></svg>");
  background-repeat:no-repeat;
  background-size:16px;
  background-position:right 0.9rem center; /* not all the way right */
}

/* Copy link button (fixed width = 96px) */
#copyBtn{
  height:var(--control-h);
  width:var(--copy-w);
  background:var(--copy-bg);
  color:var(--copy-text);
  border:none;border-radius:12px;
  font-weight:800;font-size:16px;letter-spacing:.2px;
  cursor:pointer;
}

/* Brand buttons row */
.btns .btn{
  height:46px;
  border:none;border-radius:12px;
  padding:0 18px;
  font-size:16px;font-weight:600;
  cursor:pointer;
}
.btn.apple  {background:var(--apple-bg);  color:var(--apple-text);}
.btn.google {background:var(--google-bg); color:var(--google-text);}
.btn.olpers {background:var(--outlook-bg);color:var(--outlook-text);}
.btn.olwork {background:var(--outlook-bg);color:var(--outlook-text);}
</style>
</head>
<body>
  <div class="card">
    <h1>Subscribe to Calendars</h1>
    <p class="lead">Choose a calendar, then subscribe. Use <em>Copy link</em> to grab the raw ICS URL.</p>

    <!-- Top line: dropdown + Copy link -->
    <div class="topline">
      <label class="sr-only" for="calSel">Calendar</label>
      <select id="calSel" class="calsel" aria-label="Choose calendar"></select>
      <button id="copyBtn" type="button">Copy</button>
    </div>

    <!-- Brand buttons -->
    <div class="row btns">
      <button id="appleBtn"  class="btn apple"  type="button">Apple Calendar</button>
      <button id="googleBtn" class="btn google" type="button">Google Calendar</button>
      <button id="olLiveBtn" class="btn olpers" type="button">Outlook (personal)</button>
      <button id="olWorkBtn" class="btn olwork" type="button">Outlook (work/school)</button>
    </div>
  </div>

<script>
(async function(){
  const sel      = document.getElementById('calSel');
  const copyBtn  = document.getElementById('copyBtn');
  const appleBtn = document.getElementById('appleBtn');
  const googleBtn= document.getElementById('googleBtn');
  const olLiveBtn= document.getElementById('olLiveBtn');
  const olWorkBtn= document.getElementById('olWorkBtn');

  function absUrl(rel){ return new URL(rel, location.href).href; }
  function currentIcsUrl(){
    const slug = sel.value;
    return absUrl('calendars/' + slug + '.ics');
  }

  async function loadManifest(){
    const res = await fetch(absUrl('calendars.json'), {cache:'no-store'});
    if(!res.ok) throw new Error('Failed to load calendars.json');
    return res.json();
  }

  function setButtons(){
    const ics  = currentIcsUrl();
    const name = encodeURIComponent(sel.options[sel.selectedIndex].text);
    const enc  = encodeURIComponent(ics);

    // Apple calendar (webcal)
    appleBtn.onclick  = () => location.href = 'webcal://' + ics.replace(/^https?:\/\//,'');
    // Google "Add by URL" page (pre-populates the URL field)
    googleBtn.onclick = () => window.open('https://calendar.google.com/calendar/u/0/r/settings/addbyurl?cid=' + enc, '_blank');
    // Outlook (personal)
    olLiveBtn.onclick = () => window.open('https://outlook.live.com/calendar/0/addfromweb?url=' + enc + '&name=' + name, '_blank');
    // Outlook (work/school)
    olWorkBtn.onclick = () => window.open('https://outlook.office.com/calendar/0/addfromweb?url=' + enc + '&name=' + name, '_blank');
  }

  // Populate dropdown
  try{
    const calendars = await loadManifest();
    sel.innerHTML = '';
    calendars.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.slug;
      opt.textContent = c.name;
      sel.appendChild(opt);
    });
    sel.addEventListener('change', setButtons);
    setButtons();
  }catch(e){
    sel.innerHTML = '<option value="">(failed to load)</option>';
  }

  // Copy link
  copyBtn.onclick = async () =>{
    try{
      await navigator.clipboard.writeText(currentIcsUrl());
      const old = copyBtn.textContent;
      copyBtn.textContent = 'Copied!';
      setTimeout(()=>copyBtn.textContent = old, 1200);
    }catch{
      alert('Copy failed. Link:\\n' + currentIcsUrl());
    }
  };
})();
</script>
</body>
</html>
"""


with open(INDEX_HTML_PATH, "w", encoding="utf-8") as f:
    f.write(index_html)

print("✅ Wrote", MANIFEST_PATH)
print("✅ Wrote", INDEX_HTML_PATH)
print("🎉 Build complete.")
