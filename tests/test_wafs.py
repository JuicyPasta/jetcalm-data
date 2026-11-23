import gzip
import io

import numpy as np
import pytest
from PIL import Image

from conftest import FIXTURES
from jetcalm_data import grid, tiles, wafs

AWC = FIXTURES / "awc"


def png(name):
    return (AWC / f"20261122_12_F12_wafs_{name}_m.png").read_bytes()


def to_png(arr_rgba):
    buf = io.BytesIO()
    Image.fromarray(arr_rgba, "RGBA").save(buf, "PNG")
    return buf.getvalue()


def blank(h=600, w=900):
    a = np.zeros((h, w, 4), np.uint8)
    a[..., :3] = 255
    return a


def test_palette_decode_matches_stored_array():
    got = wafs.decode_png(png("301_edr"))
    want = np.frombuffer(gzip.decompress((FIXTURES / "decoded_20261122_12_F12_301.u8.gz").read_bytes()),
                         np.uint8).reshape(600, 900)
    assert got.dtype == np.uint8 and np.array_equal(got, want)
    # the values the 2026-11-22 proof read off this image (proof_output.txt)
    assert sorted(np.unique(got)) == [0, 15, 17, 18, 20, 22, 24, 26, 28, 30, 32, 34, 36, 38, 40, 42]


def test_anchor_colours_decode_exactly():
    for rgb, edr in wafs.EDR_RAMP:
        assert wafs.colour_to_value(rgb, wafs.EDR_RAMP) == edr
    assert wafs.colour_to_value((255, 255, 255), wafs.EDR_RAMP) == 0
    assert wafs.colour_to_value((204, 204, 204), wafs.CB_RAMP) == 30
    assert wafs.colour_to_value((102, 102, 102), wafs.CB_RAMP) == 100
    assert wafs.colour_to_value((153, 153, 153), wafs.CB_RAMP) == 65


def test_transparent_and_white_are_zero():
    a = blank()
    a[:300, :, 3] = 255                 # opaque white on top, transparent below
    assert not wafs.decode_png(to_png(a)).any()


@pytest.mark.parametrize("colour", [(0, 0, 255), (0, 128, 0), (120, 120, 200), (0, 0, 0)])
def test_unknown_colour_fails(colour):
    a = blank()
    a[10, 10] = (*colour, 255)
    with pytest.raises(wafs.WafsError, match="palette"):
        wafs.decode_png(to_png(a))


def test_grey_is_not_a_turbulence_colour_and_yellow_is_not_a_cb_colour():
    a = blank()
    a[5, 5] = (153, 153, 153, 255)
    with pytest.raises(wafs.WafsError):
        wafs.decode_png(to_png(a), wafs.EDR_RAMP)
    a[5, 5] = (255, 204, 0, 255)
    with pytest.raises(wafs.WafsError):
        wafs.decode_png(to_png(a), wafs.CB_RAMP)


def test_colour_just_inside_and_outside_the_tolerance():
    assert wafs.colour_to_value((255, 204, 29), wafs.EDR_RAMP) == 30
    assert wafs.colour_to_value((255, 204, 31), wafs.EDR_RAMP) is None


def test_wrong_size_fails():
    with pytest.raises(wafs.WafsError, match="size"):
        wafs.decode_png(to_png(blank(600, 1200)))
    with pytest.raises(wafs.WafsError, match="readable"):
        wafs.decode_png(b"<html>not found</html>")


def test_mercator_rows():
    rows = grid.mercator_rows([0, 45, -45, 75, -75])
    assert rows.tolist() == [300, 173, 426, 9, 590]
    assert grid.mercator_rows([grid.IMG_LAT_MAX, -grid.IMG_LAT_MAX, 89]).tolist() == [0, 599, 0]
    r = grid.mercator_rows()
    assert len(r) == 301 and np.all(np.diff(r) >= 0)
    # symmetric about the equator (row 300 is the first row south of it)
    assert np.all(r + r[::-1] >= 599) and np.all(r + r[::-1] <= 600)


def test_mercator_cols_and_regrid():
    assert grid.mercator_cols([-180, 0, 179.5]).tolist() == [0, 450, 898]
    img = np.zeros((600, 900), np.uint8)
    img[300:, 450:] = 7                  # south-east quadrant
    out = grid.regrid_image(img)
    assert out.shape == (301, 720)
    assert out[151, 360] == 7 and out[149, 360] == 0 and out[151, 359] == 0
    with pytest.raises(ValueError):
        grid.regrid_image(np.zeros((10, 10)))


def test_regression_against_prototype_tile_2026112212_f12():
    """The production decode reproduces the proof's published tile byte for byte."""
    proto = gzip.decompress((FIXTURES / "prototype_wafs_2026112212_f12.bin.gz").read_bytes())
    cruise, low = np.frombuffer(proto, np.uint8).reshape(2, 301, 720)
    edr = {fl: grid.regrid_image(wafs.decode_png(png(code))) for fl, code in wafs.LEVELS.items()}
    cb = grid.regrid_image(wafs.decode_png(png("whole_cbhe"), wafs.CB_RAMP))
    layers = wafs.layers_from_levels(edr, cb)
    assert np.array_equal(layers["cruise_max"], cruise)
    assert np.array_equal(layers["low_max"], low)
    assert np.all(layers["cruise_mid"] <= layers["cruise_max"])
    assert set(np.unique(layers["cb"])) <= {0, *range(30, 101)}
    # numbers recorded by the proof for this file
    assert round(float((cruise >= 15).mean()), 2) == round(float((layers["cruise_max"] >= 15).mean()), 2)
    blob = tiles.encode(layers, tiles.LAYERS_WAFS)
    back = tiles.decode(blob, tiles.LAYERS_WAFS)
    assert all(np.array_equal(back[k], layers[k]) for k in layers)
    assert 60_000 < len(blob) < 200_000


def test_candidate_runs_and_urls():
    from datetime import datetime, timezone
    now = datetime(2026, 11, 23, 2, 50, tzinfo=timezone.utc)
    assert wafs.candidate_runs(now) == ["2026112300", "2026112218", "2026112212", "2026112206"]
    assert wafs.url("2026112212", 6, "250_edr") == (
        "https://aviationweather.gov/data/products/wafs/20261122/12/20261122_12_F06_wafs_250_edr_m.png")
    assert len(wafs.STEPS) == 15 and wafs.STEPS[0] == 6 and wafs.STEPS[-1] == 48
