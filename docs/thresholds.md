# Level thresholds: checked against primary sources (2026-11-24)

The feed's `thresholds` (in `manifest.json`, set in `jetcalm_data/tiles.py`) turn the forecast value, EDR × 100 (eddy dissipation rate, m^(2/3) s^-1, the WAFS turbulence unit), into the app's five levels:

| Level | EDR (feed byte ÷ 100) |
|---|---|
| Calm | below 0.15 (the WAFS images draw nothing under 0.15) |
| Light | 0.15 – 0.21 |
| Moderate | 0.22 – 0.33 |
| Strong | 0.34 – 0.44 |
| Severe | 0.45 and up |

## What the primary sources say

**1. ICAO Annex 3, Appendix 4, as amended by Amendment 79 (applicable November 2020).** The Annex itself is sold by ICAO; the text below is quoted verbatim in ICAO's own WAFS training material ("New WAFS Hazard data training material", November 2020, slide "Turbulence Severity"), https://www.icao.int/sites/default/files/METP/Documents/Nov-2020-New-WAFS-Hazard-data-training-material.pdf

> "Turbulence shall be considered: a) Severe when the peak value of EDR equals or exceeds 0.45; b) moderate when the peak value is equal to or above 0.20 and below 0.45; c) Light when the peak value is above 0.10 and below 0.20; and d) nil when the peak value is below or equal to 0.10."
>
> "Note: The EDR values given in Annex 3 describe the severity levels for a medium-sized transport aircraft under typical en-route conditions (i.e. altitude, airspeed and weight)."

The same deck says the WAFS turbulence severity grid is a single deterministic GTG field "provided as an Eddy Dissipation Rate".

**2. NOAA/NWS Aviation Weather Center, Graphical Forecasts for Aviation help, Turbulence section**, https://aviationweather.gov/gfa/help/

> "The Graphical Turbulence Guidance (GTG) grid is expressed in terms of Eddy Dissipation Rate (EDR) to the 1/3 power"

Its table "Aircraft Weight Class (Max Takeoff Weight) | Estimated EDR Threshold * 100":

| Weight class | Light | Moderate | Severe | Extreme |
|---|---|---|---|---|
| Light (< 15,500 lb) | 13 | 16 | 36 | 64 |
| **Medium (15,500–300,000 lb; A320, B737)** | **15** | **20** | **44** | **79** |
| Heavy (> 300,000 lb) | 17 | 24 | 54 | 96 |

**3. AWC WAFS page**, https://aviationweather.gov/wafs/ (the images this pipeline decodes): legend "Turbulence EDR (m 2/3 s -1 x 100)", labels 10 to 100. The feed's byte scale is the same unit.

**4. NCAR (the GTG developers), where 0.15 / 0.22 / 0.34 come from.** Pearson and Deierling, "WTIC industry day: GTGN", slide "PIREPs & Insitu EDR Comparisons", https://ral.ucar.edu/sites/default/files/docs//3-2-wtic-industry-day-gtgn.pdf

> "Based on 60,000 UAL757 matches, 50,000 DAL737 matches 'Light'=0.15 m2/3s-1 'Moderate'=0.22 m2/3s-1 'Severe'=0.34 m2/3s-1 These are lower than ICAO 'standard' thresholds"

and Sharman (2023), slide 13, https://cpaess.ucar.edu/sites/default/files/2023-08/Sharman-Bob.pdf: "For reference ICAO standard thresholds (2001) for mid-sized aircraft are EDR=0.10, 0.3, 0.5 for 'light', 'moderate', 'severe', resp. – We use EDR=0.15,0.22,0.34". The underlying papers (Sharman et al. 2014, JAMC 53(6), doi:10.1175/JAMC-D-13-0329.1; Sharman and Pearson 2017, JAMC 56(2), doi:10.1175/JAMC-D-16-0205.1) were not readable without a bot check, so they are cited, not quoted.

## Verdict

| Boundary | Jetcalm | ICAO Annex 3 (2020) | AWC, medium aircraft | NCAR GTG | Agrees? |
|---|---|---|---|---|---|
| Light starts | 0.15 | above 0.10 | 0.15 | 0.15 | Yes with AWC and NCAR. ICAO's 0.10–0.15 can't be shown: the WAFS images draw nothing under 0.15 |
| Moderate starts | **0.22** | **0.20** | **0.20** | 0.22 | **No** with ICAO and AWC (0.02 higher); yes with NCAR |
| Strong starts | 0.34 | (no such level) | (no such level) | 0.34 (their "severe") | An in-between level; matches NCAR's severe |
| Severe starts | 0.45 | 0.45 | 0.44 | 0.34 | Yes with ICAO; within 0.01 of AWC |

**Overall:** the thresholds are grounded, but they mix two schemes: NCAR's light and moderate (0.15, 0.22) with ICAO's severe (0.45), and NCAR's severe as "Strong". Light and Severe agree with the official sources. **Moderate is the one material difference: ICAO Annex 3 and AWC both start moderate at 0.20; Jetcalm starts it at 0.22.** In the decoded WAFS images 0.20 is a common value (one of the drawn steps: 15, 17, 18, 20, then every 2), so the choice moves whole areas between Light and Moderate.

The first pilot-report check (`docs/verification.md`, 2026-11-24, 455 reports) points the same way: where the forecast was exactly 0.20, 43 % of reports were moderate (21 of 49), close to the 51 % at 0.22–0.25 (38 of 74) and well above the 25 % at 0.15–0.19 (30 of 120).

**Recommended change (not applied; the thresholds are Jackson's and the app's call):** set `"moderate": 20` in `jetcalm_data/tiles.py` `THRESHOLDS` (the manifest carries it, so no app release is needed; also update the app's built-in default in `lib/forecast/levels.dart`, the tests that pin 22, `FORMAT.md` and `Jetcalm/SPEC.md`). Confirm with two weeks of verification data first: the 2026-11-24 sample is one night, mostly US domestic flights, and pilot reports over-represent bumpy flights. Keep Strong at 0.34 and Severe at 0.45; the app's plain-language level descriptions describe a medium-sized airliner, which is what these values mean.
