# build_calendars.py
# Google Sheet (CSV_URL secret) -> public/calendars/<slug>.ics + calendars.json + index.html

import os
import re
import json
from hashlib import md5

import pandas as pd
from ics import Calendar, Event

CSV_URL = os.getenv("CSV_URL")
if not CSV_URL:
    raise ValueError("❌ CSV_URL environment variable is not set (add a repo secret named CSV_URL).")

# ---- Helpers --------------------------------------------------------------

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
    if pd.isna(v):
        return None
    dt = pd.to_datetime(v, errors="coerce")
    if pd.isna(dt):
        return None
    return dt

def make_uid(title, start, end, extra=""):
    s = f"{title}|{start}|{end}|{extra}"
    return md5(s.encode("utf-8")).hexdigest() + "@dynamic-cal"

# ---- Read Google Sheet ----------------------------------------------------

try:
    df = pd.read_csv(CSV_URL)
except Exception as e:
    raise RuntimeError(f"❌ Failed to read CSV from {CSV_URL}: {e}")

# Expected headers (keep your existing naming); tolerate common variants
header_map = {
    "Calendar": "Calendar",
    "Title": "Title",
    "Start": "Start",
    "Start Date": "Start",
    "End": "End",
    "End Date": "End",
    "Location": "Location",
    "Description": "Description",
    "URL": "URL",
    "Uid": "UID",
    "UID": "UID",
}

# Normalize columns
norm_cols = {}
for c in df.columns:
    key = c.strip()
    norm = header_map.get(key, key)
    norm_cols[c] = norm
df = df.rename(columns=norm_cols)

required = ["Calendar", "Title", "Start"]
missing = [c for c in required if c not in df.columns]
if missing:
    raise ValueError(f"❌ Missing required column(s): {', '.join(missing)}")

# Clean blanks and coerce datetimes
for col in ["Calendar", "Title", "Location", "Description", "URL", "UID"]:
    if col in df.columns:
        df[col] = df[col].apply(clean_str)

for col in ["Start", "End"]:
    if col in df.columns:
        df[col] = pd.to_datetime(df[col], errors="coerce")

# Drop empty rows
df = df.dropna(subset=["Calendar", "Title", "Start"])

# ---- Build output dirs ----------------------------------------------------

os.makedirs("public/calendars", exist_ok=True)

# ---- Build ICS per calendar ----------------------------------------------

manifest = []   # [{name, slug, ics}]
counts = {}     # calendar name -> event count

# Preserve first-seen calendar order
cal_names_in_order = list(dict.fromkeys(df["Calendar"].tolist()))

for cal_name in cal_names_in_order:
    if not cal_name:
        continue
    g = df[df["Calendar"] == cal_name]
    if g.empty:
        continue

    cal = Calendar()
    total = 0

    for _, r in g.iterrows():
        title = clean_str(r.get("Title"))
        if not title:
            continue

        start = r.get("Start")
        end = r.get("End") if "End" in r else None
        start_dt = parse_dt(start)
        end_dt = parse_dt(end) if end is not None else None
        if start_dt is None and end_dt is None:
            continue

        ev = Event()
        ev.name = title

        # All-day detection: if Start has 00:00:00 and End is missing or also 00:00:00
        # we treat it as all-day; otherwise use datetime.
        if start_dt is not None:
            if (start_dt.hour, start_dt.minute, start_dt.second) == (0, 0, 0) and (end_dt is None or (end_dt.hour, end_dt.minute, end_dt.second) == (0, 0, 0)):
                ev.begin = start_dt.date()
                ev.make_all_day()
                # For all-day with explicit End date, ics will handle DTEND; otherwise single-day
                if end_dt is not None and end_dt.date() != start_dt.date():
                    ev.end = end_dt.date()
            else:
                ev.begin = start_dt
                if end_dt is not None:
                    ev.end = end_dt
        elif end_dt is not None:
            ev.begin = end_dt

        loc = clean_str(r.get("Location"))
        desc = clean_str(r.get("Description"))
        url = clean_str(r.get("URL"))
        uid = clean_str(r.get("UID")) or make_uid(title, start_dt, end_dt, loc)

        if loc: ev.location = loc
        if desc: ev.description = desc
        if url: ev.url = url
        ev.uid = uid

        cal.events.add(ev)
        total += 1

    slug = slugify(cal_name)
    ics_rel = f"/calendars/{slug}.ics"
    ics_path = "public" + ics_rel

    with open(ics_path, "w", encoding="utf-8") as f:
        f.writelines(cal.serialize_iter())

    manifest.append({"name": cal_name, "slug": slug, "ics": ics_rel})
    counts[cal_name] = total
    print(f"✅ Wrote {ics_path} ({total} events)")

# ---- Write manifest -------------------------------------------------------

with open("public/calendars.json", "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)

# ---- Write landing page (dropdown + buttons) -----------------------------

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
      <p class="lead">Choose a calendar, then subscribe with one click. Use <em>Copy link</em> to grab the raw ICS URL.</p>

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
    // Apple uses webcal:// scheme
    appleBtn.onclick  = () => location.href = 'webcal://' + ics.replace(/^https?:\/\//,'');
    // Google Calendar
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

with open("public/index.html", "w", encoding="utf-8") as f:
    f.write(index_html)

print("✅ Wrote public/calendars.json")
print("✅ Wrote public/index.html")
print("🎉 Build complete.")
