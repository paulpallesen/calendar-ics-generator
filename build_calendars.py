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
# Copy link button – #000000 (bg) / #ffffff (text)
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

# Clean common strings
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

from ics.grammar.parse import ContentLine  # (once, top-level)

for cal_name in cal_order:
    if not cal_name:
        continue
    subset = df[df[col_calendar] == cal_name]
    if subset.empty:
        continue

    cal = Calendar()
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

        uid = clean_str(r.get(col_uid)) if col_uid else ""
        ev.uid = uid or make_uid(title, start_dt, end_dt, ev.location or "")

        # Transparent flag (TRUE = free, FALSE/blank = busy)
        if col_transp:
            v = str(r.get(col_transp)).strip().lower()
            ev.transparent = v in ("true", "1", "yes", "y")

        cal.events.add(ev)
        created += 1
        total_events += 1

    # Write ICS if any events for this calendar
    slug = slugify(cal_name)
    rel_ics = f"/calendars/{slug}.ics"
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
    print("❌ No events were generated. Please verify your sheet.")
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
  /* Core palette */
  --bg: #f6f4e8;         /* page background */
  --card: #881228;       /* card background */
  --text: #ffffff;       /* main title text */
  --muted: #f5f5f5;      /* subheadline */

  /* Brands */
  --apple-bg:   #979797; --apple-text:   #ffffff;
  --google-bg:  #ea4335; --google-text:  #ffffff;
  --outlook-bg: #0078d4; --outlook-text: #ffffff;

  /* Controls */
  --copy-bg:#000000; --copy-text:#ffffff;          /* per your request */
  --dropdown-text:#000000; --chev:#000000;

  /* Layout + sizing */
  --radius: 18px;
  --gap: 14px;             /* gap between items in rows */
  --control-h: 62px;       /* dropdown + copy height (desktop) */
  --shadow: 0 24px 48px rgba(0,0,0,.15);
}

*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,"Helvetica Neue",Arial,sans-serif;color:var(--text)}

.container{max-width:1080px;margin:44px auto;padding:0 20px}
.card{background:var(--card);border-radius:28px;box-shadow:var(--shadow);padding:28px 28px 32px}

h1{margin:0 0 10px;font-size:40px;letter-spacing:.2px}
p.lead{margin:0 0 18px;color:var(--muted);font-size:18px;line-height:1.45}

.row{display:flex;align-items:center;gap:var(--gap);flex-wrap:wrap}

/* --- Select (dropdown) --- */
.select-wrap{
  position:relative;
  height:var(--control-h);
  display:inline-flex;
  align-items:center;
  border-radius:14px;
  background:#fff;
  box-shadow:0 1px 0 rgba(0,0,0,.12), 0 6px 18px rgba(0,0,0,.18);
  padding:0 24px;          /* even padding; select will add right padding for chevron */
  overflow:hidden;         /* ensures select stays within */
}
#calSel{
  appearance:none;
  border:none;
  outline:none;
  font-size:20px;
  color:var(--dropdown-text);
  background:transparent;
  height:100%;
  width:100%;              /* fill wrapper so whole area is clickable */
  display:block;
  padding-right:32px;      /* reserve room for chevron */
  cursor:pointer;
}
.select-wrap:after{
  content:"";
  position:absolute;
  right:10px;              /* slightly inset */
  top:50%;
  width:10px; height:10px; /* smaller chevron */
  transform:translateY(-50%) rotate(45deg);
  border-right:2px solid var(--chev);
  border-bottom:2px solid var(--chev);
  opacity:.9;
  pointer-events:none;     /* chevron never blocks clicks */
}

/* Copy button — same height as dropdown (desktop) */
#copyBtn{
  height:var(--control-h);
  width:96px;
  padding:0 12px;
  border:none;
  border-radius:14px;
  background:var(--copy-bg);
  color:var(--copy-text);
  font-size:20px;
  font-weight:700;
  cursor:pointer;
  box-shadow:0 1px 0 rgba(0,0,0,.12), 0 6px 18px rgba(0,0,0,.18);
}

/* Brand buttons row */
.btn{border:none;border-radius:14px;cursor:pointer;padding:12px 20px;font-size:20px;font-weight:700;transition:.15s transform ease,.2s filter}
.apple{background:var(--apple-bg);color:var(--apple-text)}
.google{background:var(--google-bg);color:var(--google-text)}
.outlook{background:var(--outlook-bg);color:var(--outlook-text)}

/* ---- Hovers ---- */
.btn:hover{
  transform:translateY(-1px);
  filter:brightness(1.05);
}
.apple:hover{ box-shadow:0 2px 0 rgba(0,0,0,.10), 0 10px 22px rgba(0,0,0,.18) }
.google:hover{ background:#d93c2f }
.outlook:hover{ background:#0069bd }
#copyBtn:hover{ box-shadow:0 2px 0 rgba(0,0,0,.10), 0 10px 22px rgba(0,0,0,.18) }
.select-wrap:hover{ box-shadow:0 2px 0 rgba(0,0,0,.12), 0 10px 24px rgba(0,0,0,.22) }

/* ---------- Mobile layout (<=760px) ---------- */
@media (max-width:760px){
  :root{
    --control-h: 56px;            /* slightly shorter controls on mobile */
  }
  .row{gap:12px}
  h1{font-size:34px}
  #calSel{font-size:18px}         /* slightly smaller so long names fit */
  /* Make dropdown and all buttons full-width and same width */
  #rowControls, #rowBrands{flex-direction:column; align-items:stretch}
  .select-wrap{width:100% !important}
  #copyBtn{width:100%}
  #appleBtn, #googleBtn, #olLiveBtn, #olWorkBtn{width:100%}
}
</style>
</head>
<body>
  <div class="container">
    <div class="card">
      <h1>Subscribe to Calendars</h1>
      <p class="lead">Choose a calendar, then subscribe. Use <em>Copy link</em> to grab the raw ICS URL.</p>

      <!-- Controls row (dropdown + copy) -->
      <div class="row" id="rowControls">
        <div class="select-wrap" id="selectWrap">
          <select id="calSel" aria-label="Choose calendar"></select>
        </div>
        <button id="copyBtn">Copy</button>
      </div>

      <!-- Brand buttons row -->
      <div class="row" id="rowBrands" style="margin-top:16px">
        <button id="appleBtn"  class="btn apple">Apple Calendar</button>
        <button id="googleBtn" class="btn google">Google Calendar</button>
        <button id="olLiveBtn" class="btn outlook">Outlook (personal)</button>
        <button id="olWorkBtn" class="btn outlook">Outlook (work/school)</button>
      </div>
    </div>
  </div>

<script>
(async function(){
  // Load manifest
  async function loadManifest(){
    const url = new URL('calendars.json', location.href).href;
    const res = await fetch(url, {cache:'no-store'});
    if(!res.ok) throw new Error('Failed to load calendars.json');
    return res.json();
  }
  function absUrl(rel){ return new URL(rel, location.href).href; }
  function currentIcsUrl(){
    const sel = document.getElementById('calSel');
    return absUrl('calendars/' + sel.value + '.ics');
  }

  // Wire buttons (uses current dropdown selection)
  function setButtons(){
    const sel = document.getElementById('calSel');
    const name = encodeURIComponent(sel.options[sel.selectedIndex].text);
    const enc  = encodeURIComponent(currentIcsUrl());

    // Apple (webcal)
    document.getElementById('appleBtn').onclick = () =>
      location.href = 'webcal://' + currentIcsUrl().replace(/^https?:\/\//,'');

    // Google: open Add-by-URL with cid prefilled
    document.getElementById('googleBtn').onclick = () =>
      window.open('https://calendar.google.com/calendar/u/0/r/settings/addbyurl?cid='+enc, '_blank');

    // Outlook (personal)
    document.getElementById('olLiveBtn').onclick = () =>
      window.open('https://outlook.live.com/calendar/0/addfromweb?url='+enc+'&name='+name, '_blank');

    // Outlook (work/school)
    document.getElementById('olWorkBtn').onclick = () =>
      window.open('https://outlook.office.com/calendar/0/addfromweb?url='+enc+'&name='+name, '_blank');
  }

  // Copy link
  document.getElementById('copyBtn').onclick = async () => {
    try{
      await navigator.clipboard.writeText(currentIcsUrl());
      const b = document.getElementById('copyBtn');
      const old = b.textContent;
      b.textContent = 'Copied!';
      setTimeout(()=>b.textContent=old, 1200);
    }catch(e){
      alert('Copy failed. Link:\\n' + currentIcsUrl());
    }
  };

  // --- Auto width for dropdown: Apple width + gap + Google width (desktop) ---
  function syncDropdownWidth(){
    const apple  = document.getElementById('appleBtn');
    const google = document.getElementById('googleBtn');
    const wrap   = document.getElementById('selectWrap');
    const brandsRow = document.getElementById('rowBrands');

    if (!apple || !google || !wrap || !brandsRow) return;

    // On mobile layout we use full width; skip measuring
    const isMobile = window.matchMedia('(max-width: 760px)').matches;
    if (isMobile){
      wrap.style.width = '100%';
      return;
    }

    const gap = parseFloat(getComputedStyle(brandsRow).gap || '14');
    const width = apple.offsetWidth + gap + google.offsetWidth;
    wrap.style.width = width + 'px';          // dropdown width
  }

  // Make wrapper click also open the native picker (mobile-friendly)
  (function(){
    const wrap = document.getElementById('selectWrap');
    const sel  = document.getElementById('calSel');
    if (wrap && sel) {
      wrap.addEventListener('click', () => {
        if (typeof sel.showPicker === 'function') sel.showPicker();
        else sel.focus();
      });
    }
  })();

  // Populate dropdown from manifest, then size + wire
  try{
    const calendars = await loadManifest();
    const sel = document.getElementById('calSel');
    sel.innerHTML = '';
    calendars.forEach((c) => {
      const opt = document.createElement('option');
      opt.value = c.slug;
      opt.textContent = c.name;
      sel.appendChild(opt);
    });
    sel.addEventListener('change', setButtons);
    setButtons();
    // Defer to ensure buttons have layout
    requestAnimationFrame(syncDropdownWidth);
    window.addEventListener('resize', syncDropdownWidth);
  }catch(e){
    console.error(e);
  }
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
