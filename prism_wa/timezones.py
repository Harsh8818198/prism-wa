"""Which time zone a phone number lives in, and the next "10:00 their time".

WhatsApp numbers carry a country calling code, and for most countries that is enough to name the time zone.
A few countries span several zones (the USA, Canada, Russia, Brazil, Australia, Indonesia, Mexico): for those
this picks the zone where most of the people are, which is right for most numbers and an hour or two off for
the rest. That is good enough for "send at 10:00 their time" — and never worse than sending everyone at the
sender's own clock — and the owner can see it is a country-level guess, not a location.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

DEFAULT_ZONE = "Asia/Kolkata"

# calling code -> zone (longest matching prefix wins)
ZONES = {
    "1": "America/New_York", "7": "Europe/Moscow", "20": "Africa/Cairo", "27": "Africa/Johannesburg", "30": "Europe/Athens",
    "31": "Europe/Amsterdam", "32": "Europe/Brussels", "33": "Europe/Paris", "34": "Europe/Madrid", "39": "Europe/Rome",
    "40": "Europe/Bucharest", "41": "Europe/Zurich", "43": "Europe/Vienna", "44": "Europe/London", "45": "Europe/Copenhagen",
    "46": "Europe/Stockholm", "47": "Europe/Oslo", "48": "Europe/Warsaw", "49": "Europe/Berlin", "51": "America/Lima",
    "52": "America/Mexico_City", "54": "America/Argentina/Buenos_Aires", "55": "America/Sao_Paulo", "56": "America/Santiago",
    "57": "America/Bogota", "58": "America/Caracas", "60": "Asia/Kuala_Lumpur", "61": "Australia/Sydney", "62": "Asia/Jakarta",
    "63": "Asia/Manila", "64": "Pacific/Auckland", "65": "Asia/Singapore", "66": "Asia/Bangkok", "81": "Asia/Tokyo",
    "82": "Asia/Seoul", "84": "Asia/Ho_Chi_Minh", "86": "Asia/Shanghai", "90": "Europe/Istanbul", "91": "Asia/Kolkata",
    "92": "Asia/Karachi", "93": "Asia/Kabul", "94": "Asia/Colombo", "95": "Asia/Yangon", "98": "Asia/Tehran",
    "212": "Africa/Casablanca", "213": "Africa/Algiers", "216": "Africa/Tunis", "234": "Africa/Lagos", "233": "Africa/Accra",
    "251": "Africa/Addis_Ababa", "254": "Africa/Nairobi", "255": "Africa/Dar_es_Salaam", "256": "Africa/Kampala",
    "351": "Europe/Lisbon", "353": "Europe/Dublin", "358": "Europe/Helsinki", "880": "Asia/Dhaka", "961": "Asia/Beirut",
    "962": "Asia/Amman", "964": "Asia/Baghdad", "965": "Asia/Kuwait", "966": "Asia/Riyadh", "968": "Asia/Muscat",
    "971": "Asia/Dubai", "972": "Asia/Jerusalem", "973": "Asia/Bahrain", "974": "Asia/Qatar", "977": "Asia/Kathmandu",
}


def zone_name_for_phone(digits: Optional[str], default: str = DEFAULT_ZONE) -> str:
    digits = "".join(ch for ch in (digits or "") if ch.isdigit())
    for size in (3, 2, 1):
        if len(digits) >= size and digits[:size] in ZONES:
            return ZONES[digits[:size]]
    return default


def zone_for_phone(digits: Optional[str], default: str = DEFAULT_ZONE) -> ZoneInfo:
    return ZoneInfo(zone_name_for_phone(digits, default))


def next_at_local(hhmm: str, zone: ZoneInfo, after: datetime) -> datetime:
    """The first moment after `after` when the clock in `zone` reads `hhmm`, as an aware UTC datetime."""
    hour, minute = int(hhmm[:2]), int(hhmm[3:5])
    local = after.astimezone(zone)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate = (local + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate.astimezone(timezone.utc)
