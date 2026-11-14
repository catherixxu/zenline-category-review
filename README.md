# Hair Coloration category review

A mini category opportunity review for ZenBeauty Retail (fictional German drugstore), category
Beauty > Hair Coloration. Built for the Zenline AI Forward Deployed Engineer take-home.

One script reads the three provided data files, applies five documented rules, and writes a
one-page executive summary, an in-depth review and a product-level action list.

## Run it

Requires Python 3.10 or newer. Tested with Python 3.13 and pandas 2.2 and 3.0.

```
pip install -r requirements.txt
python review.py --input data --output output
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
| `output/executive_summary.html` | Decision maker | The five actions on one page: the decision asked for, the 12-week estimate, and what still needs a data check |
| `output/category_review.html` | Category manager | The in-depth review: the evidence for each action, data checks, watchlist, the full action list, thresholds and open questions |
| `output/action_list.csv` | Whoever executes | One row per product with an action: rule, evidence fields, estimate with formula, owner, who to coordinate with |

Both HTML files are self-contained and open offline in any browser.

## Where things are

```
review.py            the whole pipeline; thresholds sit in the CONFIG block at the top
test_review.py       23 tests: known cases, figures, hard failures, outputs
data/                the provided files, untouched, plus the task text
output/              the results of the last run
docs/DECISIONS.md    assumptions, rules, thresholds and every decision, with reasons
docs/AI_WORKFLOW.md  what the AI did, what was decided by Catherine, what was verified and how
```

`review.py` runs in this order: load and validate, consistency checks, rules, action list,
in-depth report, executive summary. Everything up to and including the action list is in the
first 480 lines. The two report layouts follow, and `main()` at the end shows the whole run in
order.

## Assumptions and rules

They are documented in [docs/DECISIONS.md](docs/DECISIONS.md), which was agreed before any code
was written and updated with each later decision. The estimates are simple 12-week run rates and
are labelled as estimates wherever they appear.
