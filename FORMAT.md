# Jetcalm tile format, v1

Base URL: `https://juicypasta.github.io/jetcalm-data/v1/`

A client needs two things: `manifest.json`, then the step files it names. A reference reader in Python is `jetcalm_data/sample.py`.

The flight-number table under `flights/v1/` has its own format: [FLIGHTS.md](FLIGHTS.md).

## Grid

| | |
|---|---|
| Resolution | 0.5° latitude × 0.5° longitude |
| Rows | 301: latitude 75.0 (row 0) down to −75.0 (row 300) |
| Columns | 720: longitude −180.0 (column 0) east to 179.5 (column 719) |
| Cell value | one unsigned byte |
| Order | layer by layer; inside a layer row by row (north to south), west to east. Single bytes, so there is no byte order to get wrong |

Nearest cell for a point:

```
row = round((75 − lat) / 0.5)            valid for |lat| ≤ 75.25, else there is no data
col = round((lon + 180) / 0.5) mod 720
byte offset = layer_index × 216720 + row × 720 + col
```

**There is no coverage poleward of 75°.** Polar routes (for example New York to Hong Kong) leave the grid; treat those points as "no data", not as smooth.

## Values

| Layer | Meaning of a byte |
|---|---|
| `cruise_mid`, `cruise_max`, `low_max` | EDR × 100 (eddy dissipation rate, m^(2/3) s^-1). `0` = below 0.15, which the source does not draw. `15`–`100` = the forecast value. `255` = no data (reserved; not produced inside the grid today) |
| `cb` | Cumulonimbus horizontal extent, percent. `0` = none or under 30. `30`–`100` = cover. `255` = no data |

The four layers:

| Name | What it is | In which steps |
|---|---|---|
| `cruise_mid` | Turbulence at FL340 (250 hPa) | all |
| `cruise_max` | The largest of FL300, FL340 and FL390 | all |
| `low_max` | The largest of FL140, FL180, FL240 and FL270 (climb and descent) | `wafs` only |
| `cb` | Thunderstorm (cumulonimbus) cover | `wafs` only |

`cruise_mid ≤ cruise_max` in every cell.

## Step files

`v1/<directory>/f<hhh>.bin.gz`: gzip of the layers listed in the step's `layers`, concatenated in that order, 216,720 bytes each.

| Kind | Layers | Uncompressed | Gzipped (2026-11-22 18Z) |
|---|---|---|---|
| `wafs` | `cruise_mid`, `cruise_max`, `low_max`, `cb` | 866,880 bytes | 92–105 KB |
| `outlook` | `cruise_mid`, `cruise_max` | 433,440 bytes | 52–54 KB |

- **`wafs`**: the official World Area Forecast System turbulence forecast, 6 to 48 hours after the run, every 3 hours (15 steps).
- **`outlook`**: an estimate computed from the GFS model (Ellrod index, calibrated to WAFS on every run), 54 to 120 hours after the run, every 6 hours (12 steps). It is less exact than `wafs`: show it as an early outlook. If the WAFS source fails, `outlook` steps also cover hours 6 to 48, so the feed is always complete.

Always take the path from the manifest's `file` (relative to the base URL). Directory names are not part of the contract. A path never changes content once published.

GitHub Pages may serve the `.bin.gz` as `application/gzip` with no `Content-Encoding`: gunzip the body yourself.

## manifest.json

```json
{
  "version": 1,
  "generated": "2026-11-23T03:25:11Z",
  "wafs_run": "2026112218",
  "gfs_run": "2026112218",
  "grid": {"rows": 301, "cols": 720, "lat0": 75.0, "lon0": -180.0, "dlat": -0.5, "dlon": 0.5,
           "dtype": "uint8", "order": "layer, row (north to south), column (west to east)",
           "scale": 100, "below_cutoff": 0, "nodata": 255},
  "steps": [
    {"valid": "2026-11-23T00:00:00Z", "kind": "wafs", "file": "2026112218/f006.bin.gz",
     "layers": ["cruise_mid", "cruise_max", "low_max", "cb"], "bytes": 92369},
    {"valid": "2026-11-25T00:00:00Z", "kind": "outlook", "file": "2026112218-cal2026112218/f054.bin.gz",
     "layers": ["cruise_mid", "cruise_max"], "bytes": 53425}
  ],
  "thresholds": {"light": 15, "moderate": 22, "strong": 34, "severe": 45},
  "min_build": 1
}
```

| Field | Type | Notes |
|---|---|---|
| `version` | int | `1`. A breaking change gets a new base path (`v2/`), not a new number here |
| `generated` | UTC time, `YYYY-MM-DDTHH:MM:SSZ` | When this manifest was built |
| `wafs_run`, `gfs_run` | `"YYYYMMDDHH"` or `null` | The source cycles (UTC). `null` = that source failed and the feed is degraded: without `wafs_run` every step is `outlook`; without `gfs_run` there are no steps past 48 hours |
| `grid` | object | As above; constant in v1 |
| `steps` | array, rising `valid` | `valid` (UTC time the step is for), `kind` (`wafs` or `outlook`), `file`, `layers` (the order inside the file), `bytes` (gzipped size) |
| `thresholds` | object | Lower bounds on the byte value: `light ≤ v < moderate` is light, and so on; under `light` is smooth. Read them from here so they can be tuned without an app release |
| `min_build` | int | The oldest app build that can read this feed. An older build should ask for an update instead of showing a forecast **Keep it ≤ 3 until a build after iOS 1.0.1 (4) is live:** that build reports its build as 3 internally (`AppIdentity.build`, fixed on the app's main 2026-11-24), so a higher value would show its users an "update the app" banner. |
| `notice` | object, optional | A service message: language code → text, `en` always present |
| `problems` | array, optional | For operators: which source failed and why. Clients can ignore it |

Unknown fields may be added; ignore them.

## Picking a step for a moment in time

Use the step whose `valid` is nearest, or interpolate between the two around it. Steps before the first or after the last `valid` do not exist: beyond the last one there is no forecast yet. Past steps stay in the manifest until the next run replaces it (a new run arrives about every 6 hours).

## Caching

- **`manifest.json`**: GitHub Pages sends `Cache-Control: max-age=600` and an `ETag`. Ask at most about once an hour, with `If-None-Match`. New data arrives four times a day, roughly 5 hours after 00, 06, 12 and 18 UTC.
- **Step files** never change at a given path: cache them on disk keyed by `file`, for offline use in the air. Delete cached files that the current manifest no longer lists.
- The site keeps the files of the two newest builds, so a manifest up to about 6 hours old still resolves. On a 404 for a step file, fetch the manifest again.
- Download only the steps a flight needs (usually 2 to 5 files, 0.1 MB each).

## Accuracy notes

- WAFS values are decoded from the colours of AWC's public WAFS display images (2 EDR-units steps above 20; 15, 17, 18, 20 below). The legend gives labels at 10, 20, 40, 60, 80 and 100; values between are interpolated, so absolute values may be off by 1 or 2 units.
- The `cb` scale is an assumption: the display has no legend for it. The lightest grey is taken as 30 % and the darkest as 100 %. The ordering is certain; use `cb` as "storms likely here", not as an exact percentage.
- The outlook has the same area share of each intensity as WAFS, by construction. Where exactly the areas fall is less certain: against WAFS on 2026-11-22 the ranking score (AUC) was 0.81–0.88 for light-or-more and 0.91–0.96 for EDR ≥ 0.30.
