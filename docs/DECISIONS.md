# Decisions log

Source of truth for the assumptions, rules and thresholds in `review.py`. Sections 1 to 6 were set from the data exploration, before any code was written. Section 7 records the decisions taken during the build, each with its reason. A change is recorded here first and then made in the code.

All counts and euro figures below were computed from the three provided CSV files. "12 weeks" means the 12-week period in the data.

## 1. Assumptions

| # | Assumption | Consequence |
|---|---|---|
| A1 | Trend columns are used as direction and strength only. We never derive euro figures from them. | The dictionary does not define the trend basis, and 7 rows with zero revenue carry a trend other than −100%, so trend and revenue cannot be reconciled. |
| A2 | `channel_margin_pct` is gross margin as a share of revenue. | Used for price review. |
| A3 | Channel revenue ÷ market revenue is our share of that product's market. Overall share is 6.3%. | Basis for "weak channel execution". |
| A4 | `inaktiv` means being phased out but still selling down stock (35 of 38 still sell, €191,360 in 12 weeks). | Inactive SKUs are reviewed only for relisting. |
| A5 | The data covers roughly April to June 2026 (signals are dated 1 July 2026). | The 30 seasonal SKUs get markdown, not delist. |
| A6 | Competitor signals are used at subcategory and shade level, and by product name only for the 2 exact matches. Scores are clipped to 0–100. | No per-SKU price matching beyond exact matches. |
| A7 | Euro impact is a simple run-rate estimate with the formula shown. No price elasticity. | Figures are labelled as estimates. |
| A8 | Consistency checks run on every row. Flagged rows keep their action but carry a "verify first" marker. | See section 3. |
| A9 | One primary action per SKU. Where rules overlap, precedence is R1, R2, R3, R4, R5. | Avoids double counting. Precedence is separate from the presentation order in section 4. |

## 2. Rules and thresholds

| Rule | Trigger | SKUs | Estimated impact, 12 weeks | Formula |
|---|---|---|---|---|
| R1 Fix availability | Active, out of stock, market trend above 0 | 9 | €61,646 revenue at risk | Sum of 12-week channel revenue |
| R2 Relist | Inactive, market trend above +20% | 15 | €30,364 incremental | Market revenue × average share − current revenue, floored at 0 |
| R3 Promote | Active, not out of stock, market trend above +15%, share below half the average | 13 | €20,486 incremental | Same as R2 |
| R4 Price review | Active, margin below 25%, channel and market trend both above 0 | 11 as primary action (16 triggered) | €2,347 | Revenue × 3% price increase at constant volume |
| R4b Price gap note | Exact product-name match with a competitor row, price differs by more than 20% | 2 | Not sized | Our price ÷ competitor price − 1 |
| R5 Delist or mark down | Active, revenue in the bottom quartile (below €1,306), channel and market trend both below 0 | 19 | 131 cm of shelf freed, €6,689 revenue forgone | Sum of shelf space and revenue |

R4 triggers on 16 SKUs (€3,526). Five of them have an earlier primary action under A9: HC0043, HC0053 and HC0070 are restock first (R1), and HC0061 and HC0094 are promote first (R3). The report counts the 11 and mentions the 5 (B7).

R4b is a secondary note. It does not change a SKU's primary action, and it is shown as evidence in the price review. On this data it catches:
- HC0006 Garnier Olia Copper Glow: €4.77 against €7.95 at rossmann.de, 40% below. Primary action R1.
- HC0007 Manic Panic High Voltage Ultra Violet: €18.92 against €11.99 at amazon.de, 58% above. Primary action R2.

R5 detail: 15 of the 19 are Evergreen and are delist candidates (100 cm). The other 4 are seasonal and are markdown candidates (A5).

Watchlist, not a recommendation: 13 active low-stock SKUs in markets growing above 15% (€100,098 revenue).

## 3. Consistency checks

| Check | Rows in this data |
|---|---|
| Zero revenue but channel trend is not −100% | 7 |
| Revenue above zero with channel trend at −90% or worse | 1 (HC0001) |
| Revenue recorded but zero units | 1 |
| Implied price (revenue ÷ units) more than 25% off list price | 3 |
| Competitor score outside 0–100 | 4 |
| Active product with no revenue in the period (B9) | 5 (HC0002, HC0062, HC0117, HC0137, HC0193) |

The script also fails before running any rule if a file or column is missing, a required field is blank, `status` or `stock_status` holds an unknown label, an ID is duplicated, or the metadata does not join 1:1. The blank and unknown-label failures are there because an unknown label would otherwise drop a SKU from every rule without warning. This data has no blanks and no unknown labels.

## 4. Order of recommendations

The order is the sequence of execution, not a ranking by size. The report shows the euro impact beside each item.

Principles: restock first, then price review, and free up shelf space before adding to the range.

| # | Recommendation | Stage | Rule |
|---|---|---|---|
| 1 | Restock 9 out-of-stock SKUs | Fix the existing range | R1 |
| 2 | Price review on 11 SKUs, plus the 2 price gap notes | Fix the existing range | R4, R4b |
| 3 | Delist or mark down 19 SKUs | Change the range | R5 |
| 4 | Relist 15 inactive SKUs | Change the range | R2 |
| 5 | Fix execution on 13 under-indexed SKUs | Grow | R3 |

Recommendations 3 and 4 are presented as a linked pair. Delisting the 15 Evergreen SKUs frees 100 cm now, and the 4 seasonal markdown SKUs free a further 31 cm only after they sell through. The 15 relist candidates need 105 cm. The pairing depends on the open question in section 6 on whether inactive SKUs still hold their shelf space. Recommendations 1 and 2 are linked through HC0006, which is both out of stock and priced 40% below Rossmann.

## 5. Artifact and scope

- One script (`review.py`) that writes `output/category_review.html` (a tabbed report that includes the executive summary), `output/action_list.csv` and `output/assumptions_and_calculations.md` (B13, B14).
- Rule-based, no ML, no web app.
- Built for reruns: input and output folders are arguments, thresholds sit in one config block. Output file names are fixed; the outputs show the data date and no creation date, and the caller chooses the output folder (B1, B19).
- Described in the README as next improvements, not built: run-over-run comparison, action status tracking, EAN-level competitor matching, seasonal calendar.

## 6. Open questions for the customer

- What does `inaktiv` mean operationally (A4)?
- How is the trend calculated (A1)?
- Do inactive SKUs still occupy the shelf space listed in the metadata?
- Is there a minimum revenue per SKU per week that should replace the relative bottom-quartile cut-off in R5 (B2)?

## 7. Build decisions

| # | Decision | Reason |
|---|---|---|
| B1 | Output file names are fixed (`category_review.html`, `action_list.csv`). The report and the CSV show the data date (latest `observed_date` in the competitor file) and the run date. History is kept by pointing the output argument at a new folder. | One predictable file to open; a scheduled run passes a dated folder. Replaces "outputs are dated" in section 5. |
| B2 | The R5 bottom-quartile cut-off is recomputed on every run. The config holds 25%, and the report prints the euro value. €1,306 is the value on this data drop. | A fixed euro value stops meaning "bottom quarter" when the period or the category changes. |
| B3 | Dependencies are pandas and pytest only. Charts are inline SVG written by the script. | The report opens offline in any browser. |
| B4 | Every action is owned by the category manager. A separate "coordinate with" note names who else is involved. The wording of the notes is in B10. | All actions sit within category management scope. |
| B5 | The README is written in two steps: run instructions with the build, then assumptions, limitations and next improvements once the outputs are final. | Keeps the build within the time budget. |
| B6 | Shelf space is reported as 100 cm freed now and 31 cm after markdown sell-through, against 105 cm needed for relisting. | The 4 seasonal SKUs stay on the shelf until they sell through. See section 4. |
| B7 | R4 is reported on the 11 SKUs where it is the primary action (€2,347). The 5 SKUs that also trigger R4 but are restock or promote first are mentioned, not counted. | Keeps A9: no SKU appears in two recommendations. Replaces 16 SKUs and €3,526. |
| B8 | Confirmed by reproduction: the average share is total channel revenue ÷ total market revenue over all 235 SKUs (6.3%), and the R5 bottom quartile is taken over all 235 SKUs, active and inactive. | These readings reproduce the R2, R3 and R5 figures in section 2 exactly. |
| B9 | New consistency check: an active SKU with zero revenue is marked "verify first". | HC0002 is a promote candidate that is in stock, sold nothing and has a −100% trend. That points to a listing problem, not a promotion gap. 13 SKUs are now marked, 10 of them with an action. |
| B10 | "Coordinate with" notes: R1 supply chain (reorder and confirm the delivery date), R4 pricing (price ladder and competitor prices), R5 supplier and pricing (sell-through, returns and markdown level), R2 supplier (availability and terms), R3 store operations (placement and promotion slot). | The wording used in the action list (B4). |
| B11 | The action list CSV holds only SKUs with a primary action (67). The 9 watchlist SKUs without an action appear in the report's watchlist section only. | The watchlist is not a recommendation. |
| B12 | Two HTML outputs from the same run: a one-page executive summary (the five actions, the decision asked for, the estimate, and what still needs a data check) and the in-depth category review. Each links to the other. | The in-depth review is analytical. A decision maker needs one page. Both read the same summary figures, so they cannot disagree. Replaces "one HTML report". |
| B13 | The report is one tabbed HTML file built as a consulting deliverable for the category manager. Tabs: Info, Executive summary, Actions (one sub-tab per action: Restock, Price, Delist, Relist, Promote), Open points to clarify, Full action list. Each action has "What to do" with its products and "What we learned" with two exhibits. The separate `executive_summary.html` is dropped. | Easier to read and digest for a business stakeholder. The executive summary tab is what the category manager can show a VP. Replaces B12. |
| B14 | Rule, data fields, estimate formula and owner are removed from the report and written to `output/assumptions_and_calculations.md` by the same run, with the thresholds and data checks. | The report is for decisions; the method is documentation. Generating the file keeps it in step with the code. |
| B15 | The report uses Zenline AI's visual language, read from tryzenline.ai: near-black, orange `#f25722`, lilac `#4f5db5`, warm beige borders, medium-weight headings with tight tracking, 6 px buttons, 20 px cards, dotted separators. The Zenline logo, from `assets/zenline-logo.png`, sits at the top right. Zenline's typeface Roobert is licensed and is not redistributed; the report uses it if installed and otherwise Inter, Zenline's own declared fallback, embedded under its open licence. A footer states that the report is a take-home exercise prepared by Catherine Xu. | The report is presented as a Zenline deliverable. The footer keeps the file from being mistaken for an official Zenline product. |
| B16 | The deliverable is named "Zenline category review". The customer name from the brief is not used in the outputs; the text says "the retailer". Product names in the data are unchanged. | Follows from B15: the report is presented as a Zenline deliverable for a retailer. |
| B17 | An estimate is tagged "Provisional" when products with an open data point carry at least 10% of it. On this data that is relist (€25,154 of €30,364) and promote (€6,261 of €20,486). | The executive summary carries no commentary on the analysis, but it must not present these two figures as firm. |
| B18 | The watchlist is a section inside the Restock action. The subcategory table sits at the end of the executive summary. Checks are reframed as "Open points to clarify": data points to confirm and questions for the retailer, each with why it matters. | The watchlist is about stock, so it sits with the stock action. Open points read as what to clarify next, not as a list of data errors. The subcategory table answers the "subcategories" part of the customer brief. |
| B19 | No output carries a creation date. The report, the CSV and the method file show the data date only. | The same data gives identical files on any day, so a rerun can be compared with the previous one byte for byte. The data date identifies the data drop, and the output folder identifies the run. Amends B1. |
