"""Quantile mapping from Ellrod TI1 to WAFS EDR x 100.

A mapping is a step function: edr[i] applies where ti1 >= ti1[i]. Both lists
rise, so the mapping is monotone. Values below the first threshold map to 0
(WAFS draws nothing under EDR 0.15).
"""
import json

import numpy as np

# hPa level of the TI1 field -> flight level of the WAFS field it is matched to.
LEVEL_PAIRS = {200: 390, 250: 340, 300: 300}


def fit(ti1, edr):
    """Match the distribution of ti1 to edr (same number of samples not needed).

    The k-th largest share of TI1 values gets the k-th largest EDR values:
    for each EDR value e, the threshold is the TI1 quantile at
    1 - fraction(edr >= e).
    """
    ti1 = np.asarray(ti1, float).ravel()
    edr = np.asarray(edr).ravel()
    edr = edr[edr != 255]
    if ti1.size == 0 or edr.size == 0:
        raise ValueError("empty calibration sample")
    values = np.unique(edr[edr > 0])
    thresholds, out = [], []
    for e in values:
        frac = float((edr >= e).mean())
        t = float(np.quantile(ti1, 1.0 - frac))
        if thresholds and t <= thresholds[-1]:
            out[-1] = int(e)            # same threshold: keep the larger value
            continue
        thresholds.append(t)
        out.append(int(e))
    return {"ti1": thresholds, "edr": out}


def apply(mapping, ti1):
    """TI1 array -> uint8 EDR x 100."""
    th = np.asarray(mapping["ti1"], float)
    vals = np.concatenate([[0], np.asarray(mapping["edr"], int)]).astype(np.uint8)
    return vals[np.searchsorted(th, np.asarray(ti1, float), side="right")]


def validate(cal):
    """Raise ValueError unless cal is a usable calibration document."""
    levels = cal.get("levels", {})
    for p in LEVEL_PAIRS:
        m = levels.get(str(p))
        if not m or not m["ti1"] or len(m["ti1"]) != len(m["edr"]):
            raise ValueError(f"calibration has no usable mapping for {p} hPa")
        if any(b <= a for a, b in zip(m["ti1"], m["ti1"][1:])) or any(
                b <= a for a, b in zip(m["edr"], m["edr"][1:])):
            raise ValueError(f"calibration for {p} hPa is not strictly increasing")
        if m["edr"][0] < 1 or m["edr"][-1] > 254 or m["ti1"][0] <= 0:
            raise ValueError(f"calibration for {p} hPa is out of range")
    return cal


def load(path):
    with open(path) as f:
        return validate(json.load(f))


def dump(cal, path):
    with open(path, "w") as f:
        json.dump(cal, f, separators=(",", ":"))
        f.write("\n")
