"""GFS 0.5 degree winds and heights by HTTP byte range, and the Ellrod TI1 index."""
import os
import tempfile

import numpy as np

from . import grid, http

BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{day}/{hh}/atmos/gfs.t{hh}z.pgrb2.0p50.f{fh:03d}"
PRESSURES = (150, 200, 250, 300, 350, 400)
VARS = ("UGRD", "VGRD", "HGT")
TI_LEVELS = (200, 250, 300)             # hPa; about FL390, FL340, FL300
EARTH_RADIUS = 6.371e6
NI, NJ = 720, 361                       # 0.5 degree global grid, 90N -> 90S, 0E -> 359.5E


class GfsError(Exception):
    pass


def url(run, fh):
    return BASE.format(day=run[:8], hh=run[8:], fh=fh)


def parse_idx(text, wanted_vars=VARS, pressures=PRESSURES):
    """.idx text -> merged [(start, end)] byte ranges for the wanted records."""
    lines = [l.split(":") for l in text.splitlines() if l.strip()]
    wanted = {(v, f"{p} mb") for v in wanted_vars for p in pressures}
    ranges, found = [], set()
    for i, rec in enumerate(lines):
        if (rec[3], rec[4]) in wanted:
            start = int(rec[1])
            end = int(lines[i + 1][1]) - 1 if i + 1 < len(lines) else None
            found.add((rec[3], rec[4]))
            if ranges and ranges[-1][1] is not None and ranges[-1][1] + 1 == start:
                ranges[-1] = (ranges[-1][0], end)
            else:
                ranges.append((start, end))
    if found != wanted:
        raise GfsError(f"idx is missing {sorted(wanted - found)}")
    return ranges


def exists(run, fh, fetch=http.get):
    try:
        fetch(url(run, fh) + ".idx", retries=2)
        return True
    except (http.NotFound, IOError):
        return False


def decode_grib(data):
    """GRIB2 bytes -> {(var, hPa): 361x720 float array}."""
    import eccodes as ec

    names = {"u": "UGRD", "v": "VGRD", "gh": "HGT"}
    fields = {}
    fd, path = tempfile.mkstemp(suffix=".grib2")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        with open(path, "rb") as f:
            while True:
                h = ec.codes_grib_new_from_file(f)
                if h is None:
                    break
                try:
                    short = ec.codes_get(h, "shortName")
                    if short not in names or ec.codes_get(h, "typeOfLevel") != "isobaricInhPa":
                        continue
                    ni, nj = ec.codes_get(h, "Ni"), ec.codes_get(h, "Nj")
                    if (ni, nj) != (NI, NJ):
                        raise GfsError(f"unexpected grid {ni}x{nj}")
                    if ec.codes_get(h, "jScansPositively") or not ec.codes_get(h, "iScansPositively"):
                        raise GfsError("unexpected scan order")
                    lat0 = ec.codes_get(h, "latitudeOfFirstGridPointInDegrees")
                    lon0 = ec.codes_get(h, "longitudeOfFirstGridPointInDegrees")
                    if abs(lat0 - 90) > 1e-6 or abs(lon0) > 1e-6:
                        raise GfsError(f"unexpected grid origin {lat0},{lon0}")
                    fields[(names[short], int(ec.codes_get(h, "level")))] = (
                        ec.codes_get_values(h).reshape(nj, ni))
                finally:
                    ec.codes_release(h)
    finally:
        os.unlink(path)
    return fields


def fetch_fields(run, fh, fetch=http.get):
    """Download only U, V and height at the six levels (about 4 MB of a 150 MB file)."""
    u = url(run, fh)
    try:
        idx = fetch(u + ".idx").decode()
        data = b"".join(fetch(u, byte_range=r, timeout=120) for r in parse_idx(idx))
    except (http.NotFound, IOError) as e:
        raise GfsError(f"GFS {run} f{fh:03d}: {e}") from None
    fields = decode_grib(data)
    missing = [(v, p) for v in VARS for p in PRESSURES if (v, p) not in fields]
    if missing:
        raise GfsError(f"GFS {run} f{fh:03d}: missing fields {missing}")
    return fields


def ellrod_ti1(u, v, u_below, v_below, u_above, v_above, dz, lats, dlon_deg):
    """Ellrod-Knapp TI1 = vertical wind shear x horizontal deformation (s^-2).

    u, v: wind at the level (rows = latitude north -> south, columns = longitude,
    periodic). *_below / *_above: wind one level below and above; dz: thickness
    between them in metres. lats: row latitudes in degrees (evenly spaced).
    """
    lats = np.asarray(lats, float)
    dlat = np.radians(abs(lats[1] - lats[0]))
    dlon = np.radians(dlon_deg)
    cos = np.cos(np.radians(lats))[:, None].clip(0.05)

    def ddx(a):
        return (np.roll(a, -1, 1) - np.roll(a, 1, 1)) / (2 * EARTH_RADIUS * cos * dlon)

    def ddy(a):                         # rows run north -> south
        return -np.gradient(a, axis=0) / (EARTH_RADIUS * dlat)

    stretching = ddx(u) - ddy(v)
    shearing = ddx(v) + ddy(u)
    deformation = np.hypot(stretching, shearing)
    vws = np.hypot(u_above - u_below, v_above - v_below) / np.maximum(dz, 1.0)
    return vws * deformation


def smooth3(a):
    """3x3 box mean: periodic in longitude, edge rows repeated in latitude."""
    padded = np.concatenate([a[:1], a, a[-1:]], axis=0)
    rows = padded[:-2] + padded[1:-1] + padded[2:]
    return (np.roll(rows, 1, 1) + rows + np.roll(rows, -1, 1)) / 9.0


def to_output_grid(a):
    """361x720 (90..-90, 0..359.5) -> 301x720 (75..-75, -180..179.5)."""
    return np.roll(a[30:331], NI // 2, axis=1)


def ti1_levels(fields):
    """{hPa: smoothed TI1 on the output grid} for 200, 250 and 300 hPa.

    The 3x3 mean removes grid-scale speckle; against WAFS it lifts the AUC for
    EDR >= 0.15 from 0.76-0.83 to 0.81-0.88 (2026-11-22 18Z, F24).
    """
    lats = np.linspace(90, -90, NJ)
    out = {}
    for p in TI_LEVELS:
        lo, hi = p + 50, p - 50         # the level below (higher pressure) and above
        ti = ellrod_ti1(fields[("UGRD", p)], fields[("VGRD", p)],
                        fields[("UGRD", lo)], fields[("VGRD", lo)],
                        fields[("UGRD", hi)], fields[("VGRD", hi)],
                        fields[("HGT", hi)] - fields[("HGT", lo)], lats, 0.5)
        out[p] = to_output_grid(smooth3(ti))
    return out


assert grid.ROWS == 301 and grid.COLS == NI
