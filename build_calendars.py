# build_calendars.py
# Google Sheet (CSV_URL secret) -> public/calendars/<slug>.ics + calendars.json + index.html

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

# Clean strings
for c in [col_calendar, col_title, col_loc, col_desc, col_url, col_uid]:
    if c:
        df[c] = df[c].apply(clean_str)

# Prepare output
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

    # Ensure X-WR-CALNAME is a proper ContentLine (avoids tuple/list clone errors)
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

# ------------------ Landing page (palette + exact widths) ------------
index_html = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Subscribe to Calendars</title>
<style>
:root{
  /* Palette (as you provided) */
  --bg:#f6f4e8;
  --card:#881228;
  --title:#ffffff;
  --sub:#f5f5f5;

  --apple-bg:#f5f5f7;  --apple-text:#000000;
  --google-bg:#ea4335; --google-text:#ffffff;
  --outlook-bg:#0078d4; --outlook-text:#ffffff;

  --copy-bg:#ffffff; --copy-text:#000000;

  --dropdown-bg:#ffffff; --dropdown-text:#000000; --chevron:#000000;

  --border:#1f3b7a;
  --gap:12px; /* keep this the same gap used between buttons */
}

/* Base */
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--title);font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,sans-serif}
.container{max-width:1200px;margin:40px auto;padding:0 20px}

/* Compact card */
.card{
  background:var(--card);
  border-radius:22px;
  padding:24px;
  box-shadow:0 18px 60px rgba(0,0,0,.18);
}

/* Compact headings */
h1{margin:0 0 8px;font-size:44px;line-height:1.05;font-weight:800;color:var(--title)}
p.lead{margin:0 0 16px;font-size:18px;color:var(--sub)}

/* 2-column grid: left = dropdown + left buttons; right = copy link + right buttons */
.grid{
  display:grid;
  grid-template-columns:1fr 1fr;
  gap:var(--gap);
  align-items:center;
}

/* Row 1 (dropdown + copy) */
.selWrap{grid-column:1}
#calSel{
  display:block;
  width:auto; /* JS sets exact px width = Apple + gap + Google */
  font-size:16px;
  padding:12px 46px 12px 14px;     /* room for chevron */
  border-radius:14px;
  background:var(--dropdown-bg);
  color:var(--dropdown-text);
  border:2px solid var(--border);
  outline:none;
  appearance:none;
  min-height:48px;
  /* Chevron */
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' width='20' height='20' fill='%23000000'><path d='M7 10l5 5 5-5'/></svg>");
  background-repeat:no-repeat;
  background-position:right 18px center; /* slightly inset from far right */
  background-size:20px;
}

#copyBtn{
  grid-column:2;
  height:100%;
  min-height:48px;                /* same height as dropdown */
  font-size:16px;
  border-radius:14px;
  padding:12px 16px;
  border:0;
  background:var(--copy-bg);
  color:var(--copy-text);
  font-weight:800;
  cursor:pointer;
  box-shadow:0 2px 0 rgba(0,0,0,.22);
}

/* Row 2 (buttons) */
.btn-group{display:flex;gap:var(--gap);align-items:center}
.left{grid-column:1}
.right{grid-column:2}

/* Compact buttons */
.btn{
  display:inline-flex;align-items:center;justify-content:center;
  padding:12px 18px;border-radius:14px;border:none;cursor:pointer;
  font-size:18px;font-weight:700;box-shadow:0 2px 0 rgba(0,0,0,.18);
}
.apple{background:var(--apple-bg);color:var(--apple-text)}
.google{background:var(--google-bg);color:var(--google-text)}
.outlook{background:var(--outlook-bg);color:var(--outlook-text)}

/* Responsive */
@media (max-width: 900px){
  .grid{grid-template-columns:1fr;gap:var(--gap)}
  .selWrap{grid-column:1}
  #calSel{width:100%}
  #copyBtn{grid-column:1}
  .left,.right{grid-column:1}
}
</style>
</head>
<body>
  <div class="container">
    <div class="card">
      <h1>Subscribe to Calendars</h1>
      <p class="lead">Choose a calendar, then subscribe. Use <em>Copy link</em> to grab the raw ICS URL.</p>

      <div class="grid">
        <!-- Row 1 -->
        <div class="selWrap">
          <select id="calSel" aria-label="Choose calendar"></select>
        </div>
        <button id="copyBtn">Copy link</button>

        <!-- Row 2 -->
        <div id="leftGroup" class="btn-group left">
          <button id="appleBtn" class="btn apple">Apple Calendar</button>
          <button id="googleBtn" class="btn google">Google Calendar</button>
        </div>
        <div id="rightGroup" class="btn-group right">
          <button id="olLiveBtn" class="btn outlook">Outlook (personal)</button>
          <button id="olWorkBtn" class="btn outlook">Outlook (work/school)</button>
        </div>
      </div>
    </div>
  </div>

<script>
(async function(){
  const sel = document.getElementById('calSel');
  const copyBtn = document.getElementById('copyBtn');
  const appleBtn = document.getElementById('appleBtn');
  const googleBtn = document.getElementById('googleBtn');
  const olLiveBtn = document.getElementById('olLiveBtn');
  const olWorkBtn = document.getElementById('olWorkBtn');
  const leftGroup = document.getElementById('leftGroup');

  async function loadManifest(){
    const url = new URL('calendars.json', location.href).href;
    const res = await fetch(url, {cache:'no-store'});
    if(!res.ok) throw new Error('Failed to load calendars.json');
    return res.json();
  }

  function absUrl(rel){ return new URL(rel, location.href).href; }
  function currentIcsUrl(){
    const slug = sel.value;
    return absUrl('calendars/' + slug + '.ics');
  }

  function setButtons(){
    const ics = currentIcsUrl();
    const enc = encodeURIComponent(ics);
    const name = encodeURIComponent(sel.options[sel.selectedIndex].text);

    // Apple (webcal)
    appleBtn.onclick = () => location.href = 'webcal://' + ics.replace(/^https?:\/\//,'');

    // Google: open "Add by URL"
    googleBtn.onclick = () => window.open(
      'https://calendar.google.com/calendar/u/0/r/settings/addbyurl?cid=' + enc,
      '_blank'
    );

    // Outlook personal
    olLiveBtn.onclick = () => window.open(
      'https://outlook.live.com/calendar/0/addfromweb?url=' + enc + '&name=' + name,
      '_blank'
    );

    // Outlook work/school
    olWorkBtn.onclick = () => window.open(
      'https://outlook.office.com/calendar/0/addfromweb?url=' + enc + '&name=' + name,
      '_blank'
    );
  }

  // Make dropdown width = Apple + gap + Google (exact)
  function syncWidths(){
    if (window.matchMedia('(max-width: 900px)').matches){
      sel.style.width = '100%';
      return;
    }
    requestAnimationFrame(() => {
      const leftWidth = document.getElementById('leftGroup').getBoundingClientRect().width;
      if (leftWidth > 0){
        sel.style.width = Math.round(leftWidth) + 'px';
      }
    });
  }

  try{
    const calendars = await loadManifest();
    sel.innerHTML = '';
    calendars.forEach((c) => {
      const opt = document.createElement('option');
      opt.value = c.slug;
      opt.textContent = c.name;
      sel.appendChild(opt);
    });
    sel.addEventListener('change', setButtons);
    setButtons();
  }catch(e){
    sel.innerHTML = '<option>Failed to load calendars</option>';
  }

  // Keep widths in sync with rendering
  window.addEventListener('load', syncWidths);
  window.addEventListener('resize', syncWidths);
  setTimeout(syncWidths, 0);

  copyBtn.addEventListener('click', async () => {
    try{
      await navigator.clipboard.writeText(currentIcsUrl());
      const old = copyBtn.textContent;
      copyBtn.textContent = 'Copied!';
      setTimeout(() => copyBtn.textContent = old, 1000);
    }catch(e){
      alert('Copy failed. Link:\\n' + currentIcsUrl());
    }
  });
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
