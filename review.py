#!/usr/bin/env python3
"""Category opportunity review: ZenBeauty Retail, Beauty > Hair Coloration.

Usage:
    python review.py --input data --output output

Steps: load and validate -> consistency checks -> rules -> outputs.
Every rule and threshold is documented in docs/DECISIONS.md.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# CONFIG: every label and threshold in one place.
# Trends and margins are stored as fractions in the data: 0.18 means 18%.
# ---------------------------------------------------------------------------
CONFIG = {
    # Labels used in the data
    "status_active": "aktiv",
    "status_inactive": "inaktiv",
    "stock_in": "in_stock",
    "stock_out": "out_of_stock",
    "stock_low": "low_stock",
    "season_evergreen": "Evergreen",  # any other seasonality value counts as seasonal
    # Consistency checks (DECISIONS.md section 3)
    "check_trend_total_loss": -1.00,  # trend a SKU with zero revenue should show
    "check_trend_collapse": -0.90,    # revenue above zero with a trend at or below this
    "check_price_tolerance": 0.25,    # allowed gap between revenue / units and list price
    "score_min": 0,                   # competitor scores are clipped to this range (A6)
    "score_max": 100,
    # Rules (DECISIONS.md section 2)
    "precedence": ["R1", "R2", "R3", "R4", "R5"],  # first match is the primary action (A9)
    "r1_market_trend_min": 0.0,       # R1: market trend must be above this
    "r2_market_trend_min": 0.20,      # R2: market trend must be above this
    "r3_market_trend_min": 0.15,      # R3: market trend must be above this
    "r3_share_factor": 0.5,           # R3: share must be below this fraction of the average
    "r4_margin_max": 0.25,            # R4: margin must be below this
    "r4_trend_min": 0.0,              # R4: channel and market trend must both be above this
    "r4_price_increase": 0.03,        # R4: price increase used for the estimate
}

FILES = {
    "skus": "sku_performance.csv",
    "metadata": "product_metadata.csv",
    "competitors": "competitor_market_signals.csv",
}

REQUIRED_COLUMNS = {
    "skus": [
        "product_id", "product_name", "brand", "subcategory", "shade_group",
        "seasonality", "status", "stock_status", "private_label", "price_eur",
        "channel_revenue_eur_12w", "channel_units_sold_12w", "channel_margin_pct",
        "channel_sales_trend_12w_pct", "market_revenue_eur_12w",
        "market_units_sold_12w", "market_sales_trend_12w_pct",
    ],
    "metadata": ["product_id", "launch_season", "shelf_space_cm", "supplier", "ean"],
    "competitors": [
        "competitor_product_id", "competitor_product_name", "brand", "subcategory",
        "shade_group", "price_eur", "signal_score_0_100", "source",
        "trend_score_12w_pct", "observed_date",
    ],
}

# Columns taken from the metadata file. The rest of that file repeats the SKU file.
METADATA_EXTRA = ["launch_season", "shelf_space_cm", "supplier", "ean"]

# Consistency checks on SKU rows: key -> wording shown to the reader.
SKU_CHECKS = {
    "zero_revenue_trend": "No revenue, but the sales trend is not -100%",
    "collapse_with_revenue": "Revenue recorded, but the sales trend shows a near-total collapse",
    "revenue_without_units": "Revenue recorded with zero units sold",
    "implied_price_gap": "Revenue / units differs from the list price by more than {tol:.0%}",
}


class DataError(Exception):
    """The input files cannot be trusted enough to run any rule."""


# ---------------------------------------------------------------------------
# 1. Load and validate
# ---------------------------------------------------------------------------
def load_data(input_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read the three files and return (skus joined with metadata, competitors).

    Stops with a DataError before any rule runs if a file or column is
    missing, a required field is blank, a status or stock label is unknown,
    a product ID is duplicated, or the metadata does not match the SKU file
    one to one.
    """
    frames = {}
    for key, filename in FILES.items():
        path = Path(input_dir) / filename
        if not path.exists():
            raise DataError(f"File not found: {path}")
        frame = pd.read_csv(path, dtype={"ean": str})
        missing = [c for c in REQUIRED_COLUMNS[key] if c not in frame.columns]
        if missing:
            raise DataError(f"{filename} is missing columns: {', '.join(missing)}")
        blank = [c for c in REQUIRED_COLUMNS[key] if frame[c].isna().any()]
        if blank:
            raise DataError(f"{filename} has blank values in: {', '.join(blank)}")
        frames[key] = frame

    skus, metadata, competitors = frames["skus"], frames["metadata"], frames["competitors"]

    # An unknown label would silently drop a SKU from every rule, so stop instead.
    known_labels = {
        "status": {CONFIG["status_active"], CONFIG["status_inactive"]},
        "stock_status": {CONFIG["stock_in"], CONFIG["stock_low"], CONFIG["stock_out"]},
    }
    for column, known in known_labels.items():
        unknown = sorted(set(skus[column]) - known)
        if unknown:
            raise DataError(f"{FILES['skus']} has unknown {column} values: {', '.join(unknown)}")

    for name, frame, id_col in [
        (FILES["skus"], skus, "product_id"),
        (FILES["metadata"], metadata, "product_id"),
        (FILES["competitors"], competitors, "competitor_product_id"),
    ]:
        duplicated = frame.loc[frame[id_col].duplicated(), id_col].tolist()
        if duplicated:
            raise DataError(f"{name} has duplicated IDs: {', '.join(map(str, duplicated[:10]))}")

    only_skus = sorted(set(skus["product_id"]) - set(metadata["product_id"]))
    only_metadata = sorted(set(metadata["product_id"]) - set(skus["product_id"]))
    if only_skus or only_metadata:
        raise DataError(
            "SKU file and metadata do not match one to one. "
            f"Missing in metadata: {only_skus[:10]}. Missing in SKU file: {only_metadata[:10]}."
        )

    joined = skus.merge(
        metadata[["product_id"] + METADATA_EXTRA], on="product_id", validate="one_to_one"
    )
    return joined, competitors


# ---------------------------------------------------------------------------
# 2. Consistency checks (DECISIONS.md section 3, assumption A8)
# ---------------------------------------------------------------------------
def run_checks(
    skus: pd.DataFrame, competitors: pd.DataFrame, cfg: dict = CONFIG
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Flag rows whose numbers contradict each other.

    Flagged SKUs keep their action later on but carry a "verify first" marker
    with the reason. Competitor scores outside the valid range are counted and
    clipped. Returns (skus, competitors, counts per check).
    """
    skus = skus.copy()
    competitors = competitors.copy()

    revenue = skus["channel_revenue_eur_12w"]
    units = skus["channel_units_sold_12w"]
    trend = skus["channel_sales_trend_12w_pct"]

    # Average selling price implied by revenue and units; empty where no units sold.
    implied_price = revenue / units.where(units > 0)
    price_gap = (implied_price / skus["price_eur"] - 1).abs()
    skus["implied_price_eur"] = implied_price.round(2)

    flags = pd.DataFrame(index=skus.index)
    flags["zero_revenue_trend"] = (revenue == 0) & (trend != cfg["check_trend_total_loss"])
    flags["collapse_with_revenue"] = (revenue > 0) & (trend <= cfg["check_trend_collapse"])
    flags["revenue_without_units"] = (revenue > 0) & (units == 0)
    flags["implied_price_gap"] = price_gap > cfg["check_price_tolerance"]

    labels = {k: v.format(tol=cfg["check_price_tolerance"]) for k, v in SKU_CHECKS.items()}
    skus["verify_first"] = flags.any(axis=1)
    skus["verify_reason"] = flags.apply(
        lambda row: "; ".join(labels[key] for key in SKU_CHECKS if row[key]), axis=1
    )

    score = competitors["signal_score_0_100"]
    out_of_range = (score < cfg["score_min"]) | (score > cfg["score_max"])
    competitors["signal_score_clipped"] = score.clip(cfg["score_min"], cfg["score_max"])

    counts = {key: int(flags[key].sum()) for key in SKU_CHECKS}
    counts["competitor_score_out_of_range"] = int(out_of_range.sum())
    counts["skus_to_verify"] = int(skus["verify_first"].sum())
    return skus, competitors, counts


# ---------------------------------------------------------------------------
# 3. Rules (DECISIONS.md section 2)
# ---------------------------------------------------------------------------
RULES = {
    "R1": {
        "name": "Fix availability",
        "action": "Restock",
        "impact_type": "Revenue at risk",
        "formula": "12-week channel revenue",
    },
    "R2": {
        "name": "Relist",
        "action": "Relist",
        "impact_type": "Incremental revenue",
        "formula": "Market revenue x average share - current revenue, floored at 0",
    },
    "R3": {
        "name": "Promote",
        "action": "Promote",
        "impact_type": "Incremental revenue",
        "formula": "Market revenue x average share - current revenue, floored at 0",
    },
    "R4": {
        "name": "Price review",
        "action": "Review price",
        "impact_type": "Extra revenue",
        "formula": "12-week channel revenue x 3% price increase at constant volume",
    },
}


def apply_rules(
    skus: pd.DataFrame, competitors: pd.DataFrame, cfg: dict = CONFIG
) -> tuple[pd.DataFrame, dict]:
    """Test every rule on every SKU, then pick one primary action per SKU.

    Adds to each SKU: its market share, every rule it triggers, the primary
    rule (first match in the precedence order, A9), the action, and the
    estimated impact with its formula. Returns (skus, context); context holds
    the figures computed from this data drop, such as the average share.
    """
    skus = skus.copy()
    context = {}

    revenue = skus["channel_revenue_eur_12w"]
    market = skus["market_revenue_eur_12w"]
    channel_trend = skus["channel_sales_trend_12w_pct"]
    market_trend = skus["market_sales_trend_12w_pct"]
    active = skus["status"] == cfg["status_active"]
    inactive = skus["status"] == cfg["status_inactive"]
    out_of_stock = skus["stock_status"] == cfg["stock_out"]
    low_stock = skus["stock_status"] == cfg["stock_low"]
    seasonal = skus["seasonality"] != cfg["season_evergreen"]

    # Our share of each product's market, and the overall average (A3).
    avg_share = revenue.sum() / market.sum()
    skus["share"] = revenue / market.where(market > 0)
    context["avg_share"] = avg_share
    # Revenue we would add by reaching the average share, never below zero (A7).
    skus["gap_to_avg_share_eur"] = (market * avg_share - revenue).clip(lower=0).round(2)

    # One column per rule: does the SKU trigger it, and what is the estimate.
    trigger = pd.DataFrame(index=skus.index)
    impact = pd.DataFrame(index=skus.index)

    # R1 Fix availability: active, out of stock, market still growing.
    trigger["R1"] = active & out_of_stock & (market_trend > cfg["r1_market_trend_min"])
    impact["R1"] = revenue

    # R2 Relist: inactive, but the market for the product is growing strongly.
    trigger["R2"] = inactive & (market_trend > cfg["r2_market_trend_min"])
    impact["R2"] = skus["gap_to_avg_share_eur"]

    # R3 Promote: active and available, market growing, but our share is weak.
    trigger["R3"] = (
        active
        & ~out_of_stock
        & (market_trend > cfg["r3_market_trend_min"])
        & (skus["share"] < cfg["r3_share_factor"] * avg_share)
    )
    impact["R3"] = skus["gap_to_avg_share_eur"]

    # R4 Price review: thin margin on a product that is growing here and in the market.
    trigger["R4"] = (
        active
        & (skus["channel_margin_pct"] < cfg["r4_margin_max"])
        & (channel_trend > cfg["r4_trend_min"])
        & (market_trend > cfg["r4_trend_min"])
    )
    impact["R4"] = revenue * cfg["r4_price_increase"]

    # --- further rules are added above this line ---

    # Primary action: the first rule that fires in the precedence order (A9).
    order = [rule for rule in cfg["precedence"] if rule in trigger]
    skus["rule"] = ""
    skus["impact_eur"] = float("nan")
    for rule in reversed(order):
        skus.loc[trigger[rule], "rule"] = rule
        skus.loc[trigger[rule], "impact_eur"] = impact.loc[trigger[rule], rule].round(2)
    skus["rules_triggered"] = trigger[order].apply(
        lambda row: ", ".join(rule for rule in order if row[rule]), axis=1
    )
    for field in ["name", "action", "impact_type", "formula"]:
        column = {"name": "rule_name", "formula": "impact_formula"}.get(field, field)
        skus[column] = skus["rule"].map(lambda rule: RULES[rule][field] if rule else "")

    context["triggered_counts"] = {rule: int(trigger[rule].sum()) for rule in order}
    return skus, context


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Category opportunity review")
    parser.add_argument("--input", default="data", help="folder with the three CSV files")
    parser.add_argument("--output", default="output", help="folder for the report and action list")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        skus, competitors = load_data(Path(args.input))
    except DataError as error:
        print(f"DATA ERROR, nothing was produced: {error}", file=sys.stderr)
        return 2

    skus, competitors, check_counts = run_checks(skus, competitors)

    data_date = competitors["observed_date"].max()
    print(f"Loaded {len(skus)} SKUs and {len(competitors)} competitor rows from {args.input}/")
    print(f"Data date {data_date}, run date {date.today().isoformat()}")
    print("Consistency checks:")
    for key, count in check_counts.items():
        print(f"  {key:32s} {count}")

    skus, context = apply_rules(skus, competitors)
    print("Rules (primary action per SKU):")
    flagged = skus[skus["rule"] != ""]
    for rule, group in flagged.groupby("rule"):
        print(
            f"  {rule} {RULES[rule]['name']:22s} {len(group):3d} SKUs"
            f"  EUR {group['impact_eur'].sum():>10,.0f}  {RULES[rule]['impact_type']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
