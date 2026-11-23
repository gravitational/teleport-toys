# Org Health Playbook

The agent's instructions for analyzing people data inside the Beam. It defines what to measure, what counts as a flag, and what each org's read-out looks like.

The agent reads this file inside every worker Beam, so anything written here shapes the insights it produces. The numbers themselves are calculated by `compute_metrics.py`; if you change a measure or threshold here, change it there too.

All thresholds are starting points, not published benchmarks. Adjust them to fit your organization.

---

## 1. Scope

- **Orgs:** the sample data is a fictional company of about 300 employees across 5 orgs: Engineering, Product, Sales, Marketing, G&A. To use your own org names, update `ORGS` in `compute_metrics.py`.
- **Window:** trailing 12 months unless noted.
- **Unit of reporting:** one read-out per org, for that org's leader. The company-wide figure appears next to each org's number as the comparison point.
- **Ratings:** a 4-point scale, where 4 is a top performer. If yours differs, update `TOP` in `compute_metrics.py`.
- **Privacy floor:** no number may describe fewer than 5 people (see section 5).

---

## 2. Measures

Each measure gets a status: 🟢 healthy, 🟡 watch, 🔴 act.

### Retention

| ID | Measure | Definition | 🟢 | 🟡 | 🔴 |
|---|---|---|---|---|---|
| R1 | Voluntary attrition | Voluntary leavers in the last 12 months ÷ average headcount | under 10% | 10 to 15% | over 15% |
| R2 | Regretted attrition | Share of voluntary leavers flagged as regretted (the `regretted` field, typically set by the manager or HR at exit) | under 25% | 25 to 40% | over 40% |
| R3 | First-year attrition | Of people hired in the last 24 months, share who left within their first 12 months | under 10% | 10 to 20% | over 20% |
| R4 | High-performer stall | Share of top-rated employees with 2+ years' tenure and no promotion in 24+ months | under 30% | 30 to 50% | over 50% |

### Managers and structure

| ID | Measure | Definition | 🟢 | 🟡 | 🔴 |
|---|---|---|---|---|---|
| M1 | Average span of control | Average direct reports per people manager | 5 to 8 | 3 to 4, or 9 to 10 | under 3, or over 10 |
| M2 | Very small teams | Share of managers with only 1 or 2 direct reports | under 15% | 15 to 25% | over 25% |
| M3 | Manager churn | Share of employees whose manager changed in the last 12 months | under 20% | 20 to 35% | over 35% |

### Performance and growth

| ID | Measure | Definition | 🟢 | 🟡 | 🔴 |
|---|---|---|---|---|---|
| P1 | Rating skew | Org's share of top ratings minus the company's share | within 5 points | 5 to 10 points | over 10 points |
| P2 | Promotion rate | Promotions in the last 12 months ÷ average headcount | 8 to 15% | 5 to 8%, or 15 to 20% | under 5%, or over 20% |
| P3 | Stuck in level | Share of employees in the same level for over 36 months | under 20% | 20 to 30% | over 30% |

### Pay health

Comp is always expressed relative to the pay band, never in dollars. "Band position" means base salary ÷ the midpoint of that person's pay band (1.00 = exactly at midpoint).

| ID | Measure | Definition | 🟢 | 🟡 | 🔴 |
|---|---|---|---|---|---|
| C1 | Median band position | Median band position across the org | 0.95 to 1.05 | 0.90 to 0.95, or 1.05 to 1.10 | under 0.90, or over 1.10 |
| C2 | Top performers paid low | Share of top-rated employees with band position under 0.95 | under 15% | 15 to 30% | over 30% |
| C3 | Pay compression | Within each level, median band position of people hired in the last 12 months minus that of people with 3+ years' tenure. The org figure averages those gaps across levels, weighted by new hires. | under 3 points | 3 to 8 points | over 8 points |
| C4 | Out of band | Share of employees paid below their band minimum or above its maximum | under 3% | 3 to 8% | over 8% |

Pay gaps by gender or other demographic groups are deliberately left out. They need demographic fields, which raise re-identification risk in small orgs.

---

## 3. Data the agent can see

The coded data only: the sample CSV file, or the `v_people_coded` view when reading from a database. No names, emails or government IDs exist in it.

| Field | Notes |
|---|---|
| `emp_code` | Random code. Can't be traced back to a person without the raw table, which the agent never sees. |
| `org` | The employee's org |
| `team` | Team name within the org |
| `manager_code` | Coded ID of the current manager |
| `job_family` | Job family the pay band belongs to |
| `level` | Job level (in the sample data, IC1 to IC6 and M1 to M4) |
| `hire_date` | |
| `term_date` | Empty if still employed |
| `term_type` | Voluntary or involuntary |
| `regretted` | True/false, voluntary leavers only |
| `rating_current`, `rating_prior` | Last two review cycles, 1 to 4 (4 = top performer) |
| `last_promo_date` | Empty if never promoted |
| `manager_changes_12m` | Number of manager changes in the last 12 months |
| `base_salary` | Annual base. Used only to compute band position, never reported |
| `band_min`, `band_mid`, `band_max` | Pay band for the person's level and job family |

---

## 4. Read-out format (one per org)

1. **Header:** org name, headcount, reporting period.
2. **Scorecard:** every measure in section 2, with the org's value, the company value, and the 🟢/🟡/🔴 status.
3. **Top things to look at (up to 3):** for each:
   - *What we see* (the pattern, in one or two sentences)
   - *Why it matters* (the likely business impact)
   - *Suggested next step* (a concrete, low-effort action for the leader)

   Only red and yellow measures earn a spot. Don't promote a healthy measure into a concern to fill the list. If an org has fewer than 3 red or yellow measures, list only those. If it has none, write a single item that says the org is healthy across all reported measures, mentions any measures that were suppressed, and names one thing it's doing well that's worth keeping.

   Write for the org leader. Don't refer to this playbook or its section numbers, and only connect a finding to another measure if that measure supports the point.

   Each item already shows its measure ID, so don't repeat it in the text. Use the status names as shown: "watch" for yellow and "act" for red.
4. **Data notes:** anything suppressed by the privacy floor, plus any caveats about small numbers.

### Interpretation notes

Context an experienced HR business partner would add before recommending action:

- **Very small teams (M2) and narrow spans (M1):** before suggesting teams be combined, check whether the small teams are new managers being developed or new teams still being staffed. Both are healthy reasons. Recommend confirming which it is with the leader first.
- **Manager churn (M3):** separate planned changes (a reorg, new teams) from unplanned ones (managers leaving). The response differs.
- **Pay compression (C3):** frame it as a fairness question for tenured staff, and recommend reviewing offer guidelines before correcting individual pay.
- **Rating skew (P1):** a high skew can be real (a strong team) or inflation. Recommend calibration, not a forced distribution.

The agent writes results as a structured file. A fixed template renders it as the dashboard, so the agent never writes free text straight onto the page.

---

## 5. Privacy rules

### Enforced automatically

The privacy gate runs after the agent finishes and blocks any read-out that contains:

1. A number that describes fewer than **5 people**: a population under 5, or a count within it (or the remainder) of 1 to 4. (`compute_metrics.py` suppresses these before the agent sees them, and the gate checks again.)
2. A company figure that, minus the visible org figures, would reveal a suppressed org's number. `compute_metrics.py` treats all the hidden orgs for a measure, plus anyone whose org isn't one of the 5, as one group and hides the company figure if that group is under the minimum too. Because org headcounts are always shown, people outside the 5 orgs are also checked on their own. P1 compares each org with the company, so a hidden org's P1 could be worked out from the others: if any org's P1 is suppressed, P1 is suppressed for every org. The gate repeats these checks.
3. A currency amount. Comp appears only as band positions and percentages.
4. An `emp_code` or `manager_code`.
5. A phrase that points to a very small group, such as "the only," "a single," "a lone," "3 people," "two engineers," "one employee," "a team of three" or "1 of 3 managers." Counts like "1 in 4" or "2 of 4" are blocked too, because with a small group they say exactly how many people; use percentages. Level names ("level 3," "IC3") and measure definitions ("managers with 1-2 direct reports") are allowed.
6. An email address, SSN-like or bank-account-like pattern.
7. An insight about a measure that is suppressed for that org, or a measure ID, org or field the app doesn't know. Only the `metric`, `what`, `why` and `next_step` fields of each insight are kept, and every one of them is checked.

Blocked read-outs go back to the agent with the reason and the phrase that triggered it, for example: `Sales insight 2 why: reference to a very small group ("two engineers")`. The agent gets up to 3 tries. The person running the app sees only the reason, not the phrase.

### Followed by the agent

These aren't checked automatically, so the agent must follow them:

- Never describe any individual's rating, level history, dates or other attributes.
- Never let a small group be worked out by subtraction in the text. The org and company figures are protected automatically, but the agent must not add its own breakdowns (for example a team's total and every subteam except one, which exposes the last subteam).

To change the minimum group size, update `MIN_GROUP` in both `compute_metrics.py` and `app.py`.

---

## 6. What the sample data should show

The fictional sample data has problems planted in it, so you can check a run is working. One org stays healthy as the comparison.

At the reference date 2026-10-01:

| Org | Should be red | Also visible | Suppressed |
|---|---|---|---|
| Engineering | R2 regretted attrition, R4 high-performer stall, C2 top performers paid low | R1 on watch | C4 |
| Product | M1 span, M2 very small teams, M3 manager churn | | R1, R2, P2, P3, C3 |
| Sales | R1 attrition, R3 first-year attrition, C3 pay compression | | R2, R4, M3, C2 |
| Marketing | P1 rating skew, P2 promotion rate | | R1, R2, R4, M3, P3, C3 |
| G&A | None (healthy comparison) | | R1, R2, M3, P3, C3 |
