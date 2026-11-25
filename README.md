# jetcalm-data

The data pipeline behind **Jetcalm**, a turbulence-forecast app. Every hour it checks NOAA for a new forecast run and, when there is one, turns it into small files the app downloads:

```
https://juicypasta.github.io/jetcalm-data/v1/manifest.json
https://juicypasta.github.io/jetcalm-data/v1/<run>/f<hhh>.bin.gz
```

The file format is in [FORMAT.md](FORMAT.md).

## Data sources

| Source | Used for | How |
|---|---|---|
| **World Area Forecast System (WAFS) turbulence and cumulonimbus grids**, shown by the NOAA/NWS Aviation Weather Center at `aviationweather.gov/wafs` | The official forecast, 6–48 hours ahead, every 3 hours | The public display images (`/data/products/wafs/…_m.png`, 900×600 Web-Mercator) are downloaded for 7 flight levels plus cumulonimbus extent, and their colours are decoded back to values. An unknown colour or image size fails the step: nothing is guessed |
| **NOAA/NCEP Global Forecast System (GFS), 0.5°**, from NOAA Open Data on AWS (`noaa-gfs-bdp-pds`) | The outlook, 54–120 hours ahead, every 6 hours; and the whole feed if the WAFS images are unavailable | Wind and height at 150–400 hPa are fetched by HTTP byte range (about 4 MB of each 150 MB file). The Ellrod TI1 index (vertical wind shear × horizontal deformation), smoothed 3×3, is mapped to EDR by quantile mapping against the WAFS forecast for the same times |

The pipeline is polite to both: it identifies itself (`User-Agent: jetcalm-data/1.0 (+https://github.com/JuicyPasta/jetcalm-data)`), reuses one connection, fetches one file at a time, retries with backoff, and downloads a run once (about 125 small images and 60 MB of GFS fields per run, at most 8 times a day).

## Licence and credit

- The forecasts are works of the United States Government and are in the public domain (17 U.S.C. §105). **Credit: NOAA/NWS Aviation Weather Center and NCEP.**
- Jetcalm and this repository are **not affiliated with or endorsed by NOAA**, the National Weather Service or the Aviation Weather Center.
- These files are for passenger information. They are **not for flight planning or navigation**.
- The code in this repository is released under the MIT licence ([LICENSE](LICENSE)).

## How it runs

| Workflow | When | What |
|---|---|---|
| [`pipeline`](.github/workflows/pipeline.yml) | Hourly at :23, or by hand | Finds the newest complete WAFS run and GFS cycle. If `manifest.json` already names both, it stops. Otherwise it builds the step files and the manifest, keeps the two newest builds, and force-pushes the result as a single commit to `gh-pages` |
| [`staleness`](.github/workflows/monitor.yml) | Every 3 hours | Fails if the published manifest is older than 12 hours or a step file can't be read |
| [`tests`](.github/workflows/tests.yml) | On every push | `pytest` |
| [`verify`](.github/workflows/verify.yml) | Every 6 hours at :53 | Scores the published forecast against pilot reports from the Aviation Weather Center and commits the results to [`verification/`](verification/) and [`docs/verification.md`](docs/verification.md) (about a minute; the commit is `[skip ci]`) |
| [`reports`](.github/workflows/reports.yml) | Hourly at :17, or by hand | Writes the last 3 hours of pilot turbulence reports (smooth ones included, any altitude; aircraft type only, no call signs) to [`reports/latest.json`](reports/latest.json) on `main` for the free [turbulence tracker](https://juicypasta.github.io/jetcalm/turbulence-tracker/), which reads it from `raw.githubusercontent.com` because the AWC API sends no CORS header (about a minute; the commit is `[skip ci]`) |

If the WAFS step fails (images moved, palette changed, server down), the run publishes a GFS-only feed that still covers 6–120 hours, using the last stored calibration (`v1/calibration.json`, with the committed [`calibration.json`](calibration.json) as the last resort), and the workflow ends red. The manifest then has `"wafs_run": null` and a `problems` entry.

To show a service message in the app, commit a `notice.json` to the repository root (`{"en": "…", "de": "…"}`); the next build copies it into the manifest.

## Run it locally

```sh
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m jetcalm_data.run --site site          # builds site/v1
.venv/bin/python -m jetcalm_data.sample JFK LHR 2026-11-23T23:00:00Z   # reads the published feed
.venv/bin/python -m jetcalm_data.monitor
.venv/bin/python -m jetcalm_data.verify --site site/v1  # scores the feed in site/v1 against pilot reports
.venv/bin/python -m jetcalm_data.reports                # writes reports/latest.json
```

## Layout

| Path | |
|---|---|
| `jetcalm_data/wafs.py` | Image URLs, palette decode, the four layers |
| `jetcalm_data/grid.py` | The output grid and the Web-Mercator regrid |
| `jetcalm_data/gfs.py` | `.idx` byte ranges, GRIB2 decode, Ellrod TI1 |
| `jetcalm_data/calibrate.py` | Quantile mapping TI1 → EDR |
| `jetcalm_data/tiles.py` | Step-file encoding and the manifest schema |
| `jetcalm_data/run.py` | One pipeline run |
| `jetcalm_data/sample.py`, `monitor.py` | Reference reader; staleness check |
| `jetcalm_data/verify.py` | Forecast vs pilot reports ([docs/verification.md](docs/verification.md)) |
| `jetcalm_data/reports.py` | The latest pilot reports for the web tracker (reuses `verify`'s fetcher and parser) |
| `docs/thresholds.md` | The level thresholds checked against ICAO Annex 3, AWC and NCAR |
| `tests/fixtures/` | AWC images of the 2026-11-22 12Z run (F12) and the tile the original proof made from them |
