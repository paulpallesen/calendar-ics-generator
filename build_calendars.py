# build_calendars.py
# Google Sheet (CSV_URL secret) -> public/calendars/<slug>.ics + calendars.json + index.html
# Safe update: adds flexible header handling + diagnostics; preserves existing behavior.

import os
import re
import json
from io import StringIO
from hashlib import md5

import requests
import pandas as pd
from ics import Calendar, Event
from ics.grammar.parse import ContentLine  # ✅ ADDED

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
        # date-only
        return d.normalize()  # midnight
    # Parse time; pandas can parse a time string; combine with date
    t = pd.to_datetime(str(time_val), errors="coerce", dayfirst=DAYFIRST)
    if pd.isna(t):
        return d.normalize()
    return pd.Timestamp(
        year=d.year, month=d.month, day=d.day,
        hour=t.hour, minute=t.minute, second=t.second
    )

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
# ✅ resolve transparency column (TRUE => FREE)
col_transp   = first_col(df, ["Transparent", "FreeBusy", "Show As", "ShowAs", "Transparency"])

missing_keys = []
if not col_calendar: missing_keys.append("Calendar")
if not col_title:    missing_keys.append("Title/Event/Name")
if not (col_start or col_start_d):
    missing_keys.append("Start OR (Start Date + optional Start Time)")
if missing_keys:
    raise SystemExit("❌ Required columns missing: " + ", ".join(missing_keys))

# Clean common string columns
for c in [col_calendar, col_title, col_loc, col_desc, col_url, col_uid]:
    if c:
        df[c] = df[c].apply(clean_str)

# Prepare output
os.makedirs(ICS_DIR, exist_ok=True)
manifest = []
counts = {}

# Preserve first-seen calendar order
cal_order = list(dict.fromkeys(df[col_calendar].tolist()))

total_events = 0
per_calendar_debug = []

for cal_name in cal_order:
    if not cal_name:
        continue
    subset = df[df[col_calendar] == cal_name]
    if subset.empty:
        continue

    cal = Calendar()
    # ✅ Use ContentLine for X-WR-CALNAME so ics lib can serialize it
    cal.extra.append(ContentLine(name="X-WR-CALNAME", params={}, value=cal_name))
    created = 0

    for _, r in subset.iterrows():
        title = clean_str(r.get(col_title))
        if not title:
            continue

        # Build start/end timestamps from either combined or split columns
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
            # nothing to place on a calendar
            continue

        # Determine all-day
        allday_flag = False
        if col_allday:
            v = str(r.get(col_allday)).strip().lower()
            allday_flag = v in ("true", "1", "yes", "y")

        # If both times are blank or midnight and not explicitly timed, treat as all-day
        if not allday_flag:
            if (start_dt is not None and is_midnight(start_dt)) and (end_dt is None or is_midnight(end_dt)):
                allday_flag = True

        ev = Event()
        ev.name = title

        if allday_flag:
            # All-day: use date parts and ensure DTEND > DTSTART (non-inclusive)
            if start_dt is None and end_dt is not None:
                # If only End exists, default to single-day ending date
                start_dt = end_dt
            ev.begin = start_dt.date()
            ev.make_all_day()
            if end_dt is None or end_dt.date() <= start_dt.date():
                ev.end = (start_dt + pd.Timedelta(days=1)).date()
            else:
                ev.end = end_dt.date()
        else:
            # Timed: ensure End > Start
            if start_dt is None and end_dt is not None:
                start_dt = end_dt - pd.Timedelta(hours=DEFAULT_TIMED_DURATION_HOURS)
            if end_dt is None or end_dt <= start_dt:
                end_dt = start_dt + pd.Timedelta(hours=DEFAULT_TIMED_DURATION_HOURS)
            ev.begin = start_dt
            ev.end = end_dt

        # Optional fields
        if col_loc:  ev.location    = clean_str(r.get(col_loc)) or None
        if col_desc: ev.description = clean_str(r.get(col_desc)) or None
        if col_url:  ev.url         = clean_str(r.get(col_url)) or None

        # ✅ Map transparency so TRUE => FREE (does not block)
        if col_transp:
            raw = str(r.get(col_transp)).strip().lower()
            if raw in ("true", "1", "yes", "y", "free", "transparent"):
                ev.transparent = True     # FREE
            elif raw in ("false", "0", "no", "n", "busy", "opaque"):
                ev.transparent = False    # BUSY
            # else: leave unset (client default)

        uid = clean_str(r.get(col_uid)) if col_uid else ""
        ev.uid = uid or make_uid(title, start_dt, end_dt, ev.location or "")

        cal.events.add(ev)
        created += 1
        total_events += 1

    # Write ICS if any events for this calendar
        slug = slugify(cal_name)
    rel_ics = f"/calendars/{slug}.ics"
    # fix accidental brace in path if present
    if rel_ics.endswith("}"):
        rel_ics = rel_ics[:-1]
    ics_path = os.path.join(OUT_DIR, rel_ics.lstrip("/"))

    # Keep a small debug record
    per_calendar_debug.append((cal_name, created))

    with open(ics_path, "w", encoding="utf-8") as f:
        f.writelines(cal.serialize_iter())

    manifest.append({"name": cal_name, "slug": slug, "ics": rel_ics})
    counts[cal_name] = created
    print(f"✅ Wrote {ics_path} ({created} events)")

# Diagnostics summary
print("—— Summary ——")
print(f"Calendars found: {len(per_calendar_debug)}")
for name, cnt in per_calendar_debug:
    print(f"  • {name}: {cnt} events")
print(f"Total events across all calendars: {total_events}")

# If zero events, fail with helpful info
if total_events == 0:
    print("❌ No events were generated. Please verify:")
    print("   - Column names present in your sheet:")
    print("     ", list(df.columns))
    print("   - At least one of these start combinations exists per row:")
    print("     • 'Start'  OR  'Start Date' (+ optional 'Start Time')")
    print("   - 'Calendar' and 'Title' columns are filled")
    raise SystemExit(1)

# ------------------ Write manifest ----------
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
  --apple:#333; --google:#1A73E8; --outlook:#0078D4;
  --bg:#0b0f1a; --card:#121826; --text:#e6eaf2; --muted:#9aa4b2; --accent:#2dd4bf;
}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,sans-serif;background:var(--bg);color:var(--text)}
.container{max-width:900px;margin:40px auto;padding:24px}
.card{background:var(--card);border-radius:16px;box-shadow:0 10px 30px rgba(0,0,0,.35);padding:24px}
h1{margin:0 0 8px;font-size:28px}
p.lead{margin:0 0 20px;color:var(--muted)}
.row{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}
select,button{font-size:16px;border-radius:10px;border:1px solid #223;padding:10px 12px;background:#0f1524;color:var(--text)}
select{min-width:260px}
button{cursor:pointer;transition:.15s transform ease,.2s opacity}
button:hover{transform:translateY(-1px)}
.btn{display:inline-flex;align-items:center;gap:8px;padding:10px 14px;border:none}
.apple{background:var(--apple);color:#fff}
.google{background:var(--google);color:#fff}
.outlook{background:var(--outlook);color:#fff}
.copy{background:var(--accent);color:#042;font-weight:600}
.badge{display:inline-block;background:#172036;color:var(--muted);padding:6px 10px;border-radius:999px;font-size:12px;margin-left:8px}
.footer{margin-top:16px;color:var(--muted);font-size:13px}
.hidden{display:none}
code{background:#0f1524;padding:2px 6px;border-radius:6px}
</style>
</head>
<body>
  <div class="container">
    <div class="card">
      <h1>Subscribe to Calendars <span id="count" class="badge"></span></h1>
      <p class="lead">Choose a calendar, then subscribe. Use <em>Copy link</em> to grab the raw ICS URL.</p>

      <div class="row">
        <label for="calSel" class="hidden">Calendar</label>
        <select id="calSel" aria-label="Choose calendar"></select>
        <button id="copyBtn" class="btn copy">Copy link</button>
      </div>

      <div class="row">
        <button id="appleBtn"  class="btn apple">Apple Calendar</button>
        <button id="googleBtn" class="btn google">Google Calendar</button>
        <button id="olLiveBtn" class="btn outlook">Outlook (personal)</button>
        <button id="olWorkBtn" class="btn outlook">Outlook (work/school)</button>
      </div>

      <div class="footer">
        Selected feed: <code id="linkOut"></code>
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
  const linkOut = document.getElementById('linkOut');
  const countEl = document.getElementById('count');

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
    const name = encodeURIComponent(sel.options[sel.selectedIndex].text);
    const enc = encodeURIComponent(ics);
    // Apple (webcal)
    appleBtn.onclick  = () => location.href = 'webcal://' + ics.replace(/^https?:\/\//,'');
    // Google
    googleBtn.onclick = () => window.open('https://calendar.google.com/calendar/u/0/r?cid=' + enc, '_blank');
    // Outlook (personal)
    olLiveBtn.onclick = () => window.open('https://outlook.live.com/calendar/0/addfromweb?url=' + enc + '&name=' + name, '_blank');
    // Outlook (work/school)
    olWorkBtn.onclick = () => window.open('https://outlook.office.com/calendar/0/addfromweb?url=' + enc + '&name=' + name, '_blank');
    linkOut.textContent = ics;
  }

  try{
    const calendars = await loadManifest();
    countEl.textContent = calendars.length + ' available';
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
    linkOut.textContent = 'Failed to load calendars.json';
  }

  copyBtn.onclick = async () => {
    try{
      await navigator.clipboard.writeText(currentIcsUrl());
      copyBtn.textContent = 'Copied!';
      setTimeout(() => copyBtn.textContent = 'Copy link', 1200);
    }catch(e){
      alert('Copy failed. Link:\\n' + currentIcsUrl());
    }
  };
})();
</script>
</body>
</html>
"""

with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)

with open(INDEX_HTML_PATH, "w", encoding="utf-8") as f:
    f.write(index_html)

print("✅ Wrote", MANIFEST_PATH)
print("✅ Wrote", INDEX_HTML_PATH)
print("🎉 Build complete.")
