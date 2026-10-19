"""Geocoding: bundled Census place gazetteer first, cached Nominatim fallback second. No paid geocoder.

The lookup key strips spaces and punctuation, which is what lets wrapped PDF destinations such as
"MURFREESBO RO" (split mid-word) or "NEW HOLLAND" (split at a word) resolve to the same place.
"""
from __future__ import annotations

import csv
import difflib
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

from .http import HttpError, PoliteSession

log = logging.getLogger("award_to_lane.geo")

ABBREV = {"ST": "SAINT", "FT": "FORT", "MT": "MOUNT", "PT": "POINT", "N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST",
          "NO": "NORTH", "SO": "SOUTH"}


STATE_NAMES = {"AL": "ALABAMA", "AK": "ALASKA", "AZ": "ARIZONA", "AR": "ARKANSAS", "CA": "CALIFORNIA", "CO": "COLORADO",
               "CT": "CONNECTICUT", "DE": "DELAWARE", "FL": "FLORIDA", "GA": "GEORGIA", "HI": "HAWAII", "ID": "IDAHO",
               "IL": "ILLINOIS", "IN": "INDIANA", "IA": "IOWA", "KS": "KANSAS", "KY": "KENTUCKY", "LA": "LOUISIANA", "ME": "MAINE",
               "MD": "MARYLAND", "MA": "MASSACHUSETTS", "MI": "MICHIGAN", "MN": "MINNESOTA", "MS": "MISSISSIPPI", "MO": "MISSOURI",
               "MT": "MONTANA", "NE": "NEBRASKA", "NV": "NEVADA", "NH": "NEWHAMPSHIRE", "NJ": "NEWJERSEY", "NM": "NEWMEXICO",
               "NY": "NEWYORK", "NC": "NORTHCAROLINA", "ND": "NORTHDAKOTA", "OH": "OHIO", "OK": "OKLAHOMA", "OR": "OREGON",
               "PA": "PENNSYLVANIA", "RI": "RHODEISLAND", "SC": "SOUTHCAROLINA", "SD": "SOUTHDAKOTA", "TN": "TENNESSEE",
               "TX": "TEXAS", "UT": "UTAH", "VT": "VERMONT", "VA": "VIRGINIA", "WA": "WASHINGTON", "WV": "WESTVIRGINIA",
               "WI": "WISCONSIN", "WY": "WYOMING", "DC": "DISTRICTOFCOLUMBIA", "PR": "PUERTORICO"}


def norm_key(name: str | None, state: str | None = None) -> str:
    if not name:
        return ""
    s = name.upper().replace(".", " ").replace(",", " ").replace("-", " ").replace("/", " ")
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    s = re.sub(r"^CITY OF ", "", s.strip())
    s = re.sub(r"\b(TOWNSHIP|TWP|CHARTER TOWNSHIP|BOROUGH|VILLAGE OF|TOWN OF)\b", " ", s)
    key = "".join(ABBREV.get(w, w) for w in s.split())
    full = STATE_NAMES.get((state or "").upper())
    if full and key.endswith(full) and len(key) > len(full):
        key = key[: -len(full)]
    return key


class GeocodeUnavailable(RuntimeError):
    """The fallback geocoder could not be used; the result must not be cached as a miss."""


@dataclass(frozen=True)
class GeoPoint:
    lat: float
    lon: float
    name: str
    source: str


@dataclass(frozen=True)
class _Place:
    key: str
    name: str
    lat: float
    lon: float
    aland: float


class Gazetteer:
    """In-memory index of data/us_places.csv (state, name, census_name, lat, lon, aland_sqmi)."""

    def __init__(self, csv_path: Path):
        self.exact: dict[tuple[str, str], _Place] = {}
        self.by_state: dict[str, list[_Place]] = {}
        self._keys_by_state: dict[str, list[str]] = {}
        with Path(csv_path).open("r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                place = _Place(norm_key(row["name"]), f"{row['name']}, {row['state']}", float(row["lat"]), float(row["lon"]),
                               float(row.get("aland_sqmi") or 0))
                state = row["state"].upper()
                prev = self.exact.get((state, place.key))
                if prev is None or place.aland > prev.aland:
                    self.exact[(state, place.key)] = place
                self.by_state.setdefault(state, []).append(place)
        log.debug("gazetteer loaded: %d places", len(self.exact))

    def lookup(self, city: str | None, state: str | None) -> GeoPoint | None:
        if not city:
            return None
        if not state:
            return self.lookup_any_state(city)
        state = state.upper().strip()
        key = norm_key(city, state)
        if not key:
            return None
        hit = self.exact.get((state, key))
        if hit:
            return GeoPoint(hit.lat, hit.lon, hit.name, "gazetteer")
        if len(key) >= 4:
            cands = []
            for p in self.by_state.get(state, []):
                if p.key.startswith(key):
                    cands.append((len(p.key) - len(key), -p.aland, p))        # renamed/extended names: LAKEWORTH -> LAKEWORTHBEACH
                elif len(p.key) >= 6 and key.startswith(p.key) and len(key) - len(p.key) <= 4:
                    cands.append((len(key) - len(p.key) + 10, -p.aland, p))   # query carries a stray suffix
            if cands:
                cands.sort(key=lambda c: (c[0], c[1]))
                p = cands[0][2]
                return GeoPoint(p.lat, p.lon, p.name, "gazetteer~")
            # typos ("COUNCILL BLUFFS", "SHEPERDSVILLE"): closest key in the same state
            keys = self._keys_by_state.setdefault(state, [p.key for p in self.by_state.get(state, [])])
            close = difflib.get_close_matches(key, keys, n=1, cutoff=0.88)
            if close:
                p = self.exact[(state, close[0])]
                return GeoPoint(p.lat, p.lon, p.name, "gazetteer~")
        return None

    def lookup_any_state(self, city: str) -> GeoPoint | None:
        """No state given: accept the match only if one place clearly dominates (by land area)."""
        key = norm_key(city)
        cands = sorted((p for (st, k), p in self.exact.items() if k == key), key=lambda p: -p.aland)
        if not cands:
            return None
        if len(cands) == 1 or cands[0].aland >= 5 * max(cands[1].aland, 0.01):
            return GeoPoint(cands[0].lat, cands[0].lon, cands[0].name, "gazetteer?")
        return None


class NominatimClient:
    def __init__(self, url: str, user_agent: str, min_interval: float = 1.1):
        self.url = url
        self.http = PoliteSession(user_agent, min_interval=min_interval, timeout=30, max_retries=2)

    def locate(self, city: str, state: str) -> GeoPoint | None:
        params = {"q": f"{city}, {state}, USA", "format": "json", "limit": 1, "countrycodes": "us"}
        try:
            data = self.http.get_json(self.url, params=params)
        except HttpError as exc:
            if exc.status == 403:
                raise GeocodeUnavailable("Nominatim refused the request (HTTP 403). Put a real URL and contact e-mail in "
                                         "http.user_agent; OpenStreetMap blocks placeholder agents.") from exc
            raise GeocodeUnavailable(str(exc)) from exc
        except Exception as exc:  # network problems must never break a run
            raise GeocodeUnavailable(str(exc)) from exc
        if not data:
            return None
        hit = data[0]
        return GeoPoint(float(hit["lat"]), float(hit["lon"]), hit.get("display_name", f"{city}, {state}")[:80], "nominatim")


class Geocoder:
    def __init__(self, gazetteer: Gazetteer, db=None, nominatim: NominatimClient | None = None):
        self.gazetteer = gazetteer
        self.db = db
        self.nominatim = nominatim
        self.memo: dict[str, GeoPoint | None] = {}
        self.fallback_failures = 0
        self.fallback_disabled = False

    def locate(self, city: str | None, state: str | None) -> GeoPoint | None:
        if not city:
            return None
        key = f"{(state or '').upper()}|{norm_key(city, state)}"
        if key in self.memo:
            return self.memo[key]
        point = self.gazetteer.lookup(city, state)
        if point is None and not state:
            self.memo[key] = None
            return None
        if point is None and self.db is not None:
            row = self.db.geocode_get(key)
            if row is not None:
                point = GeoPoint(row["lat"], row["lon"], row["matched_name"], row["source"]) if row["lat"] is not None else None
                self.memo[key] = point
                return point
        if point is None and self.nominatim is not None and not self.fallback_disabled:
            try:
                point = self.nominatim.locate(city, state)
            except GeocodeUnavailable as exc:
                self.fallback_failures += 1
                log.warning("geocode fallback failed for %s, %s: %s", city, state, exc)
                if self.fallback_failures >= 3:
                    self.fallback_disabled = True
                    log.warning("geocode fallback disabled for the rest of this run")
                self.memo[key] = None
                return None
            if self.db is not None:
                self.db.geocode_put(key, point.lat if point else None, point.lon if point else None,
                                    point.name if point else None, point.source if point else "none")
        self.memo[key] = point
        return point


def haversine_miles(a: GeoPoint, b: GeoPoint) -> float:
    r = 3958.8
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dphi = p2 - p1
    dl = math.radians(b.lon - a.lon)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def road_miles(a: GeoPoint | None, b: GeoPoint | None, factor: float = 1.2) -> float | None:
    if a is None or b is None:
        return None
    return round(haversine_miles(a, b) * factor)
