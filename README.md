# Zenline category review

A mini category opportunity review for the Hair Coloration category of the fictional drugstore
retailer in the brief. Built by Catherine Xu for the Zenline AI Forward Deployed Engineer
take-home.

One script reads the three provided data files, applies five documented rules, and writes a
tabbed report for the category manager, a product-level action list, and a record of the
assumptions and calculations.

**Open the report in your browser:** <https://catherixxu.github.io/zenline-category-review/>

The link shows the full report, served by GitHub Pages from `output/category_review.html` on
`main`. The same file opens offline after a clone or download.

## Run it

Requires Python 3.10 or newer. Tested with Python 3.13 and pandas 2.2 and 3.0.

```
pip install -r requirements.txt
python src/review.py --input data --output output
pytest
```

`--input` is the folder with the three CSV files. `--output` is where the results go. Both are
arguments so the review can be rerun on a new data drop into a new folder.

If a file or column is missing, a required field is blank, a status label is unknown, an ID is
duplicated or the metadata does not match the SKU file, the script stops with a `DATA ERROR`
message and writes nothing.

## What it produces

| File | For whom | What it is |
|---|---|---|
| `output/category_review.html` | Category manager | A tabbed report: Info, Executive summary, Actions (one sub-tab per action with the products to act on and what we learned), Open points to clarify, Full action list |
| `output/action_list.csv` | Whoever executes | One row per product with an action: rule, evidence fields, estimate with formula, owner, who to coordinate with |
| `output/assumptions_and_calculations.md` | Anyone checking the numbers | For each action the rule, the data fields used, the estimate formula and the owner, plus the thresholds and data checks of the run |

The report is one self-contained file and opens offline in any browser. It uses Zenline AI's
visual language. The logo is embedded from `src/assets/zenline-logo.png`. Zenline's typeface is
licensed and not included; the report falls back to Inter, embedded from `src/assets/` under
the SIL Open Font License.

## Assumptions

The main assumptions, in short:

- **Trends give direction, not euros.** The trend columns are used to tell growing from
  shrinking. No euro figure is derived from them, because the data does not say how they are
  calculated.
- **Share is channel revenue divided by market revenue.** The average over all products is
  6.3%, and it is the yardstick for "selling below potential".
- **Margin is gross margin as a share of revenue.**
- **`inaktiv` means being phased out but still selling down stock.** Inactive products are
  reviewed only for relisting.
- **The data covers roughly April to June.** Seasonal products that sell poorly are marked
  down, not delisted.
- **Competitor data is evidence beside the rules.** Every product is matched to the competitor
  file by exact product name where there is one (2 products), otherwise by subcategory and
  shade (175), otherwise by shade only (58). The competitor trend is shown beside every action
  and changes none. Where it points the other way than the product's own market trend, the
  product is flagged for discussion with the retailer. Most matches rest on one or two
  competitor products, and that number is always shown.
- **Competitor pricing rests on a major assumption: pack size.** The competitor file has no
  pack size. A price gap is calculated only for the 2 products a competitor lists under exactly
  the same name, and it assumes the competitor sells the same pack. For every other product
  the average competitor price is shown as context only and no gap is calculated.
- **Estimates are simple 12-week run rates.** No price elasticity and no effect of one action
  on another. They measure different things and are not added up.
- **One primary action per product.** Where several rules apply, a fixed order decides, so no
  product is counted twice.
- **Contradictory rows are kept and marked.** A product whose numbers contradict each other
  keeps its action and carries a "check first" marker.

The full list, with the rules, thresholds, consistency checks and the reason for every
decision, is in [docs/DECISIONS.md](docs/DECISIONS.md). The formula behind each estimate is in
`output/assumptions_and_calculations.md`, written by the same run as the report.

## Next improvements

- **Compare runs.** Show what is new, what is resolved and what persists since the previous
  review.
- **Track action status.** Record which actions were accepted, done or rejected, so the next
  run does not raise them again.
- **Match competitors by EAN and pack size.** Exact product names match 2 of the 75 competitor
  rows, and competitor prices cannot be compared per unit without pack sizes. An EAN
  match would allow price comparison product by product.
- **Add a seasonal calendar.** Time markdowns and relistings by season instead of a single
  seasonal flag.
- **Replace relative thresholds with the retailer's own.** For example a minimum revenue per
  product per week in place of the bottom-quartile cut-off.
- **Refine the estimates.** Add price elasticity and the effect of one action on another.
- **Run it on a schedule.** One run per data drop into a dated folder, with the report sent to
  the category manager.
- **Separate layout from logic.** Move the report layout out of `src/review.py` once the format is
  stable.

## Where things are

```
README.md              this file
requirements.txt       pandas and pytest
index.html             forwards the GitHub Pages address to the report
src/
  review.py            the whole pipeline; thresholds sit in the CONFIG block at the top
  assets/              logo and font embedded into the report
tests/
  test_review.py       29 tests: known cases, figures, hard failures, outputs
data/                  the provided files, untouched, plus the task text
output/                the results of the last run: report, action list, method file
docs/
  DECISIONS.md         assumptions, rules, thresholds and every decision, with reasons
  AI_WORKFLOW.md       how AI was used and how the results were checked
```

`src/review.py` runs in this order: load and validate, consistency checks, rules, action list,
report, method file. Everything up to and including the action list is in the first 590 lines.
The report layout follows, and `main()` at the end shows the whole run in order.
