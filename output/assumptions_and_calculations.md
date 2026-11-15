# Assumptions and calculations

Written by `review.py` from data as of 2026-07-01. This file documents how the figures in `category_review.html` and `action_list.csv` were produced. The decisions behind the rules, with their reasons, are in `docs/DECISIONS.md`.

## Scope

- 235 products: 197 active, 38 inactive.
- 12-week channel revenue €1,387,130. Average share of the tracked market 6.29% (total channel revenue ÷ total market revenue over all products).
- Trend and margin columns are fractions in the data: 0.18 means 18%.

## The five actions

### 1. Fix availability (R1)

- Rule: Active, out of stock, and the market for the product is growing (market trend above 0%).
- Data fields used: `status`, `stock_status`, `market_sales_trend_12w_pct`, `channel_revenue_eur_12w`
- Estimate: The 12-week channel revenue of each product: what it earned in the period and stands to lose while it is unavailable.
- Result in this run: 9 products as primary action (9 trigger the rule), €61,646 revenue at risk.
- Owner: Category manager. Coordinate with supply chain: reorder and confirm the delivery date.

### 2. Price review (R4)

- Rule: Active, margin below 25%, and sales growing both in our channel and in the market.
- Data fields used: `status`, `channel_margin_pct`, `channel_sales_trend_12w_pct`, `market_sales_trend_12w_pct`, `channel_revenue_eur_12w`
- Estimate: 12-week channel revenue × a 3% price increase at unchanged volume. No price elasticity is applied.
- Result in this run: 11 products as primary action (16 trigger the rule), €2,347 extra revenue.
- Owner: Category manager. Coordinate with pricing: check the price ladder and competitor prices.

### 3. Delist or mark down (R5)

- Rule: Active, 12-week revenue in the bottom quarter of all products (below €1,306 in this data), and sales falling both in our channel and in the market. Evergreen products are delist candidates, seasonal products are marked down.
- Data fields used: `status`, `channel_revenue_eur_12w`, `channel_sales_trend_12w_pct`, `market_sales_trend_12w_pct`, `seasonality`, `shelf_space_cm`
- Estimate: Revenue given up is the 12-week channel revenue of each product. Shelf space is the shelf allocation listed for it.
- Result in this run: 19 products as primary action (19 trigger the rule), €6,689 revenue forgone.
- Owner: Category manager. Coordinate with supplier and pricing: sell-through, returns and markdown level.

### 4. Relist (R2)

- Rule: Inactive, and the market for the product grew by more than 20%.
- Data fields used: `status`, `market_sales_trend_12w_pct`, `market_revenue_eur_12w`, `channel_revenue_eur_12w`, `shelf_space_cm`
- Estimate: Market revenue × our average share (6.3%) − current revenue, never below zero. It is the revenue we would add by reaching our average share.
- Result in this run: 15 products as primary action (15 trigger the rule), €30,364 incremental revenue.
- Owner: Category manager. Coordinate with supplier: confirm availability and terms.

### 5. Promote (R3)

- Rule: Active, not out of stock, market trend above 15%, and our share of the product's market below half of our average (below 3.1%).
- Data fields used: `status`, `stock_status`, `market_sales_trend_12w_pct`, `channel_revenue_eur_12w`, `market_revenue_eur_12w`
- Estimate: Market revenue × our average share (6.3%) − current revenue, never below zero. It is the revenue we would add by reaching our average share.
- Result in this run: 13 products as primary action (13 trigger the rule), €20,486 incremental revenue.
- Owner: Category manager. Coordinate with store operations: placement and promotion slot.

## One action per product

Where several rules apply to a product, the first in this order is its primary action: Fix availability (R1), Relist (R2), Promote (R3), Price review (R4), Delist or mark down (R5). The other rules it triggers are listed in the `also_qualifies_for` column of the action list. The order of the actions in the report is the order of execution, which is a separate choice.

## Thresholds used in this run

| Threshold | Value |
|---|---|
| Our average share of the tracked market | 6.3% |
| Restock: market trend above | 0% |
| Price review: margin below | 25% |
| Price review: price increase used in the estimate | 3% |
| Price gap note: price differs from the competitor by more than | 20% |
| Delist or mark down: revenue in the bottom | 25% of all products, which is below €1,306 in this data |
| Relist: market trend above | +20% |
| Promote: market trend above | +15% |
| Promote: share below | 50% of the average share |
| Watchlist: low stock and market trend above | +15% |
| Assortment gap: no active product and competitor trend above | 0% |
| Estimate shown as provisional when open data points carry at least | 10% of it |

## How to read the estimates

- All estimates are simple 12-week run rates. They assume no change in volume when a price changes and no effect of one action on another.
- Sales trends are used as a direction only. No euro figure is derived from a trend.
- The estimates measure different things (revenue at risk, extra revenue, revenue given up, incremental revenue) and are not added up.
- Shelf space: delist candidates free their listed shelf space now, seasonal markdown candidates free theirs after sell-through.
- Competitor data is matched by exact product name only. A price gap is noted when the prices differ by more than 20%. 2 such notes exist in this run.

## Competitor data

Every product is matched to the competitor file at the best available level. The first level that finds competitor products is used:

| Match level | Products |
|---|---|
| Exact name | 2 |
| Subcategory and shade | 175 |
| Shade only | 58 |

- The competitor trend, the popularity score (clipped to 0 to 100) and the competitor price are averaged over the matched competitor products. The number of products behind each signal is in `competitor_products`.
- The match is thin: the competitor file covers 51 combinations of subcategory and shade, and 33 of them have a single product.
- The competitor signal is evidence beside the rules. It changes no action and no estimate.
- A product is flagged `competitor_trend_differs` when its competitor trend and its market trend (`market_sales_trend_12w_pct`) have opposite signs. In this run: 112 of 235 products, 26 of the 67 with an action (fix availability 2, price review 1, delist or mark down 19, relist 3, promote 1). These are listed in the report for discussion with the retailer.
- Assortment gaps: combinations of subcategory and shade with competitor products, a competitor trend above 0% and no active product of ours. 11 in this run, not sized.

**Pack size is a major assumption for anything that involves a competitor price.** The competitor file has no pack size. A price gap is calculated only where a competitor lists exactly our product name, and it assumes the competitor sells the same pack. For all other products the average competitor price of the matched products is shown as context only (`competitor_avg_price_eur`) and no gap is calculated.

## Data checks

A product whose figures contradict each other keeps its action and is marked "Check first" in the report (`verify_first` in the action list).

| Check | Products |
|---|---|
| No revenue, but the sales trend is not -100% | 7 |
| Revenue recorded, but the sales trend shows a near-total collapse | 1 |
| Revenue recorded with zero units sold | 1 |
| Revenue / units differs from the list price by more than 25% | 3 |
| Active product with no revenue in the period | 5 |

13 products are marked in total, 10 of them with an action. In the competitor file, 4 popularity scores lie outside the 0 to 100 range. They are clipped and affect no action.

The run stops before any rule if a file or column is missing, a required field is blank, a status or stock label is unknown, an ID is duplicated, or the metadata does not match the SKU file one to one.

## Open questions

- What does the status "inaktiv" mean in practice? We read it as being phased out while the remaining stock sells down. The answer decides whether the relist candidates of action 4 are really off the shelf.
- How is the sales trend calculated? All five actions use the trend as a direction. A different basis could move products across the thresholds.
- Do inactive products still occupy the shelf space listed for them? If they do, relisting needs no new space and action 3 is not a precondition for action 4.
- Is there a minimum revenue per product per week? It would replace the bottom-quarter cut-off used for delisting in action 3 with the retailer's own standard.
- Where competitor and market trends differ for a product, which signal should lead? It decides how much weight the competitor view gets. Today it is shown beside each action and changes none.
- Which pack sizes do the competitor prices refer to? The competitor data has no pack size. Competitor prices are therefore context only, and the two price gaps assume the same pack as ours.
