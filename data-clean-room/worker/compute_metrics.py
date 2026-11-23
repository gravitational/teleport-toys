#!/usr/bin/env python3
"""Runs INSIDE the worker Beam. Reads the raw (coded) people CSV and writes
org-level aggregates only. Standard library only, so it runs on a stock Beam.

Usage: python3 compute_metrics.py people_coded.csv metrics.json [YYYY-MM-DD]

The optional date is the reference date for "last 12 months" and similar
measures. It must be the first day of a quarter (default: the latest one).
Anything dated after it (hires, leavers, promotions) is ignored, so a run
describes the org as it was on that date.

Every metric is reported as {"value", "n", "status"}. Anything based on fewer
than MIN_GROUP people is suppressed (value = None, status = "suppressed").
Definitions and thresholds follow playbook.md section 2.
"""
import csv
import json
import statistics
import sys
from datetime import date, timedelta

REF = None                       # reference date; set in main()
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


def small(count):
    """True if `count` people is a group too small to describe (1 to MIN_GROUP-1).
    Zero is allowed: "nobody" doesn't single anyone out."""
    return 0 < count < MIN_GROUP


def metric(mid, value, n, groups=()):
    """n is the population the metric is based on. `groups` lists every other
    head count the value reveals (a numerator, its complement, a subgroup), and
    each must be 0 or at least MIN_GROUP, or the metric is suppressed. A
    suppressed metric doesn't release its n either."""
    label, green, yellow, unit = THRESHOLDS[mid]
    if n < MIN_GROUP or value is None or any(small(g) for g in groups):
        return {"label": label, "value": None, "n": None, "unit": unit, "status": "suppressed"}
    if value == 0:
        value = abs(value)  # avoid displaying -0.0
    status = "green" if green(value) else ("yellow" if yellow(value) else "red")
    return {"label": label, "value": value, "n": n, "unit": unit, "status": status}


def rate(mid, num, den):
    """A percentage of a population. The numerator and its complement are both
    head counts the value reveals, so both must pass the minimum."""
    return metric(mid, pct(num, den), den, groups=(num, den - num))


def compute(rs, all_rows):
    active = [r for r in rs if r["term_date"] is None]
    left12 = [r for r in rs if r["term_date"] and r["term_date"] > REF - YEAR]
    vol = [r for r in left12 if r["term_type"] == "Voluntary"]
    avg_hc = len(active) + len(left12) / 2
    out = {"headcount": len(active)}

    out["R1"] = metric("R1", pct(len(vol), avg_hc), len(active), groups=(len(vol),))
    out["R2"] = rate("R2", sum(r["regretted"] for r in vol), len(vol))
    hires = [r for r in rs if r["hire_date"] > REF - 2 * YEAR]
    first_yr = [r for r in hires if r["term_date"] and (r["term_date"] - r["hire_date"]) <= YEAR]
    out["R3"] = rate("R3", len(first_yr), len(hires))
    top_tenured = [r for r in active if r["rating_current"] == TOP and r["hire_date"] < REF - 2 * YEAR]
    stalled = [r for r in top_tenured if not r["last_promo_date"] or r["last_promo_date"] < REF - 2 * YEAR]
    out["R4"] = rate("R4", len(stalled), len(top_tenured))

    spans = {}
    for r in active:
        if r["manager_code"]:
            spans[r["manager_code"]] = spans.get(r["manager_code"], 0) + 1
    s = list(spans.values())
    out["M1"] = metric("M1", round(statistics.mean(s), 1) if s else None, len(s))
    out["M2"] = rate("M2", sum(1 for x in s if x <= 2), len(s))
    out["M3"] = rate("M3", sum(1 for r in active if r["manager_changes_12m"] > 0), len(active))

    co_active = [r for r in all_rows if r["term_date"] is None]
    co_top = pct(sum(1 for r in co_active if r["rating_current"] == TOP), len(co_active))
    n_top = sum(1 for r in active if r["rating_current"] == TOP)
    org_top = pct(n_top, len(active))
    out["P1"] = metric("P1", round(org_top - co_top, 1) if org_top is not None else None, len(active),
                       groups=(n_top, len(active) - n_top))
    promos = [r for r in rs if r["last_promo_date"] and r["last_promo_date"] > REF - YEAR]
    out["P2"] = metric("P2", pct(len(promos), avg_hc), len(active), groups=(len(promos),))
    stuck = [r for r in active if (r["last_promo_date"] or r["hire_date"]) < REF - 3 * YEAR]
    out["P3"] = rate("P3", len(stuck), len(active))

    out["C1"] = metric("C1", round(statistics.median(r["pos"] for r in active), 3) if active else None, len(active))
    top = [r for r in active if r["rating_current"] == TOP]
    out["C2"] = rate("C2", sum(1 for r in top if r["pos"] < 0.95), len(top))
    # C3: compare new hires with tenured staff AT THE SAME LEVEL. For each level
    # that has both groups, take the gap in median band position, then average
    # the gaps weighted by the number of new hires at that level. A level is
    # used only if BOTH groups have at least MIN_GROUP people, since each
    # median describes its group.
    gaps, n_new, n_old = [], 0, 0
    for lvl in sorted({r["level"] for r in active}):
        new = [r["pos"] for r in active if r["level"] == lvl and r["hire_date"] > REF - YEAR]
        old = [r["pos"] for r in active if r["level"] == lvl and r["hire_date"] < REF - 3 * YEAR]
        if len(new) >= MIN_GROUP and len(old) >= MIN_GROUP:
            gaps.append((statistics.median(new) - statistics.median(old), len(new)))
            n_new += len(new)
            n_old += len(old)
    comp = round(100 * sum(g * w for g, w in gaps) / sum(w for _, w in gaps), 1) if gaps else None
    out["C3"] = metric("C3", comp, min(n_new, n_old))
    oob = sum(1 for r in active if r["base_salary"] < r["band_min"] or r["base_salary"] > r["band_max"])
    out["C4"] = rate("C4", oob, len(active))
    return out


def as_of(rows, ref):
    """The rows as they were on the reference date: drop people hired after
    it, and undo leaving or promotion events dated after it."""
    out = []
    for r in rows:
        if r["hire_date"] > ref:
            continue
        r = dict(r)
        if r["term_date"] and r["term_date"] > ref:
            r["term_date"], r["term_type"], r["regretted"] = None, "", False
        if r["last_promo_date"] and r["last_promo_date"] > ref:
            r["last_promo_date"] = None
        out.append(r)
    return out


def suppressed(m):
    return {"label": m["label"], "value": None, "n": None, "unit": m["unit"], "status": "suppressed"}


# Measures whose n is the active headcount (see compute()). app.py has the same list.
HEADCOUNT_N = {"R1", "M3", "P1", "P2", "P3", "C1", "C4"}


def protect_company(company, orgs, rows):
    """Stop suppressed org numbers being worked out by subtraction.

    The company figure covers every org. If some orgs' figures for a measure
    are suppressed, the company figure minus the visible orgs gives the hidden
    orgs' combined figure (exactly, for counts and rates). So treat the hidden
    orgs, plus any rows whose org isn't in ORGS, as one group, and run the
    same suppression rule on it. If that group would be suppressed too, hide
    the company figure as well.

    P1 needs more: each org's P1 is its top-performer share minus the company
    share, and the company share includes every org. So the visible orgs' P1
    values give back a hidden org's count even when the company P1 (always 0)
    is hidden. If any org's P1 is suppressed, or the group outside ORGS would
    be, P1 is suppressed everywhere. app.py's gate checks the same rule."""
    other = [r for r in rows if r["org"] not in ORGS]   # never shown on its own
    hidden_company = []
    for mid in THRESHOLDS:
        if company[mid]["value"] is None:
            continue
        hidden = [org for org in ORGS if orgs[org][mid]["value"] is None]
        if not hidden and not other:
            continue
        group = other + [r for r in rows if r["org"] in hidden]
        if not group:
            continue   # the hidden orgs have nobody in them
        # Org headcounts are always shown, so for measures whose n is the
        # active headcount, company n minus the org headcounts gives the rows
        # outside ORGS on their own. Check that group separately too.
        alone_bad = mid in HEADCOUNT_N and other and compute(other, rows)[mid]["value"] is None
        if mid == "P1":
            if hidden or alone_bad or compute(group, rows)["P1"]["value"] is None:
                company["P1"] = suppressed(company["P1"])
                for org in ORGS:
                    orgs[org]["P1"] = suppressed(orgs[org]["P1"])
                hidden_company.append(mid)
            continue
        if alone_bad or compute(group, rows)[mid]["value"] is None:
            company[mid] = suppressed(company[mid])
            hidden_company.append(mid)
    return hidden_company


def main(src, dst, ref=None):
    global REF
    if ref:
        REF = date.fromisoformat(ref)
    else:   # the latest quarter start
        today = date.today()
        REF = date(today.year, (today.month - 1) // 3 * 3 + 1, 1)
    if REF.day != 1 or REF.month not in (1, 4, 7, 10):
        # Runs on nearby dates differ only by the few people whose events fall
        # in between, which singles them out. app.py enforces the same rule.
        raise SystemExit(f"reference date must be the first day of a quarter, got {REF.isoformat()}")
    rows = as_of(load(src), REF)
    company = compute(rows, rows)
    orgs = {org: compute([r for r in rows if r["org"] == org], rows) for org in ORGS}
    protect_company(company, orgs, rows)
    result = {
        "reference_date": REF.isoformat(),
        "min_group": MIN_GROUP,
        "company": company,
        "orgs": orgs,
    }
    with open(dst, "w") as f:
        json.dump(result, f, indent=2)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
