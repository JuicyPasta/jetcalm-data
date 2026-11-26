# Forecast verification against pilot reports

Does the forecast match what pilots actually felt? `python -m jetcalm_data.verify` (workflow [`verify`](../.github/workflows/verify.yml), every 6 hours) checks the published feed against pilot reports and keeps the results in [`verification/`](../verification/):

- `verification/latest.json`: the scores for the last 7 days, the last 30 days, all time and each day.
- `verification/matches/YYYY-MM-DD.jsonl`: every matched report (UTC day of the report) with the forecast values it was scored against, so any score can be recomputed later, for example with other thresholds.

## Method

1. **Reports.** PIREPs and AIREPs from the NOAA/NWS Aviation Weather Center Data API (`https://aviationweather.gov/api/data/pirep`, public domain), the last 14 hours on each run: every report with a turbulence group worldwide, plus every report over North America so that "smooth" reports are included. Requests go one at a time, 1.5 s apart, in windows small enough to stay under the API's 400-result cap (about 25 requests a run).
2. **Kept:** reports with a turbulence intensity and a flight level from FL130 to FL450. The worst intensity in the report counts. Reported intensity → category: NEG/SMOOTH → none; SMTH-LGT, LGT → light; LGT-MOD → light-moderate; MOD → moderate; MOD-SEV, SEV, SEV-EXTM, EXTM → severe.
3. **Matched to what the app would have shown:** the published step whose `valid` time is nearest the report (within 90 minutes; WAFS steps before outlook steps; then the shortest lead, i.e. the newest run), at the nearest 0.5° cell. At FL285 and above (cruise) "expected" is `cruise_mid` at that cell and "could reach" is the 3×3 maximum of `cruise_max`; from FL130 to FL284 both read `low_max` (the app's climb and descent). Values are turned into levels with the manifest's thresholds ([thresholds.md](thresholds.md)).
4. **Scores** (all on the *expected* level unless said otherwise):
   - **Forecast agreed:** calm ↔ none; light ↔ light or light-moderate; moderate ↔ light-moderate or moderate; strong ↔ moderate or severe (0.34–0.44 is severe on NCAR's scale and moderate on ICAO's); severe ↔ severe.
   - **Within expected–could-reach:** the report agrees with some level between the expected and the could-reach level, which is the range the app shows.
   - **Moderate+ hit rate:** of the reports of moderate or worse, the share where the forecast was moderate or more (also with could reach).
   - **Smooth reports: forecast calm:** of the "smooth" reports, the share where the forecast was calm.
   - Confusion tables (forecast level × reported category) for expected and could reach are in `latest.json`.

## Limits (read before quoting a number)

- **Pilots mostly report bumps.** Smooth reports are rare, so false alarms can't be measured well; a high hit rate alone would be easy to get by forecasting turbulence everywhere. Look at the moderate+ hit rate and the smooth-report rate together.
- **Mostly the United States.** About 95 % of matched reports are over North America; AIREPs elsewhere rarely carry a turbulence group.
- **A report is a point; the forecast is a 55 km cell and a 3-hour step.** Exact agreement is a strict test. The could-reach view allows one cell of position error.
- **The forecast can't say "light below 0.15":** the WAFS images draw nothing under 0.15 EDR, so ICAO's light band 0.10–0.15 shows as calm.
- **Different aircraft feel the same air differently.** A light report from a heavy jet and from a small turboprop mean different EDR values (thresholds.md, AWC table).
- **History:** the site keeps only the two newest builds, about 12 hours of past steps, so reports can only be matched while their step is still published. The workflow runs every 6 hours to see them all and stores each match the first time it is made.

## Results by day

Rewritten on every run from `verification/matches/` (the newest 60 days).

<!-- daily-table:start -->
| Day (UTC) | Reports matched | Forecast agreed | Within expected–could-reach | Moderate+ reports: forecast moderate+ (expected / could reach) | Smooth reports: forecast calm |
|---|---|---|---|---|---|
| 2026-11-23 | 112 | 48% | 64% | 55% / 73% of 22 | 58% of 31 |
| 2026-11-24 | 1905 | 54% | 70% | 48% / 76% of 649 | 43% of 298 |
| 2026-11-25 | 2694 | 53% | 68% | 55% / 84% of 966 | 27% of 353 |
| 2026-11-26 | 472 | 58% | 76% | 54% / 86% of 217 | 30% of 53 |
| **last 30 days** | 5183 | 53% | 69% | 52% / 81% of 1854 | 35% of 735 |
<!-- daily-table:end -->

## Log

### 2026-11-24: first run

455 reports matched (reports from 2026-11-23 22:30 to 2026-11-24 10:15 UTC; 433 over North America; 372 at cruise, 83 in climb/descent levels; all against WAFS steps, median lead 6 h). No severe reports in the sample.

- Forecast agreed with the report: **50 %** (225 of 455); within the expected–could-reach range: **67 %**.
- Moderate+ reports (153): forecast moderate or more **47 %** on expected, **72 %** on could reach.
- Any turbulence reported (370): forecast light or more **72 %**.
- Smooth reports (85): forecast calm **49 %**, calm or light **86 %**.
- Share of moderate reports by forecast value: calm 21 % (30 of 146); 0.15–0.19 25 % (30 of 120); **0.20–0.21 43 % (21 of 49)**; 0.22–0.25 51 % (38 of 74); 0.26–0.33 52 % (29 of 56); 0.34–0.44 50 % (5 of 10). The step at 0.20 supports moving the moderate threshold from 0.22 to 0.20 (thresholds.md), pending more data.

Expected level (rows) × reported category (columns):

| Forecast \ reported | none | light | light-moderate | moderate | severe |
|---|---|---|---|---|---|
| calm | 42 | 59 | 15 | 30 | 0 |
| light | 31 | 59 | 28 | 51 | 0 |
| moderate | 11 | 28 | 24 | 67 | 0 |
| strong | 1 | 2 | 2 | 5 | 0 |
| severe | 0 | 0 | 0 | 0 | 0 |
