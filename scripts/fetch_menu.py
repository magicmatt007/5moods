"""Fetches the Five Moods (Siemens, Zug) lunch menu from sv-gastronomie.ch.

The site is an Angular single-page app backed by Firestore; there is no
public JSON/REST API, so the page is rendered with headless Chrome and the
resulting DOM is parsed instead. Output: an .ics calendar (one event per
day) and a menu.json with structured per-dish data, both under docs/ so
they can be published via GitHub Pages.

If a run manages to parse zero entries (e.g. a transient render glitch or
the site's markup having changed), the previous docs/ output is left
untouched and the run exits non-zero, so a broken parser doesn't silently
overwrite good data.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml
from bs4 import BeautifulSoup
from icalendar import Calendar, Event
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("five_moods_menu")

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "restaurant.yaml"
OUTPUT_DIR = ROOT / "docs"

# Calendar days (not weekdays) ahead of today to request; weekends are
# skipped, so this comfortably covers the rest of the working week.
DAYS_AHEAD = 7

# Set to True to dump the rendered HTML to debug_page_{date}.html, useful
# if the site's markup changes and parsing needs to be re-derived.
DEBUG_DUMP_HTML = False


class ParserError(Exception):
    """Raised when the rendered page doesn't match the expected structure."""


@dataclass
class MenuEntry:
    day: date
    category: str | None
    dish: str
    description: str | None = None
    price: str | None = None
    tags: list[str] = field(default_factory=list)

    def dedup_key(self) -> tuple:
        return (self.category, self.dish, self.description)

    def as_dict(self) -> dict:
        return {
            "category": self.category,
            "dish": self.dish,
            "description": self.description,
            "price": self.price,
            "tags": self.tags,
        }


def load_restaurant() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--lang=de-CH")
    options.add_argument("--window-size=1400,2000")

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def fetch_rendered_page(driver: webdriver.Chrome, url: str, debug_tag: str = "") -> str:
    try:
        driver.set_page_load_timeout(30)
        driver.get(url)
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "mat-card.product-card, app-category"))
            )
        except Exception:
            log.warning("Timed out waiting for menu content on %s", url)
        html = driver.page_source
    except Exception as e:
        raise ParserError(f"Failed to load {url}: {e}") from e

    if DEBUG_DUMP_HTML:
        debug_path = ROOT / f"debug_page_{debug_tag}.html"
        debug_path.write_text(html, encoding="utf-8")
        log.info("Saved debug HTML to %s", debug_path)

    return html


def extract_menu_entries(html: str, day: date) -> list[MenuEntry]:
    soup = BeautifulSoup(html, "html.parser")

    body_text = soup.get_text(" ", strip=True)
    if not body_text or len(body_text) < 50:
        raise ParserError("Rendered page has (almost) no text; JS probably didn't finish loading")

    categories = soup.select("app-category")
    if not categories:
        raise ParserError(
            "No <app-category> element found; the site's markup has probably "
            "changed. Enable DEBUG_DUMP_HTML and inspect the saved page."
        )

    entries: list[MenuEntry] = []
    for category_el in categories:
        header = category_el.select_one("h3.category-header")
        category_name = header.get_text(strip=True) if header else None

        for product in category_el.select("mat-card.product-card"):
            name_el = product.select_one(".product-title button")
            if not name_el:
                continue

            teaser_el = product.select_one(".push-bottom-xs")
            price_el = product.select_one(".price-container .price")
            price_text = None
            if price_el:
                # Text mixes a tier label ("EXT"/"INT") with the amount, e.g.
                # "EXT CHF 16.50" - keep just the CHF amount.
                match = re.search(r"CHF\s*[\d.,]+", price_el.get_text(" ", strip=True))
                price_text = match.group(0) if match else price_el.get_text(strip=True)

            tags = [
                (img.get("title") or img.get("alt") or "").strip()
                for img in product.select("app-product-custom-tag img")
            ]
            tags = [t for t in tags if t]

            entries.append(
                MenuEntry(
                    day=day,
                    category=category_name,
                    dish=name_el.get_text(strip=True),
                    description=teaser_el.get_text(strip=True) if teaser_el else None,
                    price=price_text,
                    tags=tags,
                )
            )

    if not entries:
        log.info("No menu entries for %s (restaurant likely closed that day)", day)

    return entries


def deduplicate_entries(day_entries: list[MenuEntry]) -> list[MenuEntry]:
    seen: set[tuple] = set()
    deduped: list[MenuEntry] = []
    for e in day_entries:
        key = e.dedup_key()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    return deduped


def format_day_description(day_entries: list[MenuEntry]) -> str:
    lines: list[str] = []
    last_category = object()

    for i, e in enumerate(day_entries):
        if e.category != last_category:
            if i > 0:
                lines.append("")
            if e.category:
                lines.append(e.category.upper())
                lines.append("-" * len(e.category))
            last_category = e.category

        lines.append(e.dish)
        if e.description:
            lines.append(e.description)
        if e.price:
            lines.append(e.price)
        if e.tags:
            lines.append(f"({', '.join(e.tags)})")
        lines.append("")

    return "\n".join(lines).strip()


def build_ics(restaurant_name: str, entries_by_day: dict[date, list[MenuEntry]]) -> bytes:
    cal = Calendar()
    cal.add("prodid", f"-//Five Moods Menu//{restaurant_name}//DE")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", f"Mittagsmenu {restaurant_name}")

    for day, day_entries in sorted(entries_by_day.items()):
        event = Event()
        content_hash = hashlib.sha256(
            "|".join(f"{e.category}|{e.dish}|{e.description}|{e.price}" for e in day_entries).encode()
        ).hexdigest()[:12]
        event.add("uid", f"five-moods-zug-{day}-{content_hash}@5moods")
        event.add("summary", f"Mittagsmenu: {restaurant_name}")

        start = datetime.combine(day, datetime.min.time()).replace(hour=11, minute=30)
        event.add("dtstart", start)
        event.add("dtend", start + timedelta(minutes=90))
        event.add("dtstamp", datetime.now(timezone.utc))
        event.add("description", format_day_description(day_entries))
        event.add("location", restaurant_name)

        cal.add_component(event)

    return cal.to_ical()


def build_json(restaurant_name: str, entries_by_day: dict[date, list[MenuEntry]]) -> dict:
    return {
        "restaurant": restaurant_name,
        "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "days": {
            day.isoformat(): [e.as_dict() for e in day_entries]
            for day, day_entries in sorted(entries_by_day.items())
        },
    }


def fetch_all_entries(driver: webdriver.Chrome, base_url: str) -> dict[date, list[MenuEntry]]:
    today = date.today()
    entries_by_day: dict[date, list[MenuEntry]] = {}

    for offset in range(DAYS_AHEAD):
        day = today + timedelta(days=offset)
        if day.weekday() >= 5:
            continue

        day_url = f"{base_url.rstrip('/')}/date/{day.isoformat()}"
        try:
            html = fetch_rendered_page(driver, day_url, debug_tag=day.isoformat())
            day_entries = deduplicate_entries(extract_menu_entries(html, day))
            if day_entries:
                entries_by_day[day] = day_entries
            log.info("%s: %d entries", day, len(day_entries))
        except ParserError as e:
            log.warning("%s: parser error, skipped: %s", day, e)
            continue

    return entries_by_day


def main() -> int:
    restaurant = load_restaurant()
    name = restaurant["name"]
    slug = restaurant["slug"]

    driver = build_driver()
    try:
        entries_by_day = fetch_all_entries(driver, restaurant["url"])
    finally:
        driver.quit()

    if not entries_by_day:
        log.error("No entries found for any day; leaving previous docs/ output untouched")
        return 1

    OUTPUT_DIR.mkdir(exist_ok=True)

    ics_path = OUTPUT_DIR / f"{slug}.ics"
    ics_path.write_bytes(build_ics(name, entries_by_day))

    import json

    json_path = OUTPUT_DIR / f"{slug}.json"
    json_path.write_text(
        json.dumps(build_json(name, entries_by_day), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log.info("OK: wrote %s and %s", ics_path, json_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
