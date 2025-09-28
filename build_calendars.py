import os
import re
import json
import hashlib
import logging
import pandas as pd
from datetime import timedelta
from zoneinfo import ZoneInfo
from ics import Calendar, Event
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def slugify(s):
    if pd.isna(s):
        return ''
    s = str(s).lower().strip()
    s = re.sub(r'[^a-z0-9\s-]', '', s)
    s = re.sub(r'[\s-]+', '-', s)
    s = re.sub(r'^-+|-+$', '', s)
    return s

def clean_str(s):
    if pd.isna(s):
        return None
    return str(s).strip()

def parse_dt_str(date_str, time_str, tz):
    if pd.isna(date_str) or pd.isna(time_str):
        return None
    try:
        dt_str = f"{date_str.date()} {time_str}"
        dt = pd.to_datetime(dt_str, errors='coerce')
        if pd.isna(dt):
            return None
        if dt.tz is None:
            return dt.tz_localize(tz)
        else:
            return dt.tz_convert(tz)
    except Exception as e:
        logger.warning(f"Failed to parse datetime {date_str} {time_str}: {e}")
        return None

def make_uid(title, start, end, location):
    start_str = start.isoformat() if hasattr(start, 'isoformat') else str(start)
    end_str = end.isoformat() if hasattr(end, 'isoformat') else str(end)
    key = f"{title}|{start_str}|{end_str}|{location}"
    return hashlib.md5(key.encode()).hexdigest() + "@torrens-uni.edu.au"

csv_url = os.getenv('CSV_URL')
if not csv_url:
    logger.error("CSV_URL environment variable not set.")
    exit(1)

tz = ZoneInfo("Australia/Sydney")

logger.info("Loading CSV...")
try:
    df = pd.read_csv(csv_url, na_values=['', 'nan'])
    logger.info(f"CSV loaded with {len(df)} raw rows.")
except Exception as e:
    logger.error(f"Failed to load CSV: {e}")
    exit(1)

# Clean and prepare columns
df = df.dropna(subset=['Calendar', 'Title'])
df['Start Date'] = pd.to_datetime(df['Start Date'], errors='coerce', dayfirst=True)
df['End Date'] = pd.to_datetime(df['End Date'], errors='coerce', dayfirst=True)
df['Start Time'] = df['Start Time'].astype(str).str.strip()
df['End Time'] = df['End Time'].astype(str).str.strip()
df['has_time_start'] = (df['Start Time'] != '') & (df['Start Time'] != 'nan') & df['Start Date'].notna()
df['has_time_end'] = (df['End Time'] != '') & (df['End Time'] != 'nan') & df['End Date'].notna()

# Filter valid rows
initial_count = len(df)
df = df[df['Title'].notna() & df['Start Date'].notna()]
valid_count = len(df)
skipped_count = initial_count - valid_count
logger.info(f"Filtered to {valid_count} valid events (skipped {skipped_count} rows with missing Title or Start Date).")

Path('public/calendars').mkdir(parents=True, exist_ok=True)

calendars_list = []
grouped = df.groupby('Calendar')
total_processed = 0
for name, group in grouped:
    if pd.isna(name):
        logger.warning("Skipping group with NaN calendar name.")
        continue
    slug = slugify(name)
    if not slug:
        logger.warning(f"Skipping calendar '{name}' due to invalid slug.")
        continue
    cal = Calendar()
    cal.extra.append(('X-WR-CALNAME', str(name)))  # Set calendar name for Google/others
    cal.extra.append(('X-WR-TIMEZONE', 'Australia/Sydney'))  # Explicit timezone for Outlook
    count = 0
    skipped_in_group = 0
    for idx, row in group.iterrows():
        try:
            event = Event()
            event.name = clean_str(row['Title'])
            if not event.name:
                skipped_in_group += 1
                continue

            # Determine if timed or all-day
            is_timed = row['has_time_start'] or row['has_time_end']
            if is_timed:
                # Timed event
                start_time_str = row['Start Time'] if row['has_time_start'] else '00:00:00'
                event.begin = parse_dt_str(row['Start Date'], start_time_str, tz)
                if pd.isna(event.begin):
                    skipped_in_group += 1
                    continue
                if pd.notna(row['End Date']):
                    end_time_str = row['End Time'] if row['has_time_end'] else '00:00:00'
                    event.end = parse_dt_str(row['End Date'], end_time
