"""Official WAFS turbulence, decoded from AWC's public display images.

The image path is not a documented API. Anything unexpected (size, colour)
raises WafsError so the run falls back to the GFS-derived feed instead of
publishing guessed values.
"""
import io
from datetime import datetime, timedelta, timezone

import numpy as np
from PIL import Image

from . import grid, http

BASE = "https://aviationweather.gov/data/products/wafs/{day}/{hh}/{day}_{hh}_F{fh:02d}_wafs_{product}_m.png"

# Flight level -> product code in the image file name (pressure in hPa).
LEVELS = {140: "595_edr", 180: "506_edr", 240: "393_edr", 270: "344_edr",
          300: "301_edr", 340: "250_edr", 390: "197_edr"}
CRUISE_MID = 340
CRUISE = (300, 340, 390)
LOW = (140, 180, 240, 270)
CB = "whole_cbhe"
STEPS = tuple(range(6, 49, 3))          # F06 .. F48

# EDR x 100 -> RGB anchors, read from the legend on aviationweather.gov/wafs.
EDR_RAMP = [((204, 255, 153), 15), ((204, 255, 0), 20), ((255, 204, 0), 30), ((255, 153, 0), 40),
            ((255, 102, 0), 50), ((255, 0, 0), 60), ((204, 0, 0), 70), ((153, 0, 0), 80),
            ((102, 0, 0), 90), ((77, 0, 0), 100)]
# Cumulonimbus horizontal extent: a grey ramp, lightest = least cover. The page
# has no legend for it; the ends are taken as 30 % and 100 % (see FORMAT.md).
CB_RAMP = [((204, 204, 204), 30), ((102, 102, 102), 100)]
MAX_COLOUR_DISTANCE = 30.0


class WafsError(Exception):
    pass


def colour_to_value(rgb, ramp):
    """Project one RGB colour onto a piecewise-linear ramp.

    White -> 0. Returns None when the colour is farther than
    MAX_COLOUR_DISTANCE from every segment.
    """
    if tuple(int(c) for c in rgb) == (255, 255, 255):
        return 0
    rgb = np.array(rgb, float)
    best = None
    for (c0, e0), (c1, e1) in zip(ramp, ramp[1:]):
        c0 = np.array(c0, float)
        d = np.array(c1, float) - c0
        t = min(1.0, max(0.0, float(np.dot(rgb - c0, d) / np.dot(d, d))))
        err = float(np.sum((c0 + t * d - rgb) ** 2))
        if best is None or err < best[0]:
            best = (err, e0 + t * (e1 - e0))
    if best[0] >= MAX_COLOUR_DISTANCE ** 2:
        return None
    return int(round(best[1]))


def decode_png(data, ramp=EDR_RAMP, name="image"):
    """PNG bytes -> 600x900 uint8 array of ramp values (0 = nothing drawn)."""
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as e:
        raise WafsError(f"{name}: not a readable image: {e}") from None
    if im.size != (grid.IMG_W, grid.IMG_H):
        raise WafsError(f"{name}: size {im.size}, expected {(grid.IMG_W, grid.IMG_H)}")
    a = np.asarray(im.convert("RGBA"))
    rgb = a[..., :3].astype(np.uint32)
    packed = (rgb[..., 0] << 16) | (rgb[..., 1] << 8) | rgb[..., 2]
    packed[a[..., 3] == 0] = 0xFFFFFF                      # transparent -> white
    if np.any((a[..., 3] != 0) & (a[..., 3] != 255)):
        raise WafsError(f"{name}: partly transparent pixels")
    uniq, inverse = np.unique(packed, return_inverse=True)
    values = np.empty(len(uniq), np.uint8)
    for i, p in enumerate(uniq):
        c = (int(p) >> 16, (int(p) >> 8) & 255, int(p) & 255)
        v = colour_to_value(c, ramp)
        if v is None:
            raise WafsError(f"{name}: colour {c} is not on the expected palette")
        values[i] = v
    return values[inverse.reshape(packed.shape)]


def url(run, fh, product):
    return BASE.format(day=run[:8], hh=run[8:], fh=fh, product=product)


def candidate_runs(now, count=4):
    """The last `count` 6-hourly cycles at or before `now`, newest first."""
    t = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    t -= timedelta(hours=t.hour % 6)
    return [(t - timedelta(hours=6 * i)).strftime("%Y%m%d%H") for i in range(count)]


def find_newest_run(now, fetch=http.get):
    """Newest cycle whose first and last files both exist, else None."""
    for run in candidate_runs(now):
        try:
            fetch(url(run, STEPS[0], LEVELS[CRUISE_MID]))
            fetch(url(run, STEPS[-1], CB))
            return run
        except http.NotFound:
            continue
        except IOError:
            continue
    return None


def layers_from_levels(edr, cb):
    """{flight level: 301x720 array}, cb array -> the four WAFS layers."""
    return {
        "cruise_mid": edr[CRUISE_MID],
        "cruise_max": np.max([edr[fl] for fl in CRUISE], axis=0),
        "low_max": np.max([edr[fl] for fl in LOW], axis=0),
        "cb": cb,
    }


def fetch_step(run, fh, fetch=http.get):
    """One forecast hour -> ({flight level: grid}, cb grid). Raises WafsError."""
    products = list(LEVELS.items()) + [("cb", CB)]

    def one(item):
        key, product = item
        u = url(run, fh, product)
        try:
            data = fetch(u)
        except (IOError, http.NotFound) as e:
            raise WafsError(f"fetch failed: {e}") from None
        ramp = CB_RAMP if key == "cb" else EDR_RAMP
        return key, grid.regrid_image(decode_png(data, ramp, name=u.rsplit("/", 1)[-1]))

    out = dict(one(p) for p in products)     # one at a time, on one kept-alive connection
    cb = out.pop("cb")
    return out, cb


def valid_time(run, fh):
    return datetime.strptime(run, "%Y%m%d%H").replace(tzinfo=timezone.utc) + timedelta(hours=fh)
