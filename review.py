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
    "stock_out": "out_of_stock",
    "stock_low": "low_stock",
    # Consistency checks (DECISIONS.md section 3)
    "check_trend_total_loss": -1.00,  # trend a SKU with zero revenue should show
    "check_trend_collapse": -0.90,    # revenue above zero with a trend at or below this
    "check_price_tolerance": 0.25,    # allowed gap between revenue / units and list price
    "score_min": 0,                   # competitor scores are clipped to this range (A6)
    "score_max": 100,
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
    missing, a product ID is duplicated, or the metadata does not match the
    SKU file one to one.
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
        frames[key] = frame

    skus, metadata, competitors = frames["skus"], frames["metadata"], frames["competitors"]

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
