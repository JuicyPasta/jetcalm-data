import numpy as np
import pytest

from jetcalm_data import calibrate, gfs

IDX = """1:0:d=2026112218:PRMSL:mean sea level:54 hour fcst:
2:1000:d=2026112218:HGT:150 mb:54 hour fcst:
3:2000:d=2026112218:TMP:150 mb:54 hour fcst:
4:3000:d=2026112218:UGRD:150 mb:54 hour fcst:
5:4000:d=2026112218:VGRD:150 mb:54 hour fcst:
6:5000:d=2026112218:HGT:200 mb:54 hour fcst:
7:6000:d=2026112218:UGRD:200 mb:54 hour fcst:
8:7000:d=2026112218:VGRD:200 mb:54 hour fcst:
"""


def test_parse_idx_merges_adjacent_records_and_handles_the_last_one():
    r = gfs.parse_idx(IDX, pressures=(150, 200))
    assert r == [(1000, 1999), (3000, None)]
    assert gfs.parse_idx(IDX, wanted_vars=('UGRD', 'VGRD'), pressures=(150,)) == [(3000, 4999)]
    with pytest.raises(gfs.GfsError, match="missing"):
        gfs.parse_idx(IDX, pressures=(150, 200, 250))


def test_url():
    assert gfs.url("2026112218", 54) == (
        "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20261122/18/atmos/gfs.t18z.pgrb2.0p50.f054")


def _grid():
    lats = np.linspace(60, 20, 81)       # 0.5 degree rows, north -> south
    lon = np.arange(0, 360, 0.5)
    return lats, np.meshgrid(lon, lats)[1]


def test_ti1_pure_meridional_shear():
    """u = a*y, v = 0: deformation = |du/dy| = a, so TI1 = a * |dV| / dz."""
    lats, lat2d = _grid()
    a = 2e-5                                             # s^-1
    u = a * gfs.EARTH_RADIUS * np.radians(lat2d)
    z = np.zeros_like(u)
    ti = gfs.ellrod_ti1(u, z, u - 10.0, z, u + 10.0, z + 5.0, np.full_like(u, 2500.0), lats, 0.5)
    expected = a * np.hypot(20.0, 5.0) / 2500.0
    assert np.allclose(ti, expected, rtol=1e-9)


def test_ti1_is_zero_without_deformation_or_without_vertical_shear():
    lats, lat2d = _grid()
    const = np.full_like(lat2d, 30.0)
    z = np.zeros_like(const)
    dz = np.full_like(const, 2000.0)
    # uniform wind: strong vertical shear, no deformation
    assert np.allclose(gfs.ellrod_ti1(const, z, const - 20, z, const + 20, z, dz, lats, 0.5), 0, atol=1e-12)
    # deformation but identical wind above and below
    u = 1e-5 * gfs.EARTH_RADIUS * np.radians(lat2d)
    assert np.allclose(gfs.ellrod_ti1(u, z, u, z, u, z, dz, lats, 0.5), 0)


def test_ti1_stretching_deformation_in_longitude():
    """u = U0*sin(lon) on one latitude band: du/dx = U0*cos(lon)/(R cos(lat))."""
    lats = np.array([0.5, 0.0, -0.5])
    lon = np.radians(np.arange(0, 360, 0.5))
    u = np.tile(40.0 * np.sin(lon), (3, 1))
    z = np.zeros_like(u)
    ti = gfs.ellrod_ti1(u, z, z, z, z + 10.0, z, np.full_like(u, 1000.0), lats, 0.5)
    expected = np.abs(40.0 * np.cos(lon)) / gfs.EARTH_RADIUS * (10.0 / 1000.0)
    assert np.allclose(ti[1], expected, rtol=1e-4, atol=1e-12)


def test_to_output_grid_orientation():
    a = np.zeros((gfs.NJ, gfs.NI))
    a[30, 0] = 1        # 75N, 0E
    a[330, 360] = 2     # 75S, 180E = -180
    a[180, 719] = 3     # equator, 359.5E = -0.5
    out = gfs.to_output_grid(a)
    assert out.shape == (301, 720)
    assert out[0, 360] == 1 and out[300, 0] == 2 and out[150, 359] == 3


def test_quantile_mapping_is_monotone_and_matches_the_distribution():
    rng = np.random.default_rng(1)
    ti = rng.lognormal(-16, 1.0, 200_000)
    edr = np.zeros(150_000, np.uint8)
    edr[:30_000] = rng.integers(15, 20, 30_000)
    edr[30_000:36_000] = rng.integers(20, 45, 6_000)
    m = calibrate.fit(ti, edr)
    assert all(b > a for a, b in zip(m["ti1"], m["ti1"][1:]))
    assert all(b > a for a, b in zip(m["edr"], m["edr"][1:]))
    x = np.sort(rng.lognormal(-16, 1.2, 50_000))
    y = calibrate.apply(m, x)
    assert y.dtype == np.uint8 and np.all(np.diff(y.astype(int)) >= 0)
    mapped = calibrate.apply(m, ti)
    for thr in (15, 20, 30, 40):
        assert abs((mapped >= thr).mean() - (edr >= thr).mean()) < 0.002
    assert calibrate.apply(m, np.array([0.0, 1e-30]))[0] == 0
    assert calibrate.apply(m, np.array([1.0]))[0] == edr.max()


def test_quantile_mapping_ignores_nodata_and_rejects_empty():
    m = calibrate.fit(np.linspace(0, 1, 1000), np.array([0] * 90 + [20] * 10 + [255] * 50, np.uint8))
    assert m["edr"] == [20] and 0.89 < m["ti1"][0] < 0.91
    with pytest.raises(ValueError):
        calibrate.fit(np.array([]), np.array([15], np.uint8))


def test_committed_calibration_is_usable():
    from jetcalm_data.run import REPO
    cal = calibrate.load(REPO / "calibration.json")
    for p in calibrate.LEVEL_PAIRS:
        m = cal["levels"][str(p)]
        assert m["edr"][0] >= 15 and 1e-8 < m["ti1"][0] < 1e-5


def test_validate_rejects_bad_calibration():
    good = {"levels": {str(p): {"ti1": [1e-7, 2e-7], "edr": [15, 20]} for p in calibrate.LEVEL_PAIRS}}
    calibrate.validate(good)
    bad = {"levels": dict(good["levels"], **{"250": {"ti1": [2e-7, 1e-7], "edr": [15, 20]}})}
    with pytest.raises(ValueError):
        calibrate.validate(bad)
    with pytest.raises(ValueError):
        calibrate.validate({"levels": {}})


def test_smooth3_keeps_the_mean_and_wraps_in_longitude():
    a = np.zeros((5, 8))
    a[2, 0] = 9.0
    s = gfs.smooth3(a)
    assert np.isclose(s.sum(), 9.0) and np.isclose(s[2, 7], 1.0) and np.isclose(s[1, 1], 1.0) and s[4, 0] == 0
    assert np.allclose(gfs.smooth3(np.full((4, 6), 3.0)), 3.0)
