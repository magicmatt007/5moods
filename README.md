# Five Moods menu → Home Assistant

Publishes the daily lunch menu of **Five Moods (Siemens, Zug)** as a JSON
file and an `.ics` calendar via GitHub Actions + GitHub Pages, so it can be
pulled into Home Assistant.

**Not affiliated with SV Group.** This is an independent, unofficial
project that reads publicly visible menu data from sv-gastronomie.ch. It is
not endorsed, sponsored by, or otherwise connected to SV Group / SV
(Schweiz) AG.

## Why headless Chrome?

sv-gastronomie.ch is an Angular single-page app with no public JSON/REST
API — the menu is streamed from Firestore at runtime. `scripts/fetch_menu.py`
renders the page with headless Chrome (Selenium) and parses the resulting
DOM instead.

## Files

- `scripts/fetch_menu.py` — renders the menu pages and writes the outputs below.
- `config/restaurant.yaml` — the restaurant's menu URL.
- `docs/five-moods-zug.json` — structured menu data (category, dish, description, price, tags), keyed both by date (`days`) and by weekday label (`by_weekday`, Mo..Fr).
- `docs/five-moods-zug.ics` — the same data as a subscribable calendar.
- `homeassistant/` — example Home Assistant sensor, helper and Lovelace card YAML.

## Setup

Repo: https://github.com/magicmatt007/5moods (public, GitHub Pages enabled
on `/docs`). The menu is published at:

```
https://magicmatt007.github.io/5moods/five-moods-zug.json
https://magicmatt007.github.io/5moods/five-moods-zug.ics
```

The `Update Menu` GitHub Actions workflow runs daily at 05:00 UTC
(~07:00 CEST) and re-publishes `docs/*` if the menu changed. Trigger it
manually any time from **Actions → Update Menu → Run workflow**.

### Home Assistant

Add the RESTful sensor from `homeassistant/rest_sensor.yaml`, the template
sensor from `homeassistant/template_sensor.yaml`, then add the Markdown
card from `homeassistant/lovelace_card.yaml` to a dashboard — this gives a
card that always shows today's menu.

To browse other available days from the dashboard (a compact weekday
tab/dropdown selector), also add the `five_moods_day` helper from
`homeassistant/input_select.yaml` and the card from
`homeassistant/lovelace_card_tabs.yaml`. It looks up the selected weekday
(Mo..Fr) against `sensor.five_moods_menu`'s `by_weekday` attribute, so it
always shows whichever date currently matches that weekday, without
needing to know the actual dates.

Alternatively, subscribe to the `.ics` URL directly as a calendar using the
HACS `ics_calendar` custom integration if you just want a daily calendar
event instead of a structured card.

## Known limitations

- Only days already published by the restaurant are fetched (typically the
  current week); future weeks simply don't exist yet.
- Weekends are skipped.
- If the restaurant is closed (e.g. holidays), that day has no entries.
- This depends on scraping the site's current markup. If SV Group changes
  it, parsing can break — the workflow uploads a debug-HTML artifact on
  failure, and a broken run leaves the previously published `docs/*` files
  untouched rather than overwriting them with empty data.

## License

MIT — parsing approach adapted from
[Glukas/svgroup-menu-zu-ical](https://github.com/Glukas/svgroup-menu-zu-ical).
