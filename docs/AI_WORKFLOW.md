# AI workflow

How AI was used in this take-home, and how the results were checked.

## Setup

- Tool: Claude (Anthropic), working in a Claude project that holds the task text, the data files
  and `DECISIONS.md`.
- Division of work: Claude wrote the code. The rules, the thresholds and the final
  recommendations are Catherine's decisions, recorded with their reasons in `docs/DECISIONS.md`.

## How the work was organised

1. Decide before building. The data was explored first, and the assumptions, rules, thresholds
   and the order of the recommendations were written into `docs/DECISIONS.md` before any code.
2. Build in stages: data checks, one rule at a time, the action list, the report, the tests.
   After each stage the figures were compared with `DECISIONS.md`.
3. Every number comes from running code on the provided files. No figure is estimated or
   recalled.
4. Where the code and the decisions log disagreed, the difference was resolved in the log, with
   a reason, and then in the code. No rule was adjusted to make a figure fit.
5. Small commits, each with the AI co-author line.
6. Scope kept small: no web research for the analysis, no sub-agents, no ML, no database, no
   web app.

## What the AI did

- Wrote `review.py`, `test_review.py`, the report layout and this repository's structure.
- Reproduced every figure in the decisions log by code before building on it, and reported each
  difference instead of adjusting a rule.
- Set out the options where a choice was open.
- Read Zenline AI's colours, type settings and layout patterns from the live site through a
  browser, and built the report in that visual language.
- Rendered the report in Chrome at desktop and phone width and fixed what it saw (an
  overlapping chart label, a table that was too wide).

## Decisions that shaped the result

All of these are recorded with reasons in `docs/DECISIONS.md`.

- The assumptions, the five rules, their thresholds, the precedence and the order of the
  recommendations (sections 1 to 4), fixed before any code was written.
- Output naming, the recomputed bottom-quartile cut-off and the dependencies (B1 to B3).
- Every action is owned by the category manager, with a "coordinate with" note, instead of a
  different owner per action (B4, B10).
- Price review is reported on the 11 products where it is the primary action, not the 16 that
  trigger the rule (B7).
- A check for active products with zero revenue (B9).
- A one-page executive summary, because the in-depth review alone was too analytical for a
  decision maker (B12).
- One tabbed report in the style of a consulting deliverable, which replaced the two separate
  files; rule details moved out of the report into a method file; Zenline AI's visual language
  and logo; the name "Zenline category review"; no creation date in any output (B13 to B19).
- The task text is kept in the repository, next to the data.

## Findings from reproducing the figures in code

| Finding | What happened |
|---|---|
| The 131 cm of freed shelf space included 31 cm from seasonal products that stay on the shelf until they sell through. Delisting alone frees 100 cm, against 105 cm needed for relisting. | Wording changed to "100 cm now, 31 cm later" (B6). |
| The price review rule triggers on 16 products, but 5 of them already have an earlier primary action. A count of 16 contradicted the one-action-per-product assumption. | Reported as 11 products and €2,347 (B7). |
| HC0002 was a promote candidate with zero revenue and no warning. | New consistency check (B9). |
| €25,154 of the €30,364 relist estimate rests on three products whose figures need checking. | The estimate is tagged "Provisional" in the report (B17). No rule was changed. |
| An unknown status label or a blank field would silently drop a product from every rule. | Added as a hard failure. |
| 5 of the 15 relist candidates have an estimate of €0: they already sell at or above the average share while inactive. | Stated in the relist tab and linked to the open question on what "inaktiv" means. No rule was changed. |
| The competitor trend and a product's own market trend point the same way for 123 of 235 products (52%). As a rule condition the competitor trend would remove every delist candidate. | Competitor data is shown as evidence beside the rules, with a flag where the two differ (B20, B21). |
| The competitor file has no pack size, and prices against the matched competitor average vary widely. | Price gaps stay limited to exact name matches; other competitor prices are context only (A10, B23). |
| "Average share" and "bottom quartile" had two possible readings each. | The reading that reproduces the figures in the decisions log is recorded (B8). |

## How the results were verified

Checks run by code during the build:

| Check | Result |
|---|---|
| Files in `data/` against the original zip | Byte-identical (SHA-256) |
| Consistency check counts against `DECISIONS.md` section 3 | Match |
| Count and estimate for every rule against `DECISIONS.md` section 2 | Match, except the price review count, which led to B7 |
| `pytest` | 29 tests pass |
| Tests fail when a threshold is changed (relist threshold 20% to 30%) | 4 tests fail, as they should |
| Five kinds of broken input | Each stops the run and writes nothing |
| Same outputs under pandas 2.2 and pandas 3.0 | Identical files |
| Two runs on the same data | Identical files, confirmed by test |
| Fresh clone from GitHub, run into a new folder | Outputs identical to the committed ones |
| The report loads nothing from the web | Confirmed by test |
| Rule details are in the method file and not in the report | Confirmed by test |
| Tabs, sub-tabs, links between tabs, the back button and the filter | Exercised in Chrome |
| Chart colours | Checked with a colour-blind-safety validator |

## Hand checks

Spot checks that reproduce the key figures by hand in a spreadsheet, straight from the provided
CSV files. The expected values are what the script produced.

| # | Check | Expected |
|---|---|---|
| 1 | HC0006 in `sku_performance.csv`: `aktiv`, `out_of_stock`, market trend 0.21 | Qualifies for restock |
| 2 | Sum of `channel_revenue_eur_12w` for HC0006, HC0043, HC0053, HC0070, HC0111, HC0119, HC0135, HC0165, HC0229 | 61,646.14 |
| 3 | Sum of `channel_revenue_eur_12w` ÷ sum of `market_revenue_eur_12w`, all 235 rows | 1,387,129.76 ÷ 22,048,168.15 = 6.29% |
| 4 | HC0007 relist estimate: 310,000 × 6.29136% − 0 | 19,503.22 |
| 5 | HC0015 price review estimate: 22,999.65 × 3% | 689.99 |
| 6 | HC0006 price gap: 4.77 ÷ 7.95 − 1 | −40.0% |
| 7 | 25th percentile of `channel_revenue_eur_12w`, all 235 rows (`PERCENTILE.INC` in a spreadsheet) | 1,306.465 |
