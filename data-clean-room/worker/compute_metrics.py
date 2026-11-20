#!/usr/bin/env python3
"""Runs INSIDE the worker Beam. Reads the raw (coded) people CSV and writes
org-level aggregates only. Standard library only, so it runs on a stock Beam.

Usage: python3 compute_metrics.py people_coded.csv metrics.json

Every metric is reported as {"value", "n", "status"}. Anything based on fewer
than MIN_GROUP people is suppressed (value = None, status = "suppressed").
Definitions and thresholds follow playbook.md section 2.
"""
import csv
import json
import statistics
import sys
from datetime import date, timedelta

REF = date(2026, 10, 1)          # reference date for "last 12 months"
YEAR = timedelta(days=365)
MIN_GROUP = 5
TOP = 4                          # 4-point scale, 4 = top performer
ORGS = ["Engineering", "Product", "Sales", "Marketing", "G&A"]

# (id, label, green test, yellow test, unit)  -- anything else is red
THRESHOLDS = {
    "R1": ("Voluntary attrition", lambda v: v < 10, lambda v: v <= 15, "%"),
    "R2": ("Regretted attrition", lambda v: v < 25, lambda v: v <= 40, "%"),
    "R3": ("First-year attrition", lambda v: v < 10, lambda v: v <= 20, "%"),
    "R4": ("High-performer stall", lambda v: v < 30, lambda v: v <= 50, "%"),
    "M1": ("Average span of control", lambda v: 5 <= v <= 8, lambda v: 3 <= v <= 10, ""),
    "M2": ("Very small teams", lambda v: v < 15, lambda v: v <= 25, "%"),
    "M3": ("Manager churn", lambda v: v < 20, lambda v: v <= 35, "%"),
    "P1": ("Rating skew vs company", lambda v: abs(v) <= 5, lambda v: abs(v) <= 10, " pts"),
    "P2": ("Promotion rate", lambda v: 8 <= v <= 15, lambda v: 5 <= v <= 20, "%"),
    "P3": ("Stuck in level", lambda v: v < 20, lambda v: v <= 30, "%"),
    "C1": ("Median band position", lambda v: 0.95 <= v <= 1.05, lambda v: 0.90 <= v <= 1.10, ""),
    "C2": ("Top performers paid low", lambda v: v < 15, lambda v: v <= 30, "%"),
    "C3": ("Pay compression", lambda v: v < 3, lambda v: v <= 8, " pts"),
    "C4": ("Out of band", lambda v: v < 3, lambda v: v <= 8, "%"),
}


def d(s):
    return date.fromisoformat(s) if s else None


def load(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            for k in ("hire_date", "term_date", "last_promo_date"):
                r[k] = d(r[k])
            r["rating_current"] = int(r["rating_current"]) if r["rating_current"] else None
            r["manager_changes_12m"] = int(r["manager_changes_12m"] or 0)
            r["regretted"] = r["regretted"].lower() in ("true", "t")  # CSV file or psql export
            for k in ("base_salary", "band_min", "band_mid", "band_max"):
                r[k] = float(r[k])
            r["pos"] = r["base_salary"] / r["band_mid"]
            rows.append(r)
    return rows


def pct(num, den):
    return round(100 * num / den, 1) if den else None


def metric(mid, value, n):
    label, green, yellow, unit = THRESHOLDS[mid]
    if n < MIN_GROUP or value is None:
        return {"label": label, "value": None, "n": n, "unit": unit, "status": "suppressed"}
    if value == 0:
        value = abs(value)  # avoid displaying -0.0
    status = "green" if green(value) else ("yellow" if yellow(value) else "red")
    return {"label": label, "value": value, "n": n, "unit": unit, "status": status}


def compute(rs, all_rows):
    active = [r for r in rs if r["term_date"] is None]
    left12 = [r for r in rs if r["term_date"] and r["term_date"] > REF - YEAR]
    vol = [r for r in left12 if r["term_type"] == "Voluntary"]
    avg_hc = len(active) + len(left12) / 2
    out = {"headcount": len(active)}

    out["R1"] = metric("R1", pct(len(vol), avg_hc), len(active))
    out["R2"] = metric("R2", pct(sum(r["regretted"] for r in vol), len(vol)), len(vol))
    hires = [r for r in rs if r["hire_date"] > REF - 2 * YEAR]
    first_yr = [r for r in hires if r["term_date"] and (r["term_date"] - r["hire_date"]) <= YEAR]
    out["R3"] = metric("R3", pct(len(first_yr), len(hires)), len(hires))
    top_tenured = [r for r in active if r["rating_current"] == TOP and r["hire_date"] < REF - 2 * YEAR]
    stalled = [r for r in top_tenured if not r["last_promo_date"] or r["last_promo_date"] < REF - 2 * YEAR]
    out["R4"] = metric("R4", pct(len(stalled), len(top_tenured)), len(top_tenured))

    spans = {}
    for r in active:
        if r["manager_code"]:
            spans[r["manager_code"]] = spans.get(r["manager_code"], 0) + 1
    s = list(spans.values())
    out["M1"] = metric("M1", round(statistics.mean(s), 1) if s else None, len(s))
    out["M2"] = metric("M2", pct(sum(1 for x in s if x <= 2), len(s)), len(s))
    out["M3"] = metric("M3", pct(sum(1 for r in active if r["manager_changes_12m"] > 0), len(active)), len(active))

    co_active = [r for r in all_rows if r["term_date"] is None]
    co_top = pct(sum(1 for r in co_active if r["rating_current"] == TOP), len(co_active))
    org_top = pct(sum(1 for r in active if r["rating_current"] == TOP), len(active))
    out["P1"] = metric("P1", round(org_top - co_top, 1) if org_top is not None else None, len(active))
    promos = [r for r in rs if r["last_promo_date"] and r["last_promo_date"] > REF - YEAR]
    out["P2"] = metric("P2", pct(len(promos), avg_hc), len(active))
    stuck = [r for r in active if (r["last_promo_date"] or r["hire_date"]) < REF - 3 * YEAR]
    out["P3"] = metric("P3", pct(len(stuck), len(active)), len(active))

    out["C1"] = metric("C1", round(statistics.median(r["pos"] for r in active), 3) if active else None, len(active))
    top = [r for r in active if r["rating_current"] == TOP]
    out["C2"] = metric("C2", pct(sum(1 for r in top if r["pos"] < 0.95), len(top)), len(top))
    # C3: compare new hires with tenured staff AT THE SAME LEVEL. For each level
    # that has both groups, take the gap in median band position, then average
    # the gaps weighted by the number of new hires at that level.
    gaps, n_new, n_old = [], 0, 0
    for lvl in sorted({r["level"] for r in active}):
        new = [r["pos"] for r in active if r["level"] == lvl and r["hire_date"] > REF - YEAR]
        old = [r["pos"] for r in active if r["level"] == lvl and r["hire_date"] < REF - 3 * YEAR]
        if new and old:
            gaps.append((statistics.median(new) - statistics.median(old), len(new)))
            n_new += len(new)
            n_old += len(old)
    comp = round(100 * sum(g * w for g, w in gaps) / sum(w for _, w in gaps), 1) if gaps else None
    out["C3"] = metric("C3", comp, min(n_new, n_old))
    oob = sum(1 for r in active if r["base_salary"] < r["band_min"] or r["base_salary"] > r["band_max"])
    out["C4"] = metric("C4", pct(oob, len(active)), len(active))
    return out


def main(src, dst):
    rows = load(src)
    result = {
        "reference_date": REF.isoformat(),
        "min_group": MIN_GROUP,
        "company": compute(rows, rows),
        "orgs": {org: compute([r for r in rows if r["org"] == org], rows) for org in ORGS},
    }
    with open(dst, "w") as f:
        json.dump(result, f, indent=2)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
