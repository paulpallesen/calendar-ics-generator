# build_calendars.py
# Google Sheet (CSV_URL secret) -> public/calendars/<slug>.ics + calendars.json + index.html
#
# 🎨 COLOR LEGEND (defined in :root CSS variables below)
# --bg          (#f9f6ec)  : Page background
# --card        (#43001a)  : Card background
# --text        (#ffffff)  : Primary text
# --muted       (#cccccc)  : Muted/subtext
#
# --apple-bg    (#cccccc)  : Apple button background
# --apple-text  (#000000)  : Apple button text
# --google-bg   (#e53935)  : Google button background
# --google-text (#ffffff)  : Google button text
# --outlook-bg  (#0078D4)  : Outlook button background
# --outlook-text(#ffffff)  : Outlook button text
#
# --copy-bg     (#ffffff)  : Copy link / URL background
# --copy-text   (#000000)  : Copy link / URL text

import os
import re
import json
from io import StringIO
from hashlib import md5

import requests
import pandas as pd
from ics import Calendar, Event
from ics.grammar.parse import ContentLine

# ------------------ Config ------------------
DAYFIRST = True
DEFAULT_TIMED_DURATION_HOURS = 1
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
    cols = {c.strip().lower(): c for c in df.columns}
    for n in names:
        key = n.strip().lower()
        if key in cols:
            return cols[key]
    return None

def combine_date_time(date_val, time_val):
    d = parse_dt(date_val)
    if d is None:
        return None
    if time_val is None or (isinstance(time_val, float) and pd.isna(time_val)) or str(time_val).strip() == "":
        return d.normalize()
    t = pd.to_datetime(str(time_val), errors="coerce", dayfirst=DAYFIRST)
    if pd.isna(t):
        return d.normalize()
    return pd.Timestamp(year=d.year, month=d.month, day=d.day, hour=t.hour, minute=t.minute, second=t.second)

# ------------------ Load CSV ----------------
print(f"📥 Downloading CSV from {CSV_URL}")
resp = requests.get(CSV_URL, timeout=30)
resp.raise_for_status()
df = pd.read_csv(StringIO(resp.text))

print(f"ℹ️ Loaded {len(df)} rows from sheet.")
print("ℹ️ Columns from sheet:", list(df.columns))

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

for c in [col_calendar, col_title, col_loc, col_desc, col_url, col_uid]:
    if c: df[c] = df[c].apply(clean_str)

os.makedirs(ICS_DIR, exist_ok=True)
manifest = []
counts = {}
cal_order = list(dict.fromkeys(df[col_calendar].tolist()))
total_events = 0
per_calendar_debug = []

for cal_name in cal_order:
    if not cal_name: continue
    subset = df[df[col_calendar] == cal_name]
    if subset.empty: continue

    cal = Calendar()
    cal.extra.append(ContentLine(name="X-WR-CALNAME", params={}, value=cal_name))
    created = 0

    for _, r in subset.iterrows():
        title = clean_str(r.get(col_title))
        if not title: continue

        start_dt = None
        end_dt = None
        if col_start: start_dt = parse_dt(r.get(col_start))
        elif col_start_d: start_dt = combine_date_time(r.get(col_start_d), r.get(col_start_t))

        if col_end: end_dt = parse_dt(r.get(col_end))
        elif col_end_d: end_dt = combine_date_time(r.get(col_end_d), r.get(col_end_t))

        if start_dt is None and end_dt is None: continue

        allday_flag = False
        if col_allday:
            v = str(r.get(col_allday)).strip().lower()
            allday_flag = v in ("true", "1", "yes", "y")
        if not allday_flag:
            if (start_dt and is_midnight(start_dt)) and (end_dt is None or is_midnight(end_dt)):
                allday_flag = True

        ev = Event()
        ev.name = title
        if allday_flag:
            if start_dt is None and end_dt: start_dt = end_dt
            ev.begin = start_dt.date()
            ev.make_all_day()
            ev.end = (end_dt.date() if end_dt and end_dt.date() > start_dt.date() else (start_dt + pd.Timedelta(days=1)).date())
        else:
            if start_dt is None and end_dt: start_dt = end_dt - pd.Timedelta(hours=DEFAULT_TIMED_DURATION_HOURS)
            if end_dt is None or end_dt <= start_dt: end_dt = start_dt + pd.Timedelta(hours=DEFAULT_TIMED_DURATION_HOURS)
            ev.begin = start_dt
            ev.end = end_dt

        if col_loc:  ev.location    = clean_str(r.get(col_loc)) or None
        if col_desc: ev.description = clean_str(r.get(col_desc)) or None
        if col_url:  ev.url         = clean_str(r.get(col_url)) or None

        uid = clean_str(r.get(col_uid)) if col_uid else ""
        ev.uid = uid or make_uid(title, start_dt, end_dt, ev.location or "")

        if col_transp:
            v = str(r.get(col_transp)).strip().lower()
            ev.transparent = v in ("true", "1", "yes", "y")

        cal.events.add(ev)
        created += 1
        total_events += 1

    slug = slugify(cal_name)
    rel_ics = f"/calendars/{slug}.ics"
    ics_path = os.path.join(OUT_DIR, rel_ics.lstrip("/"))
    per_calendar_debug.append((cal_name, created))
    with open(ics_path, "w", encoding="utf-8") as f:
        f.writelines(cal.serialize_iter())

    manifest.append({"name": cal_name, "slug": slug, "ics": rel_ics})
    counts[cal_name] = created
    print(f"✅ Wrote {ics_path} ({created} events)")

print("—— Summary ——")
for name, cnt in per_calendar_debug:
    print(f"  • {name}: {cnt} events")
print(f"Total events across all calendars: {total_events}")

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
:root {
  --bg:        #f9f6ec;
  --card:      #43001a;
  --text:      #ffffff;
  --muted:     #cccccc;

  --apple-bg:  #cccccc;
  --apple-text:#000000;
  --google-bg: #e53935;
  --google-text:#ffffff;
  --outlook-bg:#0078D4;
  --outlook-text:#ffffff;

  --copy-bg:   #ffffff;
  --copy-text: #000000;
}
body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,sans-serif;background:var(--bg);color:var(--text)}
.container{max-width:900px;margin:40px auto;padding:24px}
.card{background:var(--card);border-radius:16px;padding:24px}
h1{margin:0 0 12px;font-size:28px}
select,button{font-size:16px;border-radius:10px;border:1px solid #223;padding:10px 12px}
select{background:var(--bg);color:#000}
button{cursor:pointer}
.btn{padding:10px 14px;border:none}
.apple{background:var(--apple-bg);color:var(--apple-text)}
.google{background:var(--google-bg);color:var(--google-text)}
.outlook{background:var(--outlook-bg);color:var(--outlook-text)}
.copy{background:var(--copy-bg);color:var(--copy-text);font-weight:600}
</style>
</head>
<body>
  <div class="container">
    <div class="card">
      <h1>Subscribe to a Calendar</h1>
      <div>
        <label for="calSel">Calendar:</label>
        <select id="calSel"></select>
      </div>
      <div>
        <button id="appleBtn" class="btn apple">Apple Calendar</button>
        <button id="googleBtn" class="btn google">Google Calendar</button>
        <button id="olWorkBtn" class="btn outlook">Outlook (Work/Study)</button>
      </div>
      <div>
        Direct feed URL: <code id="linkOut"></code>
      </div>
    </div>
  </div>
<script>
(async function(){
  const sel=document.getElementById('calSel');
  const linkOut=document.getElementById('linkOut');
  const appleBtn=document.getElementById('appleBtn');
  const googleBtn=document.getElementById('googleBtn');
  const olWorkBtn=document.getElementById('olWorkBtn');
  function absUrl(rel){return new URL(rel,location.href).href;}
  function currentIcsUrl(){return absUrl('calendars/'+sel.value+'.ics');}
  function setButtons(){
    const ics=currentIcsUrl();
    const enc=encodeURIComponent(ics);
    const name=encodeURIComponent(sel.options[sel.selectedIndex].text);
    appleBtn.onclick=()=>location.href='webcal://'+ics.replace(/^https?:\/\//,'');
    googleBtn.onclick=()=>window.open('https://calendar.google.com/calendar/u/0/r/settings/addbyurl?cid='+enc,'_blank');
    olWorkBtn.onclick=()=>window.open('https://outlook.office.com/calendar/0/addfromweb?url='+enc+'&name='+name,'_blank');
    linkOut.textContent=ics;
  }
  try{
    const res=await fetch('calendars.json');const calendars=await res.json();
    sel.innerHTML='';
    calendars.forEach(c=>{
      const opt=document.createElement('option');
      opt.value=c.slug;opt.textContent=c.name;sel.appendChild(opt);
    });
    sel.addEventListener('change',setButtons);setButtons();
  }catch(e){linkOut.textContent='Failed to load calendars.json';}
})();
</script>
</body>
</html>
"""

with open(INDEX_HTML_PATH, "w", encoding="utf-8") as f: f.write(index_html)
print("✅ Wrote", INDEX_HTML_PATH)
