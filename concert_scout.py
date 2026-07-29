#!/usr/bin/env python3
"""Free, small concert alert powered by Ticketmaster Discovery API v2."""

from __future__ import annotations

import argparse
import html
from html.parser import HTMLParser
import json
import logging
import math
import os
import re
import smtplib
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
API_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
IN_THE_PIT_URL = "https://www.abc.net.au/triplejunearthed/programs/in-the-pit"
LOG = logging.getLogger("concert_scout")
EXCLUDED_PHRASES = (
    "tribute to", "a tribute", "impersonator", "parking", "vip package",
    "vip club", "meet and greet", "nightclub party"
)


def normalize_artist(value: str) -> str:
    """Normalize punctuation/case while preserving meaningful words."""
    value = value.replace("’", " ").replace("‘", " ").replace("–", " ").replace("—", " ")
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = value.lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value).split())


def exact_artist_match(names: Iterable[str], priority_artists: Iterable[str]) -> str | None:
    priorities = {normalize_artist(a): a for a in priority_artists}
    for name in names:
        normalized = normalize_artist(name)
        if normalized in priorities:
            return priorities[normalized]
    return None


def is_excluded_event(event: dict[str, Any]) -> bool:
    name = normalize_artist(event.get("name", ""))
    # "Experience" is too broad alone (e.g. named performers can use it), but
    # is a tribute warning when paired with another tribute indicator.
    if any(normalize_artist(p) in name for p in EXCLUDED_PHRASES):
        return True
    if "experience" in name and ("tribute" in name or not event.get("_embedded", {}).get("attractions")):
        return True
    classifications = event.get("classifications") or []
    segment = normalize_artist((classifications[0].get("segment") or {}).get("name", "")) if classifications else ""
    genre = normalize_artist((classifications[0].get("genre") or {}).get("name", "")) if classifications else ""
    return segment not in ("", "music") or genre in {"comedy", "theatre", "sports"}


def event_names(event: dict[str, Any]) -> list[str]:
    attractions = event.get("_embedded", {}).get("attractions", [])
    return [a["name"] for a in attractions if a.get("name")] or [event.get("name", "")]


def classification_terms(event: dict[str, Any]) -> set[str]:
    terms: set[str] = set()
    for item in event.get("classifications") or []:
        for key in ("segment", "genre", "subGenre", "type", "subType"):
            value = (item.get(key) or {}).get("name")
            if value:
                terms.add(normalize_artist(value))
    for attraction in event.get("_embedded", {}).get("attractions", []):
        for item in attraction.get("classifications") or []:
            for key in ("genre", "subGenre"):
                value = (item.get(key) or {}).get("name")
                if value:
                    terms.add(normalize_artist(value))
    return terms


def terms_match(terms: set[str], keywords: Iterable[str]) -> str | None:
    normalized_keywords = [normalize_artist(keyword) for keyword in keywords]
    return next(
        (term for term in sorted(terms) if any(keyword in term for keyword in normalized_keywords)),
        None,
    )


def abc_next_data(page: str) -> dict[str, Any]:
    """Extract ABC's public page data without depending on its visual markup."""
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        page,
        flags=re.DOTALL,
    )
    if not match:
        raise ValueError("ABC page did not contain its public episode data")
    return json.loads(html.unescape(match.group(1)))


class TripleJInThePitSource:
    """Read exact artists from the latest official In The Pit tracklist."""

    def __init__(self, timeout: int = 20):
        self.timeout = timeout

    def _page(self, url: str) -> str:
        request = Request(url, headers={"User-Agent": "concert-scout/1.0"})
        with urlopen(request, timeout=self.timeout) as response:
            return response.read().decode("utf-8")

    def latest_artists(self) -> list[str]:
        program = abc_next_data(self._page(IN_THE_PIT_URL))
        latest = program["props"]["pageProps"]["latestEpisodePrepared"]["items"][0]
        episode_url = urljoin(IN_THE_PIT_URL, latest["articleLink"])
        episode = abc_next_data(self._page(episode_url))
        tracks = episode["props"]["pageProps"]["data"]["documentProps"]["tracklistPrepared"]["items"]
        return list(dict.fromkeys(track["artist"].strip() for track in tracks if track.get("artist")))


def classify_event(
    event: dict[str, Any],
    priority_artists: Iterable[str],
    pandora_liked_artists: Iterable[str] = (),
    strong_genres: Iterable[str] = ("soul", "neo-soul", "R&B", "funk", "jazz", "gospel"),
    discovery_genres: Iterable[str] = ("blues", "reggae", "afrobeat"),
) -> tuple[str, str] | None:
    artist = exact_artist_match(event_names(event), priority_artists)
    if artist:
        return "MUST SEE", f"Priority artist match: {artist}"
    liked_artist = exact_artist_match(event_names(event), pandora_liked_artists)
    if liked_artist:
        return "STRONG MATCH", f"Artist from your liked music: {liked_artist}"
    terms = classification_terms(event)
    strong_match = terms_match(terms, strong_genres)
    if strong_match:
        return "STRONG MATCH", f"Genre match: {strong_match.title()}"
    discovery_match = terms_match(terms, discovery_genres)
    if discovery_match:
        return "DISCOVERY", f"Adjacent genre match: {discovery_match.title()}"
    return None


def deduplicate(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for event in events:
        event_id = event.get("id")
        if event_id and event_id not in by_id:
            by_id[event_id] = event
    return list(by_id.values())


def price_text(event: dict[str, Any]) -> str:
    ranges = event.get("priceRanges") or []
    if not ranges:
        return "Price not published"
    lows = [p["min"] for p in ranges if isinstance(p.get("min"), (int, float))]
    highs = [p["max"] for p in ranges if isinstance(p.get("max"), (int, float))]
    if not lows and not highs:
        return "Price not published"
    currency = next((p.get("currency") for p in ranges if p.get("currency")), "USD")
    low = min(lows) if lows else min(highs)
    high = max(highs) if highs else max(lows)
    return f"${low:,.2f}–${high:,.2f} {currency}" if low != high else f"${low:,.2f} {currency}"


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    radius = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))


def local_event_datetime(event: dict[str, Any], tz_name: str) -> tuple[str, str]:
    start = event.get("dates", {}).get("start", {})
    local_date = start.get("localDate", "Date TBA")
    local_time = start.get("localTime")
    if local_time:
        try:
            display_time = datetime.strptime(local_time[:5], "%H:%M").strftime("%-I:%M %p")
        except ValueError:
            display_time = local_time
    else:
        display_time = "Time TBA"
    return local_date, f"{display_time} {datetime.now(ZoneInfo(tz_name)).tzname()}"


def sale_text(event: dict[str, Any]) -> str:
    status = (event.get("dates", {}).get("status") or {}).get("code")
    public = (event.get("sales", {}).get("public") or {})
    if status and status not in ("onsale", "offsale"):
        return status.replace("_", " ").title()
    if status == "onsale":
        return "On sale"
    start = public.get("startDateTime")
    if start:
        return "On sale " + start.replace("T", " ").replace("Z", " UTC")
    return (status or "Sale status unavailable").replace("_", " ").title()


class TicketmasterSource:
    """Isolated source adapter so additional official APIs can be added later."""

    def __init__(self, api_key: str, timeout: int = 20, retries: int = 3):
        self.api_key, self.timeout, self.retries = api_key, timeout, retries

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        url = API_URL + "?" + urlencode({**params, "apikey": self.api_key})
        for attempt in range(self.retries):
            try:
                with urlopen(Request(url, headers={"User-Agent": "concert-scout/1.0"}), timeout=self.timeout) as response:
                    return json.load(response)
            except Exception as exc:
                if attempt == self.retries - 1:
                    raise RuntimeError(f"Ticketmaster request failed after {self.retries} attempts") from exc
                time.sleep(2 ** attempt)
        return {}

    def search_area(self, area: dict[str, Any], start: str, end: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        page = 0
        while True:
            data = self._get({
                "city": area["city"], "stateCode": area["state_code"],
                "radius": area["radius_miles"], "unit": "miles",
                "classificationName": "music", "startDateTime": start,
                "endDateTime": end, "size": 200, "page": page,
                "sort": "date,asc"
            })
            events.extend(data.get("_embedded", {}).get("events", []))
            info = data.get("page", {})
            if page + 1 >= info.get("totalPages", 0):
                break
            page += 1
            if page >= 5:  # Discovery API deep paging limit (1000 results).
                LOG.warning("Stopped pagination at Ticketmaster's 1,000-result limit for %s", area["city"])
                break
        return events

    def search_artist(self, artist: str, area: dict[str, Any], start: str, end: str) -> list[dict[str, Any]]:
        data = self._get({
            "keyword": artist, "city": area["city"], "stateCode": area["state_code"],
            "radius": area["radius_miles"], "unit": "miles",
            "classificationName": "music", "startDateTime": start,
            "endDateTime": end, "size": 50, "sort": "date,asc"
        })
        return data.get("_embedded", {}).get("events", [])


def known_artists_in_title(title: str, artists: Iterable[str]) -> list[str]:
    normalized_title = f" {normalize_artist(title)} "
    matches = []
    for artist in artists:
        normalized_artist = normalize_artist(artist)
        if normalized_artist and f" {normalized_artist} " in normalized_title:
            matches.append(artist)
    return matches


def local_calendar_event(
    event_id: str,
    title: str,
    event_date: str,
    event_time: str | None,
    venue: str,
    city: str,
    state: str,
    url: str,
    artists: Iterable[str],
    description: str = "",
    price: str = "",
) -> dict[str, Any]:
    attractions = known_artists_in_title(title, artists)
    genre_text = normalize_artist(f"{title} {description}")
    subgenre = "Neo-Soul" if "neo soul" in genre_text else "Undefined"
    price_match = re.search(r"\$\s*(\d+(?:\.\d{1,2})?)", price)
    result: dict[str, Any] = {
        "id": event_id,
        "name": title,
        "url": url,
        "dates": {"start": {"localDate": event_date, "localTime": event_time}, "status": {"code": "onsale"}},
        "classifications": [{"segment": {"name": "Music"}, "genre": {"name": "R&B"}, "subGenre": {"name": subgenre}}],
        "_embedded": {
            "attractions": [{"name": artist} for artist in attractions],
            "venues": [{"name": venue, "city": {"name": city}, "state": {"stateCode": state}}],
        },
    }
    if price_match:
        amount = float(price_match.group(1))
        result["priceRanges"] = [{"min": amount, "max": amount, "currency": "USD"}]
    return result


class VisitKearneySource(TicketmasterSource):
    API = "https://visitkearney.org/wp-json/tribe/events/v1/events"

    def search(self, start_day: date, end_day: date, artists: Iterable[str]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._get_url(self.API, {
                "start_date": start_day.isoformat(), "end_date": end_day.isoformat(),
                "per_page": 50, "page": page,
            })
            for item in data.get("events", []):
                title = html.unescape(item.get("title", ""))
                description = re.sub(r"<[^>]+>", " ", html.unescape(item.get("description", "")))
                categories = " ".join(html.unescape(c.get("name", "")) for c in item.get("categories", []))
                searchable = normalize_artist(f"{title} {description} {categories}")
                if not known_artists_in_title(title, artists) and not any(
                    term in searchable for term in ("concert", "live music", "neo soul", " r b ", "rhythm and blues")
                ):
                    continue
                venue = item.get("venue") or {}
                start_value = item.get("start_date", "")
                results.append(local_calendar_event(
                    f"kearney:{item.get('id')}", title, start_value[:10],
                    start_value[11:19] or None, venue.get("venue", "Venue TBA"),
                    venue.get("city", "Kearney"), venue.get("state", "NE"),
                    item.get("website") or item.get("url", ""), artists,
                    description, item.get("cost", ""),
                ))
            if page >= data.get("total_pages", 1):
                break
            page += 1
        return results

    def _get_url(self, base: str, params: dict[str, Any]) -> dict[str, Any]:
        url = base + "?" + urlencode(params)
        for attempt in range(self.retries):
            try:
                with urlopen(Request(url, headers={"User-Agent": "concert-scout/1.0"}), timeout=self.timeout) as response:
                    return json.load(response)
            except Exception as exc:
                if attempt == self.retries - 1:
                    raise RuntimeError("Visit Kearney calendar request failed") from exc
                time.sleep(2 ** attempt)
        return {}


class LiedEventsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.events: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None
        self.capture: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        css_class = values.get("class", "") or ""
        if tag == "div" and css_class == "title":
            self.current = {}
            self.capture = "title"
        elif self.current is not None and tag == "div" and css_class == "date":
            self.capture = "date"
        elif self.current is not None and tag == "a":
            href = values.get("href")
            if self.capture == "title" and href:
                self.current["url"] = urljoin("https://www.liedcenter.org", href)
            elif href and ("ticket" in href or "stellar" in href):
                self.current["tickets"] = href

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.capture in ("title", "date"):
            self.current[self.capture] = self.current.get(self.capture, "") + data.strip()

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self.capture in ("title", "date"):
            if self.capture == "date" and self.current and self.current.get("title") and self.current.get("date"):
                self.events.append(self.current)
            self.capture = None


class LiedCenterSource:
    URL = "https://www.liedcenter.org/events-page"

    def __init__(self, timeout: int = 20):
        self.timeout = timeout

    def search(self, start_day: date, end_day: date, artists: Iterable[str]) -> list[dict[str, Any]]:
        with urlopen(Request(self.URL, headers={"User-Agent": "concert-scout/1.0"}), timeout=self.timeout) as response:
            parser = LiedEventsParser()
            parser.feed(response.read().decode("utf-8", errors="replace"))
        results = []
        for item in parser.events:
            try:
                parsed_date = datetime.strptime(item["date"].split("–")[0].split("-")[0].strip(), "%B %d, %Y").date()
            except ValueError:
                continue
            if not start_day <= parsed_date <= end_day or not known_artists_in_title(item["title"], artists):
                continue
            slug = item.get("url", item["title"]).rstrip("/").rsplit("/", 1)[-1]
            results.append(local_calendar_event(
                f"lied:{slug}:{parsed_date}", item["title"], parsed_date.isoformat(), None,
                "Lied Center for Performing Arts", "Lincoln", "NE",
                item.get("tickets") or item.get("url", self.URL), artists,
            ))
        return results


def add_months(today: date, months: int) -> date:
    month = today.month - 1 + months
    year = today.year + month // 12
    month = month % 12 + 1
    import calendar
    return date(year, month, min(today.day, calendar.monthrange(year, month)[1]))


def snapshot(event: dict[str, Any]) -> dict[str, Any]:
    venue = ((event.get("_embedded", {}).get("venues") or [{}])[0])
    return {
        "name": event.get("name"),
        "date": event.get("dates", {}).get("start", {}).get("localDate"),
        "time": event.get("dates", {}).get("start", {}).get("localTime"),
        "venue": venue.get("name"),
        "url": event.get("url"),
        "price": price_text(event),
        "status": (event.get("dates", {}).get("status") or {}).get("code")
    }


def meaningful_change(old: dict[str, Any] | None, new: dict[str, Any]) -> str | None:
    if old is None:
        return "New event"
    labels = {
        "url": "Ticket link is now available", "price": "Ticket prices are now available",
        "venue": "Venue changed", "date": "Date changed", "time": "Start time changed",
        "status": "Event status changed"
    }
    for key, label in labels.items():
        if old.get(key) != new.get(key):
            if key == "url" and not new.get(key):
                continue
            if key == "price" and new.get(key) == "Price not published":
                continue
            return label
    return None


@dataclass
class ReportEvent:
    raw: dict[str, Any]
    label: str
    reason: str
    change: str
    distance: int | None
    timezone: str


def event_area(event: dict[str, Any], areas: list[dict[str, Any]]) -> dict[str, Any]:
    venue = ((event.get("_embedded", {}).get("venues") or [{}])[0])
    state = (venue.get("state") or {}).get("stateCode")
    city = normalize_artist((venue.get("city") or {}).get("name", ""))
    same_state = [a for a in areas if a["state_code"] == state]
    return next((a for a in same_state if normalize_artist(a["city"]) == city), same_state[0] if same_state else areas[0])


def render_email(reports: list[ReportEvent], profile_name: str = "") -> str:
    colors = {"MUST SEE": "#8b1e3f", "STRONG MATCH": "#305f72", "DISCOVERY": "#5c6b3c"}
    sections = []
    for label, heading in (("MUST SEE", "MUST SEE"), ("STRONG MATCH", "STRONG MATCHES"), ("DISCOVERY", "DISCOVERIES")):
        cards = []
        for report in (r for r in reports if r.label == label):
            e = report.raw
            venue = ((e.get("_embedded", {}).get("venues") or [{}])[0])
            city = (venue.get("city") or {}).get("name", "City TBA")
            state = (venue.get("state") or {}).get("stateCode", "")
            date_value, time_value = local_event_datetime(e, report.timezone)
            lineup = ", ".join(event_names(e))
            distance = f"{report.distance} miles from Elm Creek" if report.distance is not None else "Distance unavailable"
            url = html.escape(e.get("url") or "#", quote=True)
            cards.append(f"""
              <div style="border:1px solid #ddd;border-left:5px solid {colors[label]};padding:16px;margin:12px 0;border-radius:5px">
                <div style="font-size:12px;color:{colors[label]};font-weight:bold">{label} · {html.escape(report.change)}</div>
                <h3 style="margin:6px 0">{html.escape(e.get("name", "Untitled event"))}</h3>
                <div><b>Lineup:</b> {html.escape(lineup)}</div>
                <div><b>When:</b> {html.escape(date_value)} at {html.escape(time_value)}</div>
                <div><b>Where:</b> {html.escape(venue.get("name", "Venue TBA"))}, {html.escape(city)}, {html.escape(state)}</div>
                <div><b>Distance:</b> {distance}</div>
                <div><b>Price:</b> {html.escape(price_text(e))}</div>
                <div><b>Tickets:</b> {html.escape(sale_text(e))}</div>
                <div style="margin:8px 0;color:#555">{html.escape(report.reason)}</div>
                <a href="{url}" style="display:inline-block;background:#222;color:white;text-decoration:none;padding:9px 14px;border-radius:4px">View Tickets</a>
              </div>""")
        if cards:
            sections.append(f"<h2 style='margin-top:28px'>{heading}</h2>{''.join(cards)}")
    return f"""<!doctype html><html><body style="font-family:Arial,sans-serif;max-width:720px;margin:auto;color:#222">
      <h1>{html.escape(profile_name + " " if profile_name else "")}Concert Scout</h1>
      <p>New concerts and meaningful updates near your selected cities.</p>
      {''.join(sections)}
      <p style="color:#777;font-size:12px">Distances are approximate straight-line distances from Elm Creek, Nebraska.</p>
    </body></html>"""


def send_email(reports: list[ReportEvent], recipient_env: str, profile_name: str = "") -> None:
    sender = os.environ["ALERT_EMAIL_FROM"]
    recipient = os.environ[recipient_env]
    message = EmailMessage()
    message["From"], message["To"] = sender, recipient
    prefix = f"{profile_name} " if profile_name else ""
    message["Subject"] = f"{prefix}Concert Scout: {len(reports)} new matches — {date.today().isoformat()}"
    message.set_content("Concert Scout found new matches. View this message in an HTML-capable email client.")
    message.add_alternative(render_email(reports, profile_name), subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(sender, os.environ["GMAIL_APP_PASSWORD"])
        smtp.send_message(message)


def minimum_price(event: dict[str, Any]) -> float:
    values = [
        item["min"] for item in event.get("priceRanges") or []
        if isinstance(item.get("min"), (int, float))
    ]
    return min(values) if values else math.inf


def report_sort_key(report: ReportEvent, config: dict[str, Any]) -> tuple[Any, ...]:
    rank = {"MUST SEE": 0, "STRONG MATCH": 1, "DISCOVERY": 2}
    venue = ((report.raw.get("_embedded", {}).get("venues") or [{}])[0]).get("name", "")
    normalized_venue = normalize_artist(venue)
    preferred = config.get("preferred_venues", [])
    venue_rank = 0 if any(normalize_artist(name) in normalized_venue for name in preferred) else 1
    price = minimum_price(report.raw) if config.get("prefer_low_prices") else 0
    return (
        rank[report.label], venue_rank, price,
        report.distance if report.distance is not None else 99999,
        report.raw.get("dates", {}).get("start", {}).get("localDate", "9999"),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search for new concerts and email alerts.")
    parser.add_argument("--config", default="config.json", help="Profile JSON path, relative to this project.")
    parser.add_argument("--state", default="data/seen_events.json", help="Separate state JSON path for this profile.")
    parser.add_argument("--recipient-env", default="ALERT_EMAIL_TO", help="Environment variable containing recipient email.")
    return parser.parse_args(argv)


def project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args(argv)
    config_path = project_path(args.config)
    state_path = project_path(args.state)
    api_key = os.environ.get("TICKETMASTER_API_KEY")
    if not api_key:
        LOG.error("TICKETMASTER_API_KEY is required")
        return 2
    config = json.loads(config_path.read_text())
    old_state = json.loads(state_path.read_text()) if state_path.exists() else {}
    today = date.today()
    start = datetime.combine(today, dt_time.min, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = datetime.combine(add_months(today, config.get("months_ahead", 12)), dt_time.max, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    source = TicketmasterSource(api_key)
    found: list[dict[str, Any]] = []
    for area in config["search_areas"]:
        try:
            found.extend(source.search_area(area, start, end))
            for artist in config["priority_artists"]:
                found.extend(source.search_artist(artist, area, start, end))
        except Exception as exc:
            LOG.error("Search failed for %s; continuing: %s", area["city"], exc)
    liked_artists = list(config.get("pandora_liked_artists", []))
    artist_feeds = config.get("artist_feeds", {})
    if artist_feeds.get("triple_j_in_the_pit"):
        try:
            feed_artists = TripleJInThePitSource().latest_artists()
            liked_artists = list(dict.fromkeys(liked_artists + feed_artists))
            LOG.info("Loaded %d artists from the latest ABC In The Pit tracklist", len(feed_artists))
        except Exception as exc:
            LOG.error("ABC In The Pit artist feed failed; continuing: %s", exc)
    all_artists = list(dict.fromkeys(config["priority_artists"] + liked_artists))
    calendars = config.get("official_calendars", {})
    if calendars.get("visit_kearney"):
        try:
            found.extend(VisitKearneySource(api_key).search(today, add_months(today, config.get("months_ahead", 12)), all_artists))
        except Exception as exc:
            LOG.error("Visit Kearney calendar failed; continuing: %s", exc)
    if calendars.get("lied_center"):
        try:
            found.extend(LiedCenterSource().search(today, add_months(today, config.get("months_ahead", 12)), all_artists))
        except Exception as exc:
            LOG.error("Lied Center calendar failed; continuing: %s", exc)
    reports: list[ReportEvent] = []
    new_state = dict(old_state)
    home = config["home"]
    for event in deduplicate(found):
        if is_excluded_event(event):
            continue
        classification = classify_event(
            event,
            config["priority_artists"],
            liked_artists,
            config.get("strong_genre_keywords", []),
            config.get("discovery_genre_keywords", []),
        )
        if not classification:
            continue
        event_id = event["id"]
        current = snapshot(event)
        change = meaningful_change(old_state.get(event_id), current)
        new_state[event_id] = current
        if not change:
            continue
        venue = ((event.get("_embedded", {}).get("venues") or [{}])[0])
        location = venue.get("location") or {}
        try:
            distance = haversine_miles(home["latitude"], home["longitude"], float(location["latitude"]), float(location["longitude"]))
        except (KeyError, TypeError, ValueError):
            distance = None
        area = event_area(event, config["search_areas"])
        reports.append(ReportEvent(event, *classification, change, distance, area["timezone"]))
    reports.sort(key=lambda report: report_sort_key(report, config))
    if reports:
        if not os.environ.get(args.recipient_env):
            LOG.error("%s is required when matches need to be emailed", args.recipient_env)
            return 2
        send_email(reports, args.recipient_env, config.get("profile_name", ""))
        LOG.info("Sent %d concert alert(s)", len(reports))
    else:
        LOG.info("No new or meaningfully changed events; no email sent")
    if new_state != old_state:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(new_state, indent=2, sort_keys=True) + "\n")
        LOG.info("Updated state")
    return 0


if __name__ == "__main__":
    sys.exit(main())
