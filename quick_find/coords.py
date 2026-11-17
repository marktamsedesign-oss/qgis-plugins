"""Read a latitude/longitude (WGS84) typed or pasted into the search box.

Accepted, for example:
    7.119291, 125.6281            decimal "lat, long" (Google Maps order)
    7.119291 125.6281
    125.6281, 7.119291            swapped automatically when the first value
                                  can only be a longitude (> 90)
    7°07'09.4"N 125°37'41.2"E     degrees/minutes/seconds (Google Earth)
    7 07 09.4 N, 125 37 41.2 E
    N 7.119291 E 125.6281
    https://www.google.com/maps/@7.119291,125.6281,17z   (also ?q= and !3d..!4d)

Plain numbers such as "12 15" are left to the attribute search: a pair needs
a decimal point, a comma, a degree sign or N/S/E/W to count as coordinates.
"""
import re

_NUM = r"[-+]?\d+(?:\.\d+)?"
_URL_PATTERNS = (
    re.compile(r"!3d(" + _NUM + r")!4d(" + _NUM + r")"),
    re.compile(r"@(" + _NUM + r"),(" + _NUM + r")"),
    re.compile(r"[?&](?:q|query|ll)=(" + _NUM + r"),\s*(" + _NUM + r")"),
)
_MARKS = str.maketrans({**{c: " " for c in "°º˚'′’‘\"″”“"}, "−": "-"})


def _value(numbers):
    """Degrees from 1 to 3 numbers (d, d m, d m s)."""
    if not 1 <= len(numbers) <= 3:
        return None
    parts = [float(n) for n in numbers]
    if any(p < 0 for p in parts[1:]) or any(p >= 60 for p in parts[1:]):
        return None
    if len(parts) > 1 and "." in numbers[0]:
        return None  # decimal degrees cannot have minutes after them
    degrees = abs(parts[0]) + sum(p / 60 ** i for i, p in enumerate(parts[1:], 1))
    return -degrees if numbers[0].startswith("-") else degrees


def _valid(lat, lon):
    return lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180


def parse_lat_lon(text):
    """(lat, lon) in decimal degrees, or None if text is not a coordinate."""
    text = text.strip()
    if not text:
        return None
    for pattern in _URL_PATTERNS:
        found = pattern.search(text)
        if found:
            lat, lon = float(found.group(1)), float(found.group(2))
            return (lat, lon) if _valid(lat, lon) else None

    has_degree = any(c in text for c in "°º˚")
    plain = text.upper().translate(_MARKS)
    if re.search(r"[^-+0-9.,;\sNSEW]", plain):
        return None  # other letters: an ID or name, not a coordinate

    # with hemisphere letters: "7 07 09.4 N 125 37 41.2 E" or "N 7.1 E 125.6"
    if re.search(r"[NSEW]", plain):
        tokens = re.findall(_NUM + r"|[NSEW]", plain)
        groups = []  # [hemisphere, numbers]
        if tokens[0].isalpha():  # letter before the numbers
            for token in tokens:
                if token.isalpha():
                    groups.append([token, []])
                else:
                    groups[-1][1].append(token)
        else:  # letter after the numbers
            numbers = []
            for token in tokens:
                if token.isalpha():
                    groups.append([token, numbers])
                    numbers = []
                else:
                    numbers.append(token)
            if numbers:
                return None
        if len(groups) != 2:
            return None
        values = {}
        for hemisphere, numbers in groups:
            value = _value(numbers)
            if value is None:
                return None
            values["lat" if hemisphere in "NS" else "lon"] = (
                -abs(value) if hemisphere in "SW" else abs(value))
        if set(values) != {"lat", "lon"}:
            return None
        return (values["lat"], values["lon"]) if _valid(values["lat"], values["lon"]) else None

    # no letters: split into two halves at a comma/semicolon, else by count
    halves = re.split(r"\s*[,;]\s*", plain.strip())
    if len(halves) == 2:
        first, second = re.findall(_NUM, halves[0]), re.findall(_NUM, halves[1])
        separated = True
    else:
        numbers = re.findall(_NUM, plain)
        if len(numbers) not in (2, 4, 6) or len(halves) != 1:
            return None
        half = len(numbers) // 2
        first, second = numbers[:half], numbers[half:]
        separated = False
    if not (separated or has_degree or any("." in n for n in first + second)):
        return None  # "12 15" is more likely an attribute value
    a, b = _value(first), _value(second)
    if a is None or b is None:
        return None
    lat, lon = (b, a) if abs(a) > 90 >= abs(b) else (a, b)
    return (lat, lon) if _valid(lat, lon) else None


def format_lat_lon(lat, lon):
    return f"{lat:.6f}, {lon:.6f}"


# field names that hold the coordinates or a name, best match first
_LAT_FIELDS = ("latitude", "lat", "lat_dd", "latitude_dd", "lat_wgs84", "y", "y_coord", "ycoord")
_LON_FIELDS = ("longitude", "long", "lon", "lng", "long_dd", "lon_dd", "longitude_dd",
               "lon_wgs84", "x", "x_coord", "xcoord")
_NAME_FIELDS = ("name", "pin_name", "site_name", "sitename", "label", "title")


def _pick(names, wanted, word=None):
    low = [n.strip().lower().replace(" ", "_") for n in names]
    for candidate in wanted:
        if candidate in low:
            return low.index(candidate)
    if word:  # e.g. "Site_Latitude": only when exactly one field has the word
        hits = [i for i, n in enumerate(low) if word in n]
        if len(hits) == 1:
            return hits[0]
    return None


def lat_lon_fields(names):
    """(latitude index, longitude index) among field names; None where absent."""
    return _pick(names, _LAT_FIELDS, "latitude"), _pick(names, _LON_FIELDS, "longitude")


def name_field(names):
    return _pick(names, _NAME_FIELDS)


def street_view_url(lat, lon):
    """Google Maps link that opens Street View at (or nearest to) lat/lon.

    Uses Google's documented Maps URLs, so no API key is needed; only these
    coordinates are sent, and only when the user asks for Street View.
    """
    return f"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={lat:.6f},{lon:.6f}"
