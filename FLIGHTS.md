# Flight numbers, v1

Base URL: `https://juicypasta.github.io/jetcalm-data/flights/v1/`

For a flight number as printed on a ticket (`BA 117`), the airports that flight number usually flies (`LHR → JFK`). There are no times: the app and the web page still ask for the departure time on the ticket. Built by `jetcalm_data/flights.py`, about once a day, in the `pipeline` workflow.

## Source and credit

**Virtual Radar Server standing data**, https://github.com/vradarserver/standing-data, released under **CC0 1.0** (public domain dedication): routes stored by ADS-B call sign, gathered from ACARS messages and corrections sent by Virtual Radar Server users, published daily. No attribution is required; we credit it anyway: "Flight numbers: Virtual Radar Server standing data (CC0)".

It is crowd-sourced. A route can be out of date (BA 2 still reads JFK → London City, a route that ended in 2020) or wrong, and it is the route flown under that call sign, which for some airlines is not the number on the ticket. **Show it as the route the flight "usually" flies and let the person change the airports.** Never present it as their flight's confirmed route.

## Files

| File | What |
|---|---|
| `index.json` | Every airline in the table |
| `<IATA>.json` | One airline's flight numbers, e.g. `BA.json`, `U2.json`, `6E.json` |

### `index.json`

```json
{
  "version": 1,
  "generated": "2026-11-27T23:30:00Z",
  "builder": 1,
  "source": {"name": "Virtual Radar Server standing data", "url": "https://github.com/vradarserver/standing-data",
             "license": "CC0-1.0", "commit": "bd5f5d5af341ed9d75e1758e4a6bb740e8247f93", "committed": "2026-11-27T04:49:26Z"},
  "flights": 344798,
  "airlines": {
    "BA": {"name": "British Airways", "icao": ["BAW", "SHT"], "flights": 2119, "file": "BA.json", "sha": "b52f87c4aee3"}
  }
}
```

| Field | Notes |
|---|---|
| `version` | `1`. A breaking change gets a new base path (`flights/v2/`) |
| `generated` | When the table was built (UTC). Rebuilt about once a day |
| `builder` | Version of the rules below; clients can ignore it |
| `source` | Where the data came from, down to the commit |
| `flights` | Total flight numbers |
| `airlines` | IATA code → `name` (the source's), `icao` (the ICAO codes merged into it, largest first), `flights`, `file` (relative to the base URL), `sha` (12 hex characters; changes exactly when that airline's flights change) |

### `<IATA>.json`

```json
{"version": 1, "airline": "BA", "name": "British Airways", "generated": "2026-11-27T23:30:00Z",
 "flights": {"1": "LHR-JFK", "15": "LHR-SIN-SYD", "112": "JFK-LHR", "117": "LHR-JFK"}}
```

- Keys: the flight number without leading zeros, 1 to 4 digits, sorted numerically.
- Values: IATA airport codes in the order flown, joined by `-`. Two codes are one flight. More are a through flight with stops (`BA 15`: London → Singapore → Sydney): each pair of neighbours is a leg with its own departure time, so ask which leg.

## Reading a flight number

What the app and the web page do (`flights.parse` and `flights.lookup` are the reference):

1. Upper-case; drop spaces, hyphens and dots.
2. `^([A-Z][A-Z0-9]|[0-9][A-Z])(\d{1,4})[A-Z]?$` is an IATA airline and a number (`BA117`, `U28001`, `B61`, `6E123`). Otherwise `^([A-Z]{3})(\d{1,4})[A-Z]?$` is an ICAO airline (`BAW117`): find the airline whose `icao` lists it. A trailing letter (an operational suffix) is ignored. Anything else is not a flight number.
3. Drop leading zeros from the number (`0117` → `117`; `0000` → `0`).
4. No `airlines[code]`: an airline we don't know. No `flights[number]` in its file: a flight we don't know. In both cases say so and offer the airport search.
5. Keep only legs whose airports the client knows (the app's `assets/airports.json`; the page's `airports.json`).

## What is in, what is left out

- **Live airlines only:** an operator is in when it added at least one new call sign to the source in the last two years (read from the source's git history). Airlines that stopped flying are out (US Airways, Continental, AirTran, Germania), and a reused code goes to its current holder (`DP` is Pobeda, not First Choice Airways).
- **Shared codes:** several operators can share an IATA code. The one with the most flights is the airline. Another is merged only if its name shares a word with it (`BA`: British Airways + BA Shuttle; `QF`: Qantas + QantasLink; `CA`: Air China + Air China Cargo + Air China Inner Mongolia). Airbus Transport International does not join `4Y` Discover Airlines. On a number both have, the larger operator wins.
- **Numeric call signs only** (`BAW117`). Alphanumeric ones (`BAW8EP`, `RYR5UP`, `EZY12AB`) are not ticket numbers and are left out, so short-haul flights of airlines that use them (Ryanair, easyJet, BA in Europe, Wizz Air, Air France) are often missing.
- Positioning (empty) flights, by the source's per-airline pattern, are left out.
- A route with an airport that has no IATA code is left out.

Counts on 2026-11-27: 630,397 routes in the source; 344,798 flight numbers in 686 airlines published (240,011 alphanumeric, 33,394 from airlines with no new call sign in two years, 9,786 from operators with no IATA code, 879 from a different brand on a shared code, 490 with an airport without an IATA code, 33 positioning).

## Sizes and caching

- `index.json` is about 67 KB (20 KB gzipped on the wire). Airline files are 1 to 140 KB (Ryanair, 47 KB gzipped); `BA.json` is 35 KB. GitHub Pages gzips JSON for clients that accept it.
- Fetch `index.json` at most about once a day (Pages sends `max-age=600` and an `ETag`); keep it for offline use.
- Keep airline files; fetch one again only when its `sha` in the index has changed.

## How it is built

`python -m jetcalm_data.flights --site site` (a step of the hourly `pipeline` workflow):

1. `git ls-remote` the source. If the table was built from that commit by these rules less than 20 hours ago, stop.
2. Clone the source with 2 years and 30 days of history (about 25 MB, 5 seconds). Walk `git log -p` of `routes/` for the newest date each operator added a call sign not seen before.
3. Apply the rules above and write `flights/v1/` (airlines that left are removed).
4. **Not published** (the old table stays, the run ends red) when the source fails, the table has under 100,000 flights, or it has under half the flights of the published one.

The `staleness` workflow also fails when `index.json` is older than 72 hours or invalid.

`python -m jetcalm_data.flights --site site --lookup "BA 117"` looks a number up in a built table.
