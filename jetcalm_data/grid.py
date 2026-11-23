"""The output grid and the Web-Mercator mapping of AWC's WAFS images."""
import math

import numpy as np

ROWS, COLS = 301, 720
LATS = 75.0 - 0.5 * np.arange(ROWS)        # 75 .. -75
LONS = -180.0 + 0.5 * np.arange(COLS)      # -180 .. 179.5
NODATA = 255

# AWC image geometry (900x600 Web-Mercator), measured in the 2026-11-22 proof.
IMG_W, IMG_H = 900, 600
IMG_LAT_MAX = 75.940536
IMG_LON_MAX = 179.983902


def _merc(lat_deg):
    return np.log(np.tan(np.pi / 4 + np.radians(lat_deg) / 2))


_YMAX = math.log(math.tan(math.pi / 4 + math.radians(IMG_LAT_MAX) / 2))


def mercator_rows(lats=LATS, height=IMG_H):
    """Image row (0 = north edge) holding each latitude."""
    lats = np.asarray(lats, dtype=float)
    return ((_YMAX - _merc(lats)) / (2 * _YMAX) * height).astype(int).clip(0, height - 1)


def mercator_cols(lons=LONS, width=IMG_W):
    lons = np.asarray(lons, dtype=float)
    return ((lons + IMG_LON_MAX) / (2 * IMG_LON_MAX) * width).astype(int).clip(0, width - 1)


def regrid_image(img):
    """600x900 Web-Mercator array -> 301x720 lat/lon array, nearest pixel."""
    if img.shape != (IMG_H, IMG_W):
        raise ValueError(f"expected {(IMG_H, IMG_W)}, got {img.shape}")
    return img[np.ix_(mercator_rows(), mercator_cols())]
