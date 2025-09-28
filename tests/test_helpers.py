import pytest
from datetime import date
import pandas as pd
from zoneinfo import ZoneInfo
from build_calendars import slugify, clean_str, parse_dt_str, make_uid

@pytest.fixture
def tz():
    return ZoneInfo("Australia/Sydney")

def test_slugify():
    assert slugify("Academic Calendar - Staff") == "academic-calendar-staff"
    assert slugify("Timetable Team") == "timetable-team"
    assert slugify(None) == ""
    assert slugify("  Invalid!@#Chars  ") == "invalid-chars"

def test_clean_str():
    assert clean_str("Test") == "Test"
    assert clean_str(None) is None
    assert clean_str("  ") == ""
    assert clean_str(pd.NA) is None

def test_parse_dt_str(tz):
    from datetime import datetime
    # Timed
    dt = datetime(2025, 9, 28, 9, 0)
    parsed = parse_dt_str(pd.Timestamp('2025-09-28'), "09:00:00", tz)
    assert parsed == pd.Timestamp('2025-09-28 09:00:00+10:00', tz=tz)
    # All-day-ish but with default time
    parsed_default = parse_dt_str(pd.Timestamp('2025-09-28'), "00:00:00", tz)
    assert parsed_default == pd.Timestamp('2025-09-28 00:00:00+10:00', tz=tz)
    # Invalid
    assert parse_dt_str(pd.Timestamp('invalid'), "09:00", tz) is None

def test_make_uid():
    uid = make_uid("Test Event", pd.Timestamp('2025-09-28 09:00'), pd.Timestamp('2025-09-28 10:00'), "Room 101")
    assert uid.endswith("@torrens-uni")
    assert len(uid) == 36  # MD5 hex + domain
