# AI workflow

How AI was used in this take-home, who decided what, and how the results were checked.

## Setup

- Tool: Claude (Anthropic), working in a Claude project that holds the task text, the data files
  and `DECISIONS.md`.
- `DECISIONS.md` was agreed before the build started. A build chat then produced this repository,
  and a separate chat covers the README and interview preparation.
- Division of work: Claude writes the code. Catherine decides the rules, thresholds and final
  recommendations.

## Working rules given to the AI

- `docs/DECISIONS.md` is the source of truth. No decision changes silently: the AI proposes, and
  the file is updated only after Catherine agrees.
- Every number comes from running code on the provided files. No figure is estimated or recalled.
- The build runs in stages. The AI stops after each stage, shows the result against
  `DECISIONS.md`, and waits for approval.
- Small commits with the AI co-author line kept. History is never rewritten to hide AI use.
- No web research, no sub-agents, no ML, no database, no web app.

## What the AI did

- Wrote `review.py`, `test_review.py`, the two HTML layouts and this repository's structure.
- Reproduced every approved figure by code before building on it, and reported each difference
  instead of adjusting a rule.
- Proposed options with a recommendation where a choice was open.
- Rendered both HTML files in Chrome at desktop and phone width and fixed what it saw (an
  overlapping chart label, a table that was too wide).

## What Catherine decided

All of these are recorded with reasons in `docs/DECISIONS.md`.

- The assumptions, the five rules, their thresholds, the precedence and the order of the
  recommendations (sections 1 to 4, approved before any code was written).
- Output naming, the recomputed bottom-quartile cut-off and the dependencies (B1 to B3).
- Every action is owned by the category manager, with a "coordinate with" note (B4, B10). The AI
  had proposed different owners per action.
- Price review is reported on the 11 products where it is the primary action, not the 16 that
  trigger the rule (B7).
- A new check for active products with zero revenue (B9).
- A one-page executive summary next to the in-depth review, because the in-depth review alone was
  too analytical for a decision maker (B12).
- The task text is kept in the repository. The AI had recommended leaving it out.

## What the AI surfaced that changed the result

| Finding | What happened |
|---|---|
| The 131 cm of freed shelf space included 31 cm from seasonal products that stay on the shelf until they sell through. Delisting alone frees 100 cm, against 105 cm needed for relisting. | Wording changed to "100 cm now, 31 cm later" (B6). |
| The price review rule triggers on 16 products, but 5 of them already have an earlier primary action. The approved figure of 16 contradicted the one-action-per-product assumption. | Reported as 11 products and €2,347 (B7). |
| HC0002 was a promote candidate with zero revenue and no warning. | New consistency check (B9). |
| €25,154 of the €30,364 relist estimate rests on three products whose figures need checking. | Stated in both reports. No rule was changed. |
| An unknown status label or a blank field would silently drop a product from every rule. | Added as a hard failure. |
| "Average share" and "bottom quartile" had two possible readings each. | The reading that reproduces the approved figures was confirmed and recorded (B8). |

## How the results were verified

Checks run by code during the build:

| Check | Result |
|---|---|
| Files in `data/` against the original zip | Byte-identical (SHA-256) |
| Consistency check counts against `DECISIONS.md` section 3 | Match |
| Count and estimate for every rule against `DECISIONS.md` section 2 | Match, except the price review count, which led to B7 |
| `pytest` | 23 tests pass |
| Tests fail when a threshold is changed (relist threshold 20% to 30%) | 4 tests fail, as they should |
| Five kinds of broken input | Each stops the run and writes nothing |
| Same outputs under pandas 2.2 and pandas 3.0 | Identical files |
| Fresh clone from GitHub, run into a new folder | Outputs identical to the committed ones |
| Both HTML files load nothing from the web | Confirmed by test |
| Chart colours | Checked with a colour-blind-safety validator |

## Hand checks

These are spot checks to do by hand in a spreadsheet, straight from the provided CSV files. The
expected values are what the script produced. The status column is filled in by Catherine.

| # | Check | Expected | Status |
|---|---|---|---|
| 1 | HC0006 in `sku_performance.csv`: `aktiv`, `out_of_stock`, market trend 0.21 | Qualifies for restock | Not yet done |
| 2 | Sum of `channel_revenue_eur_12w` for HC0006, HC0043, HC0053, HC0070, HC0111, HC0119, HC0135, HC0165, HC0229 | 61,646.14 | Not yet done |
| 3 | Sum of `channel_revenue_eur_12w` ÷ sum of `market_revenue_eur_12w`, all 235 rows | 1,387,129.76 ÷ 22,048,168.15 = 6.29% | Not yet done |
| 4 | HC0007 relist estimate: 310,000 × 6.29136% − 0 | 19,503.22 | Not yet done |
| 5 | HC0015 price review estimate: 22,999.65 × 3% | 689.99 | Not yet done |
| 6 | HC0006 price gap: 4.77 ÷ 7.95 − 1 | −40.0% | Not yet done |
| 7 | 25th percentile of `channel_revenue_eur_12w`, all 235 rows (`PERCENTILE.INC` in a spreadsheet) | 1,306.465 | Not yet done |
