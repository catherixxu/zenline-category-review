#!/usr/bin/env python3
"""Zenline category review: Beauty > Hair Coloration.

Usage:
    python review.py --input data --output output

Steps: load and validate -> consistency checks -> rules -> outputs.
Every rule and threshold is documented in docs/DECISIONS.md.
"""
from __future__ import annotations

import argparse
import base64
import html
import math
import sys
from datetime import datetime
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
    "r4b_price_gap": 0.20,            # R4b: note when our price differs from a competitor's by more
    "r5_revenue_quantile": 0.25,      # R5: revenue below this quantile of all SKUs, recomputed each run
    "r5_trend_max": 0.0,              # R5: channel and market trend must both be below this
    "watch_market_trend_min": 0.15,   # Watchlist: active, low stock, market trend above this
    "gap_competitor_trend_min": 0.0,  # Assortment gap: competitor trend of the segment above this
    # Order of recommendations in the outputs: sequence of execution (DECISIONS.md section 4)
    "report_order": ["R1", "R4", "R5", "R2", "R3"],
    # An estimate is shown as provisional when products with an open data point carry at least
    # this share of it
    "provisional_share_min": 0.10,
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
    "active_without_revenue": "Active product with no revenue in the period",
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
    flags["active_without_revenue"] = (skus["status"] == cfg["status_active"]) & (revenue == 0)

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
        "coordinate_with": "Supply chain: reorder and confirm the delivery date",
    },
    "R2": {
        "name": "Relist",
        "action": "Relist",
        "impact_type": "Incremental revenue",
        "formula": "Market revenue x average share - current revenue, floored at 0",
        "coordinate_with": "Supplier: confirm availability and terms",
    },
    "R3": {
        "name": "Promote",
        "action": "Promote",
        "impact_type": "Incremental revenue",
        "formula": "Market revenue x average share - current revenue, floored at 0",
        "coordinate_with": "Store operations: placement and promotion slot",
    },
    "R4": {
        "name": "Price review",
        "action": "Review price",
        "impact_type": "Extra revenue",
        "formula": "12-week channel revenue x 3% price increase at constant volume",
        "coordinate_with": "Pricing: check the price ladder and competitor prices",
    },
    "R5": {
        "name": "Delist or mark down",
        "action": "Delist",  # seasonal SKUs get "Mark down" instead (A5)
        "impact_type": "Revenue forgone",
        "formula": "12-week channel revenue; shelf space freed is listed separately",
        "coordinate_with": "Supplier and pricing: sell-through, returns and markdown level",
    },
}
ACTION_SEASONAL_R5 = "Mark down"
OWNER = "Category manager"  # every action sits within category management (B4)


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

    # R5 Delist or mark down: low revenue, shrinking here and in the market.
    # The cut-off is the bottom quartile of revenue across all SKUs in this drop (B2).
    revenue_cutoff = revenue.quantile(cfg["r5_revenue_quantile"])
    context["r5_revenue_cutoff"] = revenue_cutoff
    trigger["R5"] = (
        active
        & (revenue < revenue_cutoff)
        & (channel_trend < cfg["r5_trend_max"])
        & (market_trend < cfg["r5_trend_max"])
    )
    impact["R5"] = revenue

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
    # Seasonal SKUs are marked down, not delisted (A5). Shelf space only counts for R5.
    skus.loc[(skus["rule"] == "R5") & seasonal, "action"] = ACTION_SEASONAL_R5
    skus["shelf_freed_cm"] = skus["shelf_space_cm"].where(skus["rule"] == "R5")

    # Position of the SKU's recommendation in the outputs, and the other rules it triggers.
    position = {rule: number for number, rule in enumerate(cfg["report_order"], start=1)}
    skus["recommendation"] = skus["rule"].map(position)
    skus["also_qualifies_for"] = skus.apply(
        lambda row: "; ".join(
            f"{RULES[rule]['name']} ({rule})"
            for rule in row["rules_triggered"].split(", ")
            if rule and rule != row["rule"]
        ),
        axis=1,
    )

    # Watchlist, not a recommendation: could run out of stock while the market grows.
    skus["watchlist"] = active & low_stock & (market_trend > cfg["watch_market_trend_min"])

    context["triggered_counts"] = {rule: int(trigger[rule].sum()) for rule in order}

    skus = add_price_gap_notes(skus, competitors, cfg)
    skus = add_competitor_signals(skus, competitors, cfg)
    context["assortment_gaps"] = assortment_gaps(skus, competitors, cfg)
    segments = competitors.groupby(["subcategory", "shade_group"])["trend_score_12w_pct"].mean()
    context["competitor_segments"] = len(segments)
    context["competitor_segments_rising"] = int((segments > 0).sum())
    context["competitor_segments_single"] = int(
        (competitors.groupby(["subcategory", "shade_group"]).size() == 1).sum()
    )
    context["competitor_by_subcategory"] = (
        competitors.groupby("subcategory")
        .agg(products=("competitor_product_id", "count"), trend=("trend_score_12w_pct", "mean"))
        .to_dict("index")
    )
    return skus, context


def add_price_gap_notes(
    skus: pd.DataFrame, competitors: pd.DataFrame, cfg: dict = CONFIG
) -> pd.DataFrame:
    """R4b: note a price gap where a competitor row has exactly our product name.

    A secondary note only. It never changes the primary action (A6).
    """
    def clean(names: pd.Series) -> pd.Series:
        return names.str.strip().str.lower()

    ours = skus[["product_id", "price_eur"]].assign(name_key=clean(skus["product_name"]))
    theirs = competitors[["price_eur", "source"]].assign(
        name_key=clean(competitors["competitor_product_name"])
    )
    matches = ours.merge(theirs, on="name_key", suffixes=("", "_competitor"))
    matches["price_gap_pct"] = matches["price_eur"] / matches["price_eur_competitor"] - 1
    matches = matches[matches["price_gap_pct"].abs() > cfg["r4b_price_gap"]]
    # If one product matches several competitor rows, keep the widest gap.
    matches = matches.sort_values("price_gap_pct", key=abs, ascending=False)
    matches = matches.drop_duplicates("product_id").set_index("product_id")

    def describe(row: pd.Series) -> str:
        direction = "above" if row["price_gap_pct"] > 0 else "below"
        return (
            f"EUR {row['price_eur']:.2f} against EUR {row['price_eur_competitor']:.2f} "
            f"at {row['source']}, {abs(row['price_gap_pct']):.0%} {direction}"
        )

    notes = matches.apply(describe, axis=1) if len(matches) else pd.Series(dtype=str)
    skus["competitor_price_eur"] = skus["product_id"].map(matches["price_eur_competitor"])
    skus["competitor_source"] = skus["product_id"].map(matches["source"]).fillna("")
    skus["price_gap_pct"] = skus["product_id"].map(matches["price_gap_pct"]).round(3)
    skus["price_gap_note"] = skus["product_id"].map(notes).fillna("")
    return skus


# Competitor measures, each averaged over the competitor products of a match (A6)
COMPETITOR_MEASURES = {
    "competitor_products": ("competitor_product_id", "count"),
    "competitor_trend": ("trend_score_12w_pct", "mean"),
    "competitor_score": ("signal_score_clipped", "mean"),
    "competitor_avg_price_eur": ("price_eur", "mean"),
}
# Match levels, tried in this order: the first that finds competitor products is used
COMPETITOR_MATCH_LEVELS = [
    ("exact name", ["name_key"]),
    ("subcategory and shade", ["subcategory", "shade_group"]),
    ("shade only", ["shade_group"]),
]


def add_competitor_signals(
    skus: pd.DataFrame, competitors: pd.DataFrame, cfg: dict = CONFIG
) -> pd.DataFrame:
    """Give every SKU the best available competitor signal (A6).

    Match order: exact product name, then same subcategory and shade, then
    same shade. The signal is evidence beside the rules and never changes an
    action. A SKU is flagged when the competitor trend and its own market
    trend point in different directions, so the difference can be discussed.
    """
    def clean(names: pd.Series) -> pd.Series:
        return names.str.strip().str.lower()

    comp = competitors.assign(name_key=clean(competitors["competitor_product_name"]))
    out = skus.assign(name_key=clean(skus["product_name"]))
    out["competitor_match"] = ""
    for column in COMPETITOR_MEASURES:
        out[column] = float("nan")
    for label, keys in COMPETITOR_MATCH_LEVELS:
        found = out[keys].merge(
            comp.groupby(keys).agg(**COMPETITOR_MEASURES).reset_index(), on=keys, how="left"
        )
        found.index = out.index
        fill = (out["competitor_match"] == "") & found["competitor_products"].notna()
        for column in COMPETITOR_MEASURES:
            out.loc[fill, column] = found.loc[fill, column]
        out.loc[fill, "competitor_match"] = label
    out["competitor_trend_differs"] = (
        out["competitor_trend"] * out["market_sales_trend_12w_pct"]
    ) < 0
    return out.drop(columns="name_key")


def assortment_gaps(
    skus: pd.DataFrame, competitors: pd.DataFrame, cfg: dict = CONFIG
) -> pd.DataFrame:
    """Subcategory and shade combinations that competitors sell, with a rising
    competitor trend, where we have no active product. Not sized: a lead for
    supplier follow-up."""
    segments = (
        competitors.groupby(["subcategory", "shade_group"])
        .agg(**COMPETITOR_MEASURES)
        .reset_index()
    )
    active = skus[skus["status"] == cfg["status_active"]]
    covered = set(zip(active["subcategory"], active["shade_group"]))
    uncovered = [
        pair not in covered for pair in zip(segments["subcategory"], segments["shade_group"])
    ]
    gaps = segments[uncovered]
    gaps = gaps[gaps["competitor_trend"] > cfg["gap_competitor_trend_min"]]
    return gaps.sort_values(
        ["competitor_trend", "subcategory", "shade_group"], ascending=[False, True, True]
    ).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 4. Outputs
# ---------------------------------------------------------------------------
# Columns of the action list, in order. Evidence columns keep the names and
# values of the source files so that every figure can be traced back.
ACTION_LIST_COLUMNS = [
    "recommendation", "action", "rule", "rule_name", "also_qualifies_for",
    "product_id", "product_name", "brand", "subcategory", "shade_group",
    "seasonality", "supplier", "status", "stock_status",
    "estimated_impact_eur", "impact_type", "impact_formula", "shelf_freed_cm",
    "price_eur", "channel_revenue_eur_12w", "channel_units_sold_12w",
    "channel_margin_pct", "channel_sales_trend_12w_pct", "market_revenue_eur_12w",
    "market_sales_trend_12w_pct", "share_of_market", "shelf_space_cm",
    "price_gap_note", "competitor_match", "competitor_products", "competitor_trend",
    "competitor_score", "competitor_avg_price_eur", "competitor_trend_differs",
    "verify_first", "verify_reason", "watchlist",
    "owner", "coordinate_with", "data_date",
]


def flagged_in_order(skus: pd.DataFrame) -> pd.DataFrame:
    """SKUs with a primary action: by recommendation, then largest estimate first."""
    rows = skus[skus["rule"] != ""].copy()
    return rows.sort_values(
        ["recommendation", "impact_eur", "product_id"], ascending=[True, False, True]
    )


def summarise(skus: pd.DataFrame, context: dict, check_counts: dict, cfg: dict = CONFIG) -> dict:
    """The headline figures of a run. The console, the report and the tests all read these."""
    flagged = skus[skus["rule"] != ""]
    r5 = skus[skus["rule"] == "R5"]
    delist = r5["action"] == RULES["R5"]["action"]
    watch = skus[skus["watchlist"]]
    rules = {}
    for rule in cfg["report_order"]:
        group = skus[skus["rule"] == rule]
        rules[rule] = {
            "skus": len(group),
            "triggered": context["triggered_counts"][rule],
            "impact_eur": round(float(group["impact_eur"].sum()), 2),
            "verify_first": int(group["verify_first"].sum()),
            "verify_impact_eur": round(float(group.loc[group["verify_first"], "impact_eur"].sum()), 2),
            "competitor_differs": int(group["competitor_trend_differs"].sum()),
            "competitor_differs_impact_eur": round(
                float(group.loc[group["competitor_trend_differs"], "impact_eur"].sum()), 2
            ),
        }
    return {
        "sku_count": len(skus),
        "active_count": int((skus["status"] == cfg["status_active"]).sum()),
        "inactive_count": int((skus["status"] == cfg["status_inactive"]).sum()),
        "channel_revenue": float(skus["channel_revenue_eur_12w"].sum()),
        "avg_share": float(context["avg_share"]),
        "r5_revenue_cutoff": float(context["r5_revenue_cutoff"]),
        "rules": rules,
        "action_count": len(flagged),
        "verify_count": int(skus["verify_first"].sum()),
        "verify_with_action": int(flagged["verify_first"].sum()),
        "delist_count": int(delist.sum()),
        "markdown_count": int((~delist).sum()),
        "shelf_freed_now_cm": int(r5.loc[delist, "shelf_space_cm"].sum()),
        "shelf_freed_later_cm": int(r5.loc[~delist, "shelf_space_cm"].sum()),
        "shelf_needed_cm": int(skus.loc[skus["rule"] == "R2", "shelf_space_cm"].sum()),
        "price_gap_notes": int((skus["price_gap_note"] != "").sum()),
        "watchlist_count": len(watch),
        "watchlist_revenue": float(watch["channel_revenue_eur_12w"].sum()),
        "checks": dict(check_counts),
        "competitor_match_counts": {
            label: int((skus["competitor_match"] == label).sum())
            for label, _ in COMPETITOR_MATCH_LEVELS
        },
        "competitor_segments": context["competitor_segments"],
        "competitor_segments_rising": context["competitor_segments_rising"],
        "competitor_segments_single": context["competitor_segments_single"],
        "competitor_differs_all": int(skus["competitor_trend_differs"].sum()),
        "competitor_differs_with_action": int(flagged["competitor_trend_differs"].sum()),
        "assortment_gaps": context["assortment_gaps"].to_dict("records"),
        "competitor_by_subcategory": context["competitor_by_subcategory"],
    }


def build_action_list(
    skus: pd.DataFrame, data_date: str, cfg: dict = CONFIG
) -> pd.DataFrame:
    """One row per SKU with a primary action, in the order of the recommendations."""
    rows = flagged_in_order(skus)

    def yes_no(flags: pd.Series) -> pd.Series:
        return flags.map({True: "yes", False: "no"})

    rows["recommendation"] = rows["recommendation"].astype(int)
    rows["estimated_impact_eur"] = rows["impact_eur"]
    rows["share_of_market"] = rows["share"].round(4)
    rows["verify_first"] = yes_no(rows["verify_first"])
    rows["watchlist"] = yes_no(rows["watchlist"])
    rows["owner"] = OWNER
    rows["coordinate_with"] = rows["rule"].map(lambda rule: RULES[rule]["coordinate_with"])
    rows["competitor_products"] = rows["competitor_products"].astype(int)
    rows["competitor_trend"] = rows["competitor_trend"].round(3)
    rows["competitor_score"] = rows["competitor_score"].round(1)
    rows["competitor_avg_price_eur"] = rows["competitor_avg_price_eur"].round(2)
    rows["competitor_trend_differs"] = yes_no(rows["competitor_trend_differs"])
    rows["data_date"] = data_date
    return rows[ACTION_LIST_COLUMNS].reset_index(drop=True)


# ---------------------------------------------------------------------------
# 5. HTML report: one self-contained file with tabs, no external assets
# ---------------------------------------------------------------------------
REPORT_TITLE = "Zenline category review"
CATEGORY_NAME = "Hair Coloration"
AUTHOR_LINE = (
    "Prepared by Catherine Xu as a take-home exercise for Zenline AI, "
    "using the synthetic dataset provided."
)
METHOD_FILE = "assumptions_and_calculations.md"

STAGES = {
    "R1": "Fix the existing range",
    "R4": "Fix the existing range",
    "R5": "Change the range",
    "R2": "Change the range",
    "R3": "Grow",
}
KEYWORDS = {"R1": "Restock", "R4": "Price", "R5": "Delist", "R2": "Relist", "R3": "Promote"}
SHORT_LABELS = {  # column headers and filter buttons
    "R1": "Restock", "R4": "Review price", "R5": "Delist or mark down",
    "R2": "Relist", "R3": "Promote",
}
DECISIONS_ASKED = {  # what each action asks the decision maker for
    "R1": "Reorder now and confirm delivery dates.",
    "R4": "Agree a price test on these products.",
    "R5": "Approve the delist and markdown list.",
    "R2": "Approve the relist list.",
    "R3": "Agree placement and promotion slots.",
}
STOCK_LABELS = {"in_stock": "In stock", "low_stock": "Low stock", "out_of_stock": "Out of stock"}
STATUS_LABELS = {"aktiv": "Active", "inaktiv": "Inactive"}

# DECISIONS.md section 6: the question, and why the answer matters
OPEN_QUESTIONS = [
    (
        "What does the status \"inaktiv\" mean in practice?",
        "We read it as being phased out while the remaining stock sells down. The answer "
        "decides whether the relist candidates of action 4 are really off the shelf.",
    ),
    (
        "How is the sales trend calculated?",
        "All five actions use the trend as a direction. A different basis could move "
        "products across the thresholds.",
    ),
    (
        "Do inactive products still occupy the shelf space listed for them?",
        "If they do, relisting needs no new space and action 3 is not a precondition "
        "for action 4.",
    ),
    (
        "Is there a minimum revenue per product per week?",
        "It would replace the bottom-quarter cut-off used for delisting in action 3 with "
        "the retailer's own standard.",
    ),
    (
        "Where competitor and market trends differ for a product, which signal should lead?",
        "It decides how much weight the competitor view gets. Today it is shown beside each "
        "action and changes none.",
    ),
    (
        "Which pack sizes do the competitor prices refer to?",
        "The competitor data has no pack size. Competitor prices are therefore context only, "
        "and the two price gaps assume the same pack as ours.",
    ),
]


def esc(value) -> str:
    return html.escape(str(value))


def eur(value: float) -> str:
    return f"€{value:,.0f}"


def pct(value: float, signed: bool = False, digits: int = 0) -> str:
    """0.18 -> '18%'. With signed=True -> '+18%'. Uses a real minus sign."""
    if pd.isna(value):
        return "n/a"
    number = round(value * 100, digits) + 0.0  # + 0.0 turns -0.0 into 0.0
    sign = "+" if signed and number != 0 else ""
    return f"{number:{sign}.{digits}f}%".replace("-", "−")


def long_date(iso_date: str) -> str:
    parsed = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{parsed.day} {parsed:%B %Y}"


def check_flag(row: pd.Series) -> str:
    """Marker for a product with an open data point. Links to the open points tab."""
    if not row["verify_first"]:
        return ""
    return f'<a class="flag" href="#open" title="{esc(row["verify_reason"])}">! Check first</a>'


def match_label(row: pd.Series) -> str:
    """How the competitor signal of a product was found, and how many products are behind it."""
    count = int(row["competitor_products"])
    basis = {
        "exact name": "same product name",
        "subcategory and shade": "same segment",
        "shade only": "same shade",
    }[row["competitor_match"]]
    return f"{count} product{'' if count == 1 else 's'}, {basis}"


def differs_flag(row: pd.Series) -> str:
    """Marker for a product whose competitor trend and market trend point in different directions."""
    if not row["competitor_trend_differs"]:
        return ""
    tip = (
        f"Competitor trend {pct(row['competitor_trend'], signed=True)}, "
        f"market trend {pct(row['market_sales_trend_12w_pct'], signed=True)}"
    )
    return f'<a class="flag flag-alt" href="#open" title="{esc(tip)}">Competitor differs</a>'


def slug(rule: str) -> str:
    return KEYWORDS[rule].lower()


def recommendation_texts(skus: pd.DataFrame, summary: dict, cfg: dict = CONFIG) -> dict:
    """Driver, headline, estimate and reasoning for each action, in business language."""
    rules = summary["rules"]
    number = {rule: n for n, rule in enumerate(cfg["report_order"], start=1)}

    # Low-margin SKUs whose primary action is an earlier rule (B7)
    r4_elsewhere = skus[(skus["rule"] != "R4") & skus["rules_triggered"].str.contains("R4")]
    r4_note = ""
    if len(r4_elsewhere):
        where = " and ".join(str(n) for n in sorted({number[r] for r in r4_elsewhere["rule"]}))
        r4_note = (
            f" {len(r4_elsewhere)} more low-margin products are handled first under "
            f"actions {where}."
        )

    def check_note(rule: str) -> str:
        """Say how much of an estimate rests on products with an open data point."""
        info = rules[rule]
        if not info["verify_first"]:
            return ""
        return (
            f" {info['verify_first']} of them, carrying {eur(info['verify_impact_eur'])} of the "
            "estimate, have an open data point to clarify first."
        )

    texts = {
        "R1": {
            "driver": f"{rules['R1']['skus']} products in growing markets are out of stock",
            "title": f"Restock {rules['R1']['skus']} out-of-stock products",
            "figure_label": "12-week revenue at risk",
            "why": "These products are active and out of stock while their market is "
            "growing. This is revenue the range already earns, so it comes first.",
        },
        "R4": {
            "driver": f"{rules['R4']['skus']} growing products earn a margin below "
            f"{pct(cfg['r4_margin_max'])}",
            "title": f"Review the price of {rules['R4']['skus']} low-margin products",
            "figure_label": f"extra revenue from a {pct(cfg['r4_price_increase'])} price increase",
            "why": f"Margin is below {pct(cfg['r4_margin_max'])} while sales grow both in our "
            f"channel and in the market, which leaves room to test a higher price.{r4_note}",
        },
        "R5": {
            "driver": f"{rules['R5']['skus']} products with low and falling sales occupy "
            f"{summary['shelf_freed_now_cm'] + summary['shelf_freed_later_cm']} cm of shelf",
            "title": f"Delist {summary['delist_count']} products and mark down "
            f"{summary['markdown_count']} seasonal ones",
            "figure_label": f"revenue given up, for {summary['shelf_freed_now_cm']} cm of shelf "
            f"now and {summary['shelf_freed_later_cm']} cm later",
            "why": "Their revenue is in the bottom quarter of the range and sales are falling "
            "both in our channel and in the market. Seasonal products are marked down, not "
            "delisted.",
        },
        "R2": {
            "driver": f"{rules['R2']['skus']} inactive products sit in markets growing by more "
            f"than {pct(cfg['r2_market_trend_min'])}",
            "title": f"Relist {rules['R2']['skus']} inactive products",
            "figure_label": "incremental revenue",
            "why": f"These products are inactive, yet their market grew by more than "
            f"{pct(cfg['r2_market_trend_min'])}. Relisting them needs "
            f"{summary['shelf_needed_cm']} cm of shelf, and action 3 frees "
            f"{summary['shelf_freed_now_cm']} cm now.{check_note('R2')}",
        },
        "R3": {
            "driver": f"{rules['R3']['skus']} products in growing markets hold less than half "
            "our average share",
            "title": f"Promote {rules['R3']['skus']} products with strong demand and a weak share",
            "figure_label": "incremental revenue",
            "why": f"The market for these products grew by more than "
            f"{pct(cfg['r3_market_trend_min'])}, but our share is below half of our "
            f"{pct(summary['avg_share'], digits=1)} average.{check_note('R3')}",
        },
    }
    for rule, text in texts.items():
        info = rules[rule]
        text["figure"] = eur(info["impact_eur"])
        text["decision"] = DECISIONS_ASKED[rule]
        # An estimate is provisional when open data points carry a material part of it
        open_share = info["verify_impact_eur"] / info["impact_eur"] if info["impact_eur"] else 0
        text["provisional"] = open_share >= cfg["provisional_share_min"]
    return texts


def rule_method(rule: str, summary: dict, cfg: dict = CONFIG) -> dict:
    """How each action was found: rule, fields used, estimate formula. For the method file."""
    gap_formula = (
        f"Market revenue × our average share ({pct(summary['avg_share'], digits=1)}) − current "
        "revenue, never below zero. It is the revenue we would add by reaching our average share."
    )
    methods = {
        "R1": {
            "rule": "Active, out of stock, and the market for the product is growing "
            f"(market trend above {pct(cfg['r1_market_trend_min'])}).",
            "fields": ["status", "stock_status", "market_sales_trend_12w_pct",
                       "channel_revenue_eur_12w"],
            "estimate": "The 12-week channel revenue of each product: what it earned in the "
            "period and stands to lose while it is unavailable.",
        },
        "R4": {
            "rule": f"Active, margin below {pct(cfg['r4_margin_max'])}, and sales growing both "
            "in our channel and in the market.",
            "fields": ["status", "channel_margin_pct", "channel_sales_trend_12w_pct",
                       "market_sales_trend_12w_pct", "channel_revenue_eur_12w"],
            "estimate": f"12-week channel revenue × a {pct(cfg['r4_price_increase'])} price "
            "increase at unchanged volume. No price elasticity is applied.",
        },
        "R5": {
            "rule": "Active, 12-week revenue in the bottom quarter of all products (below "
            f"{eur(summary['r5_revenue_cutoff'])} in this data), and sales falling both in our "
            "channel and in the market. Evergreen products are delist candidates, seasonal "
            "products are marked down.",
            "fields": ["status", "channel_revenue_eur_12w", "channel_sales_trend_12w_pct",
                       "market_sales_trend_12w_pct", "seasonality", "shelf_space_cm"],
            "estimate": "Revenue given up is the 12-week channel revenue of each product. "
            "Shelf space is the shelf allocation listed for it.",
        },
        "R2": {
            "rule": "Inactive, and the market for the product grew by more than "
            f"{pct(cfg['r2_market_trend_min'])}.",
            "fields": ["status", "market_sales_trend_12w_pct", "market_revenue_eur_12w",
                       "channel_revenue_eur_12w", "shelf_space_cm"],
            "estimate": gap_formula,
        },
        "R3": {
            "rule": f"Active, not out of stock, market trend above "
            f"{pct(cfg['r3_market_trend_min'])}, and our share of the product's market below "
            f"half of our average (below {pct(cfg['r3_share_factor'] * summary['avg_share'], digits=1)}).",
            "fields": ["status", "stock_status", "market_sales_trend_12w_pct",
                       "channel_revenue_eur_12w", "market_revenue_eur_12w"],
            "estimate": gap_formula,
        },
    }
    return methods[rule]


def threshold_rows(summary: dict, cfg: dict = CONFIG) -> list[tuple[str, str]]:
    return [
        ("Our average share of the tracked market", pct(summary["avg_share"], digits=1)),
        ("Restock: market trend above", pct(cfg["r1_market_trend_min"])),
        ("Price review: margin below", pct(cfg["r4_margin_max"])),
        ("Price review: price increase used in the estimate", pct(cfg["r4_price_increase"])),
        ("Price gap note: price differs from the competitor by more than", pct(cfg["r4b_price_gap"])),
        ("Delist or mark down: revenue in the bottom", f"{pct(cfg['r5_revenue_quantile'])} of all "
         f"products, which is below {eur(summary['r5_revenue_cutoff'])} in this data"),
        ("Relist: market trend above", pct(cfg["r2_market_trend_min"], signed=True)),
        ("Promote: market trend above", pct(cfg["r3_market_trend_min"], signed=True)),
        ("Promote: share below", f"{pct(cfg['r3_share_factor'])} of the average share"),
        ("Watchlist: low stock and market trend above", pct(cfg["watch_market_trend_min"], signed=True)),
        ("Assortment gap: no active product and competitor trend above",
         pct(cfg["gap_competitor_trend_min"])),
        ("Estimate shown as provisional when open data points carry at least",
         f"{pct(cfg['provisional_share_min'])} of it"),
    ]


# --- Building blocks ---------------------------------------------------------
def row_tooltip(row: pd.Series) -> str:
    return (
        f"{row['product_id']} {row['product_name']}: revenue {eur(row['channel_revenue_eur_12w'])}, "
        f"margin {pct(row['channel_margin_pct'], digits=1)}, "
        f"trend in our channel {pct(row['channel_sales_trend_12w_pct'], signed=True)}, "
        f"market trend {pct(row['market_sales_trend_12w_pct'], signed=True)}, "
        f"competitor trend {pct(row['competitor_trend'], signed=True)} ({match_label(row)}), "
        f"share {pct(row['share'], digits=1)}"
    )


def product_cell(row: pd.Series) -> str:
    stock = STOCK_LABELS.get(row["stock_status"], row["stock_status"])
    return (
        f'<td><span class="sku-id">{esc(row["product_id"])}</span> {esc(row["product_name"])}'
        f'<br><span class="sub">{esc(row["subcategory"])}, {esc(stock.lower())}</span></td>'
    )


def bar_cell(value: float, top: float) -> str:
    """A euro value with a thin bar under it, sized against the largest value in the table."""
    return (
        f'<td class="num barcell"><strong>{eur(value)}</strong>'
        f'<i style="--r:{(value / top if top else 0):.4f}"></i></td>'
    )


def bars_html(items: list[dict]) -> str:
    """Horizontal bars. Each item: label (html), value, value_text, note (html), tooltip."""
    top = max(item["value"] for item in items) or 1
    parts = []
    for item in items:
        parts.append(
            f'<div class="bar-row" title="{esc(item.get("tooltip", ""))}">'
            f'<div class="bar-label">{item["label"]}</div>'
            f'<div class="bar-track"><div class="bar-fill {item.get("css", "")}" '
            f'style="--r:{item["value"] / top:.4f}"></div>'
            f'<span class="bar-value">{esc(item["value_text"])}</span></div>'
            f'<div class="bar-note">{item.get("note", "")}</div></div>'
        )
    return '<div class="bars">' + "".join(parts) + "</div>"


def product_bars(rows: pd.DataFrame, note) -> str:
    """One bar per product, sized by its estimate. `note` builds the text beside the bar."""
    return bars_html([
        {
            "label": f'<span class="sku-id">{esc(row["product_id"])}</span> {esc(row["product_name"])}',
            "value": row["impact_eur"],
            "value_text": eur(row["impact_eur"]),
            "note": f"{esc(note(row))} {check_flag(row)}",
            "tooltip": row_tooltip(row),
        }
        for _, row in rows.iterrows()
    ])


def trend_compare_html(rows: pd.DataFrame) -> str:
    """One row per product: its market trend and its competitor trend on a shared scale."""
    market_col = "market_sales_trend_12w_pct"
    low = math.floor(min(rows[market_col].min(), rows["competitor_trend"].min(), 0) * 10) / 10
    high = math.ceil(max(rows[market_col].max(), rows["competitor_trend"].max(), 0) * 10) / 10

    def position(value: float) -> float:
        return (value - low) / (high - low) * 100

    parts = [
        '<p class="legend"><span><i class="sw sw-market sw-dot"></i>Market trend of the product</span>'
        '<span><i class="sw sw-now sw-dot"></i>Competitor trend, best available match</span></p>',
        '<div class="bar-row"><div></div><div class="cmp-axis">'
        f'<span style="left:0%">{pct(low, signed=True)}</span>'
        f'<span style="left:{position(0):.1f}%">0%</span>'
        f'<span style="left:100%">{pct(high, signed=True)}</span></div><div></div></div>',
    ]
    for _, row in rows.iterrows():
        market, competitor = position(row[market_col]), position(row["competitor_trend"])
        parts.append(
            f'<div class="bar-row" title="{esc(row_tooltip(row))}">'
            f'<div class="bar-label"><span class="sku-id">{esc(row["product_id"])}</span> '
            f'{esc(row["product_name"])}</div>'
            '<div class="cmp-track">'
            f'<i class="zero" style="left:{position(0):.1f}%"></i>'
            f'<i class="link" style="left:{min(market, competitor):.1f}%;width:{abs(market - competitor):.1f}%"></i>'
            f'<i class="dot dot-m" style="left:{market:.1f}%"></i>'
            f'<i class="dot dot-c" style="left:{competitor:.1f}%"></i></div>'
            f'<div class="bar-note">Market {pct(row[market_col], signed=True)}, competitors '
            f'{pct(row["competitor_trend"], signed=True)} {differs_flag(row)}'
            f'<br><span class="sub">{esc(match_label(row))}</span></div></div>'
        )
    return '<div class="bars">' + "".join(parts) + "</div>"


def competitor_learning(number: int, rule: str, group: pd.DataFrame, data_date: str) -> str:
    """The competitor view of an action: does it point the same way as the market trend?"""
    count = len(group)
    differs = int(group["competitor_trend_differs"].sum())
    single = int((group["competitor_products"] == 1).sum())
    shade_only = int((group["competitor_match"] == "shade only").sum())
    points = []
    if rule == "R5" and differs == count:
        title = (
            f"Competitors are growing in the segments of all {count} products, so the decline "
            "is specific to these products"
        )
        points.append(
            "These products are falling in our channel and in their own market, while "
            "competitor products in the same subcategory and shade are rising. That argues for "
            "replacing them within the segment, not for leaving the segment. It is listed "
            'under <a href="#open">Open points to clarify</a>.'
        )
    elif differs == 0:
        title = f"Competitor data points the same way as the market trend for all {count} products"
    else:
        title = (
            f"Competitor data agrees with the market trend for {count - differs} of the {count} "
            f"products and differs for {differs}"
        )
        points.append(
            f"The {differs} products where the two differ keep their action. They are listed "
            'under <a href="#open">Open points to clarify</a>, to discuss with the retailer.'
        )
    thin = f"{single} of the {count} competitor signals rest on a single competitor product"
    if shade_only:
        thin += (
            f", and {shade_only} are matched by shade only because no competitor product shares "
            "the subcategory"
        )
    points.append(thin + ". Read them as a prompt to look closer, not as proof.")
    return learning(number, title, trend_compare_html(group), points, data_date)


def shelf_html(summary: dict) -> str:
    """Shelf space freed by delisting against shelf space needed for relisting."""
    now, later, needed = (
        summary["shelf_freed_now_cm"], summary["shelf_freed_later_cm"], summary["shelf_needed_cm"]
    )
    scale = max(now + later, needed) or 1
    return (
        '<div class="shelf">'
        '<p class="legend">'
        f'<span><i class="sw sw-now"></i>Freed now by delisting: {now} cm</span>'
        f'<span><i class="sw sw-later"></i>Freed after markdown sell-through: {later} cm</span>'
        f'<span><i class="sw sw-need"></i>Needed for relisting: {needed} cm</span></p>'
        '<div class="shelf-row"><div class="shelf-label">Action 3 frees</div><div class="shelf-track">'
        f'<div class="seg sw-now" style="width:{now / scale * 88:.1f}%" title="Freed now: {now} cm"></div>'
        f'<div class="seg sw-later" style="width:{later / scale * 88:.1f}%" title="Freed later: {later} cm"></div>'
        f'<span class="bar-value">{now + later} cm</span></div></div>'
        '<div class="shelf-row"><div class="shelf-label">Action 4 needs</div><div class="shelf-track">'
        f'<div class="seg sw-need" style="width:{needed / scale * 88:.1f}%" title="Needed: {needed} cm"></div>'
        f'<span class="bar-value">{needed} cm</span></div></div>'
        "</div>"
    )


def scatter_svg(skus: pd.DataFrame, summary: dict, cfg: dict = CONFIG) -> str:
    """Market trend against our share, for active products that are not out of stock."""
    pool = skus[
        (skus["status"] == cfg["status_active"]) & (skus["stock_status"] != cfg["stock_out"])
    ]
    promote = pool[pool["rule"] == "R3"]
    others = pool[pool["rule"] != "R3"]
    trend_col = "market_sales_trend_12w_pct"

    width, height = 860, 420
    left, right, top, bottom = 48, 172, 26, 46
    x_min = math.floor(pool[trend_col].min() * 10) / 10
    x_max = math.ceil(pool[trend_col].max() * 10) / 10
    y_max = math.ceil(pool["share"].max() * 20) / 20

    def x(value: float) -> float:
        return left + (value - x_min) / (x_max - x_min) * (width - left - right)

    def y(value: float) -> float:
        return top + (1 - value / y_max) * (height - top - bottom)

    avg, half = summary["avg_share"], cfg["r3_share_factor"] * summary["avg_share"]
    trend_min = cfg["r3_market_trend_min"]
    plot_right = width - right
    out = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Market trend against our '
        'share for active products in stock. Promote candidates sit in the lower right.">'
    ]
    # Zone the rule selects: growing market, weak share
    out.append(
        f'<rect x="{x(trend_min):.1f}" y="{y(half):.1f}" width="{plot_right - x(trend_min):.1f}" '
        f'height="{y(0) - y(half):.1f}" class="zone"/>'
    )
    for step in range(0, int(round(y_max * 100)) + 1, 5):
        yy = y(step / 100)
        out.append(f'<line x1="{left}" x2="{plot_right}" y1="{yy:.1f}" y2="{yy:.1f}" class="grid"/>')
        out.append(f'<text x="{left - 8}" y="{yy + 4:.1f}" text-anchor="end">{step}%</text>')
    for step in range(int(round(x_min * 100)), int(round(x_max * 100)) + 1, 20):
        out.append(
            f'<text x="{x(step / 100):.1f}" y="{height - bottom + 18}" text-anchor="middle">'
            f'{pct(step / 100, signed=True)}</text>'
        )
    out.append(f'<text x="{left}" y="12" class="axis-title">Our share of the product\'s market</text>')
    out.append(
        f'<text x="{plot_right}" y="{height - 6}" text-anchor="end" class="axis-title">'
        "Market sales trend, 12 weeks</text>"
    )
    # Reference lines with their labels in the right margin
    for value, label in [(avg, "Average share"), (half, "Half the average")]:
        out.append(
            f'<line x1="{left}" x2="{plot_right}" y1="{y(value):.1f}" y2="{y(value):.1f}" class="ref"/>'
        )
        out.append(
            f'<text x="{plot_right + 8}" y="{y(value) + 4:.1f}" class="ref-label">'
            f'{label} {pct(value, digits=1)}</text>'
        )
    out.append(
        f'<line x1="{x(trend_min):.1f}" x2="{x(trend_min):.1f}" y1="{top}" y2="{y(0):.1f}" class="ref"/>'
    )
    out.append(
        f'<text x="{x(trend_min) + 6:.1f}" y="{top + 10}" class="ref-label">'
        f'Market growth above {pct(trend_min, signed=True)}</text>'
    )

    def dot(row: pd.Series, css: str, radius: float) -> str:
        tip = (
            f"{row['product_id']} {row['product_name']}: share {pct(row['share'], digits=1)}, "
            f"market trend {pct(row[trend_col], signed=True)}, "
            f"revenue {eur(row['channel_revenue_eur_12w'])}"
        )
        return (
            f'<circle cx="{x(row[trend_col]):.1f}" cy="{y(row["share"]):.1f}" r="{radius}" '
            f'class="{css}"><title>{esc(tip)}</title></circle>'
        )

    out.extend(dot(row, "dot-other", 3.5) for _, row in others.iterrows())
    out.extend(dot(row, "dot-pick", 5.5) for _, row in promote.iterrows())
    out.append("</svg>")
    legend = (
        '<p class="legend">'
        f'<span><i class="sw sw-now sw-dot"></i>Promote candidates ({len(promote)})</span>'
        f'<span><i class="sw sw-other sw-dot"></i>Other active products in stock ({len(others)})</span>'
        "</p>"
    )
    return f'<div class="chart">{legend}{"".join(out)}</div>'


def margin_strip_svg(skus: pd.DataFrame, median: float, cfg: dict = CONFIG) -> str:
    """Every active product placed by its margin; price review candidates highlighted."""
    active = skus[skus["status"] == cfg["status_active"]].sort_values("channel_margin_pct")
    picked = active["rule"] == "R4"
    col = "channel_margin_pct"
    width, left, right, bin_width, step = 860, 24, 24, 0.008, 9
    lo = math.floor(active[col].min() * 20) / 20
    hi = math.ceil(active[col].max() * 20) / 20

    def x(value: float) -> float:
        return left + (value - lo) / (hi - lo) * (width - left - right)

    # Stack dots that fall into the same narrow margin band
    level, levels = {}, []
    for value in active[col]:
        key = int((value - lo) / bin_width)
        levels.append(level.get(key, 0))
        level[key] = level.get(key, 0) + 1
    base = 44 + max(levels) * step
    height = base + 34
    out = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Margin of every active '
        'product. Price review candidates sit below the threshold.">'
    ]
    out.append(f'<line x1="{left}" x2="{width - right}" y1="{base + 8}" y2="{base + 8}" class="ref"/>')
    for tick in range(int(round(lo * 100)), int(round(hi * 100)) + 1, 10):
        out.append(f'<text x="{x(tick / 100):.1f}" y="{base + 26}" text-anchor="middle">{tick}%</text>')
    for value, label, anchor in [
        (cfg["r4_margin_max"], f"Price review below {pct(cfg['r4_margin_max'])}", "start"),
        (median, f"Median of active products {pct(median, digits=1)}", "start"),
    ]:
        out.append(f'<line x1="{x(value):.1f}" x2="{x(value):.1f}" y1="18" y2="{base + 8}" class="ref"/>')
        out.append(
            f'<text x="{x(value) + 6:.1f}" y="14" text-anchor="{anchor}" class="ref-label">{esc(label)}</text>'
        )
    for (_, row), stacked, is_picked in zip(active.iterrows(), levels, picked):
        tip = (
            f"{row['product_id']} {row['product_name']}: margin {pct(row[col], digits=1)}, "
            f"revenue {eur(row['channel_revenue_eur_12w'])}"
        )
        css, radius = ("dot-pick", 5) if is_picked else ("dot-other", 3.5)
        out.append(
            f'<circle cx="{x(row[col]):.1f}" cy="{base - stacked * step}" r="{radius}" class="{css}">'
            f"<title>{esc(tip)}</title></circle>"
        )
    out.append("</svg>")
    legend = (
        '<p class="legend">'
        f'<span><i class="sw sw-now sw-dot"></i>Price review candidates ({int(picked.sum())})</span>'
        f'<span><i class="sw sw-other sw-dot"></i>Other active products ({int((~picked).sum())})</span>'
        "</p>"
    )
    return f'<div class="chart">{legend}{"".join(out)}</div>'


def price_gap_table(skus: pd.DataFrame) -> str:
    rows = skus[skus["price_gap_note"] != ""]
    body = "".join(
        f"<tr><td><span class='sku-id'>{esc(row['product_id'])}</span> {esc(row['product_name'])}</td>"
        f"<td class='num'>€{row['price_eur']:.2f}</td>"
        f"<td class='num'>€{row['competitor_price_eur']:.2f}</td>"
        f"<td>{esc(row['competitor_source'])}</td>"
        f"<td class='num'>{pct(row['price_gap_pct'], signed=True)}</td>"
        f"<td>{int(row['recommendation'])}. {esc(row['action'])}</td></tr>"
        for _, row in rows.iterrows()
    )
    return (
        '<div class="table-wrap"><table><thead><tr><th>Product</th><th class="num">Our price</th>'
        '<th class="num">Competitor price</th><th>Seen at</th><th class="num">Gap</th>'
        "<th>Its own action</th></tr></thead><tbody>" + body + "</tbody></table></div>"
    )


def subcategory_table(skus: pd.DataFrame, summary: dict, cfg: dict = CONFIG) -> str:
    """Where the actions sit: one row per subcategory, with the competitor trend beside it."""
    header = "".join(
        f'<th class="num wrap">{n}. {esc(SHORT_LABELS[rule])}</th>'
        for n, rule in enumerate(cfg["report_order"], start=1)
    )
    body = []
    grouped = skus.groupby("subcategory")
    order = grouped["channel_revenue_eur_12w"].sum().sort_values(ascending=False).index
    for name in order:
        group = grouped.get_group(name)
        revenue, market = group["channel_revenue_eur_12w"].sum(), group["market_revenue_eur_12w"].sum()
        competitor = summary["competitor_by_subcategory"].get(name)
        counts = "".join(
            f'<td class="num">{int((group["rule"] == rule).sum()) or "–"}</td>'
            for rule in cfg["report_order"]
        )
        body.append(
            f"<tr><td class='nowrap'>{esc(name)}</td><td class='num'>{len(group)}</td>"
            f"<td class='num'>{eur(revenue)}</td><td class='num'>{pct(revenue / market, digits=1)}</td>"
            f"<td class='num'>{pct(competitor['trend'], signed=True) if competitor else '–'}"
            f"<br><span class='sub'>{competitor['products'] if competitor else 0} products</span></td>"
            f"{counts}</tr>"
        )
    return (
        '<div class="table-wrap"><table><thead><tr><th>Subcategory</th><th class="num">Products</th>'
        '<th class="num wrap">Revenue, 12 weeks</th><th class="num wrap">Share of market</th>'
        '<th class="num wrap">Competitor trend</th>'
        f"{header}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"
    )


def learning(number: int, title: str, body: str, points: list[str], data_date: str) -> str:
    """One learning: the takeaway as the title, the exhibit, then the supporting points."""
    items = "".join(f"<li>{point}</li>" for point in points if point)
    return (
        f'<section class="learning"><p class="exhibit-no">Exhibit {number}</p>'
        f"<h3>{esc(title)}</h3>{body}"
        f'<ul class="plain">{items}</ul>'
        f'<p class="source">Source: sales and market data provided, 12 weeks to {long_date(data_date)}.</p>'
        "</section>"
    )


def span_text(values: pd.Series, signed: bool = False, digits: int = 0) -> str:
    """'21% to 53%' for a set of values."""
    return f"{pct(values.min(), signed, digits)} to {pct(values.max(), signed, digits)}"


# --- The product table of each action ------------------------------------------
def action_products_table(rule: str, rows: pd.DataFrame, cfg: dict = CONFIG) -> str:
    """The products to act on, with the few figures that explain why."""
    number = {r: n for n, r in enumerate(cfg["report_order"], start=1)}
    top = rows["impact_eur"].max() or 1

    def trend(column: str):
        return lambda row: f'<td class="num">{pct(row[column], signed=True)}</td>'

    def money(column: str):
        return lambda row: f'<td class="num">{eur(row[column])}</td>'

    ours, market = trend("channel_sales_trend_12w_pct"), trend("market_sales_trend_12w_pct")
    estimate = lambda row: bar_cell(row["impact_eur"], top)  # noqa: E731
    competitor = ("Competitor trend", lambda row: (
        f'<td class="num">{pct(row["competitor_trend"], signed=True)}'
        f'<br><span class="sub">{esc(match_label(row))}</span></td>'
    ))
    columns = {
        "R1": [
            ("Market trend", market), competitor, ("Our sales trend", ours),
            ("Revenue at risk", estimate),
        ],
        "R4": [
            ("Margin", lambda row: f'<td class="num">{pct(row["channel_margin_pct"], digits=1)}</td>'),
            ("Our sales trend", ours), ("Market trend", market), competitor,
            ("Our price", lambda row: f'<td class="num">€{row["price_eur"]:.2f}</td>'),
            ("Competitor price, average", lambda row: (
                f'<td class="num">€{row["competitor_avg_price_eur"]:.2f}</td>'
            )),
            ("Estimate", estimate),
        ],
        "R5": [
            ("Action", lambda row: f'<td class="nowrap">{esc(row["action"])}</td>'),
            ("Our sales trend", ours), ("Market trend", market), competitor,
            ("Shelf freed", lambda row: f'<td class="num">{row["shelf_space_cm"]} cm</td>'),
            ("Revenue given up", estimate),
        ],
        "R2": [
            ("Market trend", market), competitor,
            ("Market revenue, 12 weeks", money("market_revenue_eur_12w")),
            ("Our revenue, 12 weeks", money("channel_revenue_eur_12w")),
            ("Shelf needed", lambda row: f'<td class="num">{row["shelf_space_cm"]} cm</td>'),
            ("Estimate", estimate),
        ],
        "R3": [
            ("Our share", lambda row: f'<td class="num">{pct(row["share"], digits=1)}</td>'),
            ("Market trend", market), competitor,
            ("Our revenue, 12 weeks", money("channel_revenue_eur_12w")), ("Estimate", estimate),
        ],
    }[rule]

    head = "<th>Product</th>" + "".join(
        f'<th class="{"" if name == "Action" else "num wrap"}">{name}</th>' for name, _ in columns
    ) + "<th>Note</th>"
    body = []
    for _, row in rows.iterrows():
        notes = [check_flag(row)] if row["verify_first"] else []
        if row["competitor_trend_differs"]:
            notes.append(differs_flag(row))
        if row["price_gap_note"]:
            notes.append(f'Price gap: {esc(row["price_gap_note"].replace("EUR ", "€"))}')
        for other in row["rules_triggered"].split(", "):
            if other != row["rule"]:
                notes.append(f"Also a candidate for action {number[other]}")
        if row["watchlist"]:
            notes.append("Low on stock")
        cells = "".join(cell(row) for _, cell in columns)
        body.append(
            f'<tr title="{esc(row_tooltip(row))}">{product_cell(row)}{cells}'
            f'<td>{"<br>".join(notes)}</td></tr>'
        )
    return (
        f'<div class="table-wrap"><table class="action-products"><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table></div>"
    )


def watchlist_section(skus: pd.DataFrame, summary: dict, cfg: dict = CONFIG) -> str:
    watch = skus[skus["watchlist"]].sort_values("channel_revenue_eur_12w", ascending=False)
    rows = "".join(
        f'<tr title="{esc(row_tooltip(row))}">{product_cell(row)}'
        f"<td class='num'>{pct(row['market_sales_trend_12w_pct'], signed=True)}</td>"
        f"<td class='num'>{pct(row['competitor_trend'], signed=True)}"
        f"<br><span class='sub'>{esc(match_label(row))}</span></td>"
        f"<td class='num'>{pct(row['channel_sales_trend_12w_pct'], signed=True)}</td>"
        f"<td class='num'>{eur(row['channel_revenue_eur_12w'])}</td>"
        f"<td>{esc(str(int(row['recommendation'])) + '. ' + row['action']) if row['rule'] else 'None yet'}</td></tr>"
        for _, row in watch.iterrows()
    )
    return (
        "<h3>Watchlist: low on stock in growing markets</h3>"
        f"<p>No action yet. These {summary['watchlist_count']} active products are low on stock "
        f"while their market grows by more than {pct(cfg['watch_market_trend_min'])}. Together "
        f"they earned {eur(summary['watchlist_revenue'])} in 12 weeks, so they are the next "
        "restock candidates if stock runs out.</p>"
        '<div class="table-wrap"><table><thead><tr><th>Product</th><th class="num wrap">Market trend</th>'
        '<th class="num wrap">Competitor trend</th>'
        '<th class="num wrap">Our sales trend</th><th class="num wrap">Revenue, 12 weeks</th>'
        f"<th>Current action</th></tr></thead><tbody>{rows}</tbody></table></div>"
    )


def assortment_gaps_section(summary: dict) -> str:
    gaps = summary["assortment_gaps"]
    if not gaps:
        return ""
    rows = "".join(
        f"<tr><td>{esc(gap['subcategory'])}</td><td>{esc(gap['shade_group'])}</td>"
        f"<td class='num'>{gap['competitor_products']}</td>"
        f"<td class='num'>{pct(gap['competitor_trend'], signed=True)}</td>"
        f"<td class='num'>{gap['competitor_score']:.0f}</td>"
        f"<td class='num'>€{gap['competitor_avg_price_eur']:.2f}</td></tr>"
        for gap in gaps
    )
    return (
        "<h3>Assortment gaps: rising segments without an active product</h3>"
        f"<p>No action yet. Competitors sell in these {len(gaps)} combinations of subcategory "
        "and shade, their trend is rising, and we have no active product there. They are leads "
        "for supplier follow-up and are not sized. Most rest on a single competitor product, "
        "and the competitor prices are for unknown pack sizes.</p>"
        '<div class="table-wrap"><table><thead><tr><th>Subcategory</th><th>Shade</th>'
        '<th class="num wrap">Competitor products</th><th class="num wrap">Competitor trend</th>'
        '<th class="num wrap">Popularity score, 0 to 100</th>'
        '<th class="num wrap">Competitor price, average</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
    )


# --- What we learned, per action ---------------------------------------------
def learnings_html(
    rule: str, skus: pd.DataFrame, summary: dict, data_date: str, cfg: dict = CONFIG
) -> str:
    """Two learnings per action. Every figure in the wording is computed from the data."""
    rules = summary["rules"]
    group = flagged_in_order(skus)
    group = group[group["rule"] == rule]
    info = rules[rule]
    active = skus[skus["status"] == cfg["status_active"]]
    market = "market_sales_trend_12w_pct"

    def market_note(row: pd.Series) -> str:
        return f"Market {pct(row[market], signed=True)}"

    if rule == "R1":
        lead_count = min(3, len(group))
        lead_share = group["impact_eur"].head(lead_count).sum() / info["impact_eur"]
        points = [
            f"All {len(group)} products sit in growing markets, with market sales trends of "
            f"{span_text(group[market], signed=True)}.",
        ]
        for _, row in group[group["price_gap_note"] != ""].iterrows():
            points.append(
                f"{esc(row['product_id'])} {esc(row['product_name'])} is also priced at "
                f"{esc(row['price_gap_note'].replace('EUR ', '€'))}. Restock it first, then "
                "review its price under action 2."
            )
        first = learning(
            1, f"{lead_count} products carry {pct(lead_share)} of the revenue at risk",
            product_bars(group, market_note), points, data_date,
        )
        second = learning(
            2,
            f"Low stock is the next risk: {summary['watchlist_count']} more products with "
            f"{eur(summary['watchlist_revenue'])} of revenue",
            bars_html([
                {"label": f"Out of stock now ({info['skus']} products)", "value": info["impact_eur"],
                 "value_text": eur(info["impact_eur"]), "note": "Action 1"},
                {"label": f"Low on stock, market growing ({summary['watchlist_count']} products)",
                 "value": summary["watchlist_revenue"], "css": "fill-light",
                 "value_text": eur(summary["watchlist_revenue"]), "note": "Watchlist"},
            ]),
            ["The low-stock products are listed in the watchlist. None needs action yet, but "
             "they hold more revenue than the products already out of stock."],
            data_date,
        )
        return first + second + competitor_learning(3, rule, group, data_date)

    if rule == "R4":
        median = active["channel_margin_pct"].median()
        elsewhere = skus[(skus["rule"] != "R4") & skus["rules_triggered"].str.contains("R4")]
        gaps = skus[skus["price_gap_note"] != ""]
        first = learning(
            1,
            f"The {len(group)} products earn margins of "
            f"{span_text(group['channel_margin_pct'], digits=1)}, against a median of "
            f"{pct(median, digits=1)}",
            margin_strip_svg(skus, median, cfg),
            [
                f"All {len(group)} are growing both in our channel and in the market, so demand "
                "is not the constraint.",
                f"{len(elsewhere)} more products meet the same conditions but are restocked or "
                "promoted first. Their price can be reviewed in the same round."
                if len(elsewhere) else "",
            ],
            data_date,
        )
        second = ""
        if len(gaps):
            gap_list = " and ".join(pct(v, signed=True) for v in gaps["price_gap_pct"])
            second = learning(
                2, f"Where a competitor lists the same product name, the price gaps are large: {gap_list}",
                price_gap_table(skus),
                [
                    f"Only {len(gaps)} products have an exact match in the competitor data, so "
                    "this is a signal to follow up, not a price benchmark.",
                    "The competitor data does not state pack size. The gaps hold only if the "
                    "competitor sells the same pack as ours.",
                    "Both products have their own action. The price gap is a note for the "
                    "price review.",
                    "For the candidates above, the average competitor price of the matched "
                    "products is shown as context only, for the same reason.",
                ],
                data_date,
            )
        return first + second + competitor_learning(3 if second else 2, rule, group, data_date)

    if rule == "R5":
        shelf = group["shelf_space_cm"].sum()
        shelf_share = shelf / active["shelf_space_cm"].sum()
        revenue_share = info["impact_eur"] / active["channel_revenue_eur_12w"].sum()
        first = learning(
            1,
            f"These {len(group)} products hold {pct(shelf_share, digits=1)} of the shelf and bring "
            f"{pct(revenue_share, digits=1)} of the revenue",
            bars_html([
                {"label": "Share of shelf space, active products", "value": shelf_share,
                 "value_text": pct(shelf_share, digits=1), "note": f"{shelf} cm"},
                {"label": "Share of revenue, active products", "value": revenue_share,
                 "css": "fill-light", "value_text": pct(revenue_share, digits=1),
                 "note": eur(info["impact_eur"])},
            ]),
            [
                "Sales are falling both in our channel and in the market for every one of them.",
                f"{summary['markdown_count']} are seasonal. They are marked down instead of "
                "delisted, and their shelf space frees up once they have sold through.",
            ],
            data_date,
        )
        second = learning(
            2,
            f"Delisting frees {summary['shelf_freed_now_cm']} cm now and "
            f"{summary['shelf_freed_later_cm']} cm later, against {summary['shelf_needed_cm']} cm "
            "needed for relisting",
            shelf_html(summary),
            ["Actions 3 and 4 work as a pair. This holds only if inactive products no longer "
             'occupy their shelf space, which is an <a href="#open">open point</a>.'],
            data_date,
        )
        return first + second + competitor_learning(3, rule, group, data_date)

    if rule == "R2":
        inactive = skus[skus["status"] == cfg["status_inactive"]]
        selling = inactive[inactive["channel_revenue_eur_12w"] > 0]
        points = [
            f"{len(selling)} of the {len(inactive)} inactive products still recorded sales in "
            f"the period, {eur(selling['channel_revenue_eur_12w'].sum())} in total.",
        ]
        at_average = int((group["impact_eur"] == 0).sum())
        if at_average:
            points.append(
                f"{at_average} of the {len(group)} candidates already sell at or above our "
                "average share while inactive, so their estimate is zero. Whether they are "
                'really off the shelf is an <a href="#open">open point</a>.'
            )
        if info["verify_first"]:
            points.append(
                f"{info['verify_first']} products carry {eur(info['verify_impact_eur'])} of the "
                f"{eur(info['impact_eur'])} estimate and have an "
                '<a href="#open">open data point</a>, so the estimate is provisional.'
            )
        for _, row in group[group["price_gap_note"] != ""].iterrows():
            points.append(
                f"{esc(row['product_id'])} {esc(row['product_name'])} was priced at "
                f"{esc(row['price_gap_note'].replace('EUR ', '€'))}. Check the price before relisting."
            )
        first = learning(
            1,
            f"Inactive does not mean no demand: the {len(group)} candidates sit in markets "
            f"growing by {span_text(group[market])}",
            product_bars(group, market_note), points, data_date,
        )
        second = learning(
            2,
            f"Relisting needs {summary['shelf_needed_cm']} cm of shelf, and delisting frees "
            f"{summary['shelf_freed_now_cm']} cm now",
            shelf_html(summary),
            ["The remaining space becomes available once the marked-down products of action 3 "
             "have sold through."],
            data_date,
        )
        return first + second + competitor_learning(3, rule, group, data_date)

    # R3
    points = [
        f"Our average share is {pct(summary['avg_share'], digits=1)}. These products hold "
        f"{span_text(group['share'], digits=1)}.",
    ]
    second_points = []
    if info["verify_first"]:
        second_points.append(
            f"{info['verify_first']} of the {len(group)} have an "
            '<a href="#open">open data point</a>: they recorded no sales, or sales that do not '
            "add up, while being listed. That points to a listing or availability problem to fix "
            "before promoting."
        )
    first = learning(
        1,
        f"{len(group)} products sit in growing markets with less than half our average share",
        scatter_svg(skus, summary, cfg), points, data_date,
    )
    second = learning(
        2, f"Reaching our average share on these products would add {eur(info['impact_eur'])}",
        product_bars(group, lambda row: f"Share {pct(row['share'], digits=1)}"),
        second_points, data_date,
    )
    return first + second + competitor_learning(3, rule, group, data_date)


# --- The tabs ------------------------------------------------------------------
def info_tab(summary: dict, data_date: str, cfg: dict = CONFIG) -> str:
    return f"""
<h1>How to use this review</h1>
<p class="lead">This review covers the {CATEGORY_NAME} category: {summary['sku_count']} products and
12 weeks of sales to {long_date(data_date)}. It answers two questions: which products need
attention, and what to do next.</p>
<dl class="facts">
<div><dt>Products reviewed</dt><dd>{summary['sku_count']} <small>{summary['active_count']} active,
{summary['inactive_count']} inactive</small></dd></div>
<div><dt>Our revenue, 12 weeks</dt><dd>{eur(summary['channel_revenue'])}</dd></div>
<div><dt>Our share of the tracked market</dt><dd>{pct(summary['avg_share'], digits=1)}</dd></div>
<div><dt>Products with an action</dt><dd>{summary['action_count']} <small>in five actions</small></dd></div>
</dl>
<h2>What each tab contains</h2>
<div class="table-wrap"><table class="guide"><thead><tr><th>Tab</th><th>What you find there</th>
<th>Use it to</th></tr></thead><tbody>
<tr><td><a href="#summary">Executive summary</a></td><td>What drives the review and the five actions,
with the decision each one needs.</td><td>Brief your VP and agree priorities.</td></tr>
<tr><td><a href="#actions">Actions</a></td><td>One page per action. First the products to act on,
then what we learned, with the supporting charts.</td><td>Work through an action with your team.</td></tr>
<tr><td><a href="#open">Open points to clarify</a></td><td>Data points to confirm, products where
competitor and market trends differ, and questions to answer before acting.</td><td>Agree who
clarifies what.</td></tr>
<tr><td><a href="#list">Full action list</a></td><td>All {summary['action_count']} products with an
action in one table, with a filter per action.</td><td>Look up a product or hand over the list.</td></tr>
</tbody></table></div>
<h2>How to navigate</h2>
<ul class="plain">
<li>Use the tabs at the top to move between sections. Inside Actions, pick an action by its keyword.</li>
<li>Actions are numbered in the order to carry them out: fix the range you have, then change it,
then grow it.</li>
<li>Hover over a product row or a chart mark to see its full figures.</li>
<li>Products marked "Check first" have an open data point. The marker takes you to it.</li>
<li>To print or save as PDF, use the print function of your browser. All tabs print in order.</li>
</ul>
<h2>How to read the figures</h2>
<ul class="plain">
<li>Euro figures are estimates for a 12-week period. They measure different things, so they are
shown side by side and not added up.</li>
<li>Each product appears under one action only.</li>
<li>"Provisional" marks an estimate that depends on open data points.</li>
<li>"Market trend" comes from the sales data of the product itself. "Competitor trend" comes from
the competitor data: for the same product name where there is one, otherwise for the same
subcategory and shade ("same segment"), otherwise for the same shade. The number of competitor
products behind it is always shown.</li>
<li>"Competitor differs" marks a product whose competitor trend and market trend point in
different directions. The action stays, and the difference is an open point.</li>
<li>Competitor prices come without pack size. They are shown as context and compared only where
a competitor lists the same product name.</li>
<li>The product list is also available as <code>action_list.csv</code>. Assumptions and calculations
are documented in <code>{METHOD_FILE}</code>. Both sit next to this file.</li>
</ul>
"""


def summary_tab(skus: pd.DataFrame, summary: dict, texts: dict, cfg: dict = CONFIG) -> str:
    rules = summary["rules"]
    order = cfg["report_order"]
    largest = max(info["impact_eur"] for info in rules.values()) or 1

    rows, seen_stages, any_provisional = [], set(), False
    for number, rule in enumerate(order, start=1):
        info, text = rules[rule], texts[rule]
        stage_cell = ""
        if STAGES[rule] not in seen_stages:
            seen_stages.add(STAGES[rule])
            span = sum(1 for other in order if STAGES[other] == STAGES[rule])
            stage_cell = f'<td class="stage-cell" rowspan="{span}">{esc(STAGES[rule])}</td>'
        partner = RULES[rule]["coordinate_with"].split(": ", 1)[0].lower()
        provisional = ""
        if text["provisional"]:
            any_provisional = True
            provisional = ' <a class="flag" href="#open">Provisional</a>'
        meter_class = "meter cost" if rule == "R5" else "meter"
        rows.append(
            f"<tr>{stage_cell}"
            f"<td>{esc(text['driver'])}</td>"
            f'<td><div class="action-name"><span class="n">{number}</span>'
            f'<a href="#actions-{slug(rule)}">{esc(text["title"])}</a></div></td>'
            f'<td>{esc(text["decision"])}<br><span class="sub">With {esc(partner)}</span></td>'
            f'<td><div class="figure">{text["figure"]}{provisional}</div>'
            f'<p class="figure-label">{esc(text["figure_label"][0].upper() + text["figure_label"][1:])}</p>'
            f'<div class="{meter_class}"><i style="width:{info["impact_eur"] / largest * 100:.1f}%"></i></div></td>'
            "</tr>"
        )
    footnote = ""
    if any_provisional:
        footnote = (
            '<p class="source">Provisional: part of the estimate depends on data points that are '
            'still open. See <a href="#open">Open points to clarify</a>.</p>'
        )
    steps = [
        f"Reorder the {rules['R1']['skus']} out-of-stock products and confirm delivery dates.",
        f"Agree a price test on the {rules['R4']['skus']} low-margin products.",
        'Clarify the <a href="#open">open points</a>, so that the relist and promote lists can '
        "be confirmed.",
        "Decide delisting and relisting together at the next range review: delisting frees "
        f"{summary['shelf_freed_now_cm']} cm of shelf now, and relisting needs "
        f"{summary['shelf_needed_cm']} cm. Review the {len(summary['assortment_gaps'])} "
        "assortment gaps with suppliers in the same round.",
        f"Agree placement and promotion slots for the {rules['R3']['skus']} products with a weak share.",
    ]
    return f"""
<h1>Secure availability first, then reshape the range and grow</h1>
<p class="lead">{summary['action_count']} of {summary['sku_count']} products in {CATEGORY_NAME} need
action. The largest lever is availability: {rules['R1']['skus']} products in growing markets are out
of stock, which puts {eur(rules['R1']['impact_eur'])} of 12-week revenue at risk.</p>
<h2>What drives the review, and what to do about it</h2>
<div class="table-wrap summary-table"><table>
<thead><tr><th>Stage</th><th>What we see</th><th>What to do</th><th>Decision needed</th>
<th>Estimate, 12 weeks</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
{footnote}
<h2>Proposed sequence</h2>
<ol class="plain">{''.join(f'<li>{step}</li>' for step in steps)}</ol>
<h2>What competitor data adds</h2>
<ul class="plain">
<li>Competitor products are trending up in {summary['competitor_segments_rising']} of the
{summary['competitor_segments']} combinations of subcategory and shade they are sold in, which
points to growing demand in the category.</li>
<li>For {summary['action_count'] - summary['competitor_differs_with_action']} of the
{summary['action_count']} products with an action, the competitor trend points the same way as
the product's market trend. For {summary['competitor_differs_with_action']} it differs. These
keep their action and are listed under <a href="#open">Open points to clarify</a>.</li>
<li>Competitors sell in {len(summary['assortment_gaps'])} rising combinations where we have no
active product. They are listed as assortment gaps under <a href="#actions-relist">action 4</a>.</li>
</ul>
<h2>Where the actions sit</h2>
<p>Number of products per action in each subcategory, largest subcategory first, with the average
trend of competitor products in that subcategory.</p>
{subcategory_table(skus, summary, cfg)}
"""


def actions_tab(
    skus: pd.DataFrame, summary: dict, texts: dict, data_date: str, cfg: dict = CONFIG
) -> str:
    order = cfg["report_order"]
    flagged = flagged_in_order(skus)
    subtabs = "".join(
        f'<a href="#actions-{slug(rule)}" data-sub="{slug(rule)}"><span class="n">{number}</span>'
        f"{esc(KEYWORDS[rule])}</a>"
        for number, rule in enumerate(order, start=1)
    )
    panels = []
    for number, rule in enumerate(order, start=1):
        text = texts[rule]
        group = flagged[flagged["rule"] == rule]
        provisional = ' <a class="flag" href="#open">Provisional</a>' if text["provisional"] else ""
        price_note = ""
        if rule == "R4":
            price_note = (
                '<p class="source">Competitor prices are averages over the matched competitor '
                "products and come without pack size. They are context, not a benchmark.</p>"
            )
        extra = ""
        if rule == "R1":
            extra = watchlist_section(skus, summary, cfg)
        if rule == "R2":
            extra = assortment_gaps_section(summary)
        panels.append(
            f'<section data-subpanel="{slug(rule)}">'
            '<div class="action-head"><div>'
            f'<p class="stage-tag">Action {number} of {len(order)}, {esc(STAGES[rule].lower())}</p>'
            f'<h1>{esc(text["title"])}</h1><p class="lead">{esc(text["why"])}</p></div>'
            f'<div class="keyfig"><div class="figure">{text["figure"]}{provisional}</div>'
            f'<p class="figure-label">Estimate: {esc(text["figure_label"])}</p></div></div>'
            "<h2>What to do</h2>"
            f'<p><strong>{esc(text["decision"])}</strong> The {len(group)} products are listed '
            "with the largest estimate first.</p>"
            f"{action_products_table(rule, group, cfg)}{price_note}{extra}"
            "<h2>What we learned</h2>"
            f"{learnings_html(rule, skus, summary, data_date, cfg)}"
            "</section>"
        )
    return f'<nav class="subtabs" aria-label="Actions">{subtabs}</nav>{"".join(panels)}'


def open_points_tab(skus: pd.DataFrame, summary: dict, cfg: dict = CONFIG) -> str:
    rules = summary["rules"]
    to_confirm = skus[skus["verify_first"]].sort_values(["recommendation", "product_id"])
    confirm_rows = "".join(
        f"<tr>{product_cell(row)}"
        f"<td class='nowrap'>{esc(str(int(row['recommendation'])) + '. ' + row['action']) if row['rule'] else 'No action'}</td>"
        f"<td>{esc(row['verify_reason'])}</td>"
        f"<td class='num'>{eur(row['impact_eur']) if row['rule'] else '–'}</td></tr>"
        for _, row in to_confirm.iterrows()
    )
    clear = [
        str(number) for number, rule in enumerate(cfg["report_order"], start=1)
        if rules[rule]["verify_first"] == 0
    ]
    unaffected = f" Actions {' and '.join(clear)} have no open data point." if clear else ""
    differing = flagged_in_order(skus)
    differing = differing[differing["competitor_trend_differs"]]
    differ_rows = "".join(
        f"<tr>{product_cell(row)}"
        f"<td class='nowrap'>{int(row['recommendation'])}. {esc(row['action'])}</td>"
        f"<td class='num'>{pct(row['market_sales_trend_12w_pct'], signed=True)}</td>"
        f"<td class='num'>{pct(row['competitor_trend'], signed=True)}</td>"
        f"<td>{esc(match_label(row))}</td></tr>"
        for _, row in differing.iterrows()
    )
    per_action = ", ".join(
        f"{rules[rule]['competitor_differs']} in {SHORT_LABELS[rule].lower()}"
        for rule in cfg["report_order"] if rules[rule]["competitor_differs"]
    )
    question_rows = "".join(
        f"<tr><td><strong>{esc(question)}</strong></td><td>{esc(why)}</td></tr>"
        for question, why in OPEN_QUESTIONS
    )
    return f"""
<h1>Open points to clarify before acting</h1>
<p class="lead">Three kinds of points are open: sales figures to confirm for
{summary['verify_count']} products, {summary['competitor_differs_with_action']} products where
competitor and market trends differ, and {len(OPEN_QUESTIONS)} questions about how the retailer
works.</p>
<h2>Data points to confirm</h2>
<p>For {summary['verify_count']} products the sales figures contradict each other.
{summary['verify_with_action']} of them have an action and are marked "Check first". Together they
carry {eur(rules['R2']['verify_impact_eur'])} of the relist estimate and
{eur(rules['R3']['verify_impact_eur'])} of the promote estimate.{unaffected}</p>
<div class="table-wrap"><table><thead><tr><th>Product</th><th>Action</th><th>What to confirm</th>
<th class="num wrap">Estimate that depends on it</th></tr></thead><tbody>{confirm_rows}</tbody></table></div>
<h2>Where competitor and market trends differ</h2>
<p>For {summary['competitor_differs_with_action']} of the {summary['action_count']} products with
an action, competitor products are trending in the opposite direction to the product's own market
trend: {per_action}. The actions stand. Each difference is a point to discuss with the retailer
as a next step, most of all for the delist and markdown products, where competitors in the same
segment are growing.</p>
<div class="table-wrap"><table><thead><tr><th>Product</th><th>Action</th>
<th class="num wrap">Market trend</th><th class="num wrap">Competitor trend</th>
<th>Competitor trend is based on</th></tr></thead><tbody>{differ_rows}</tbody></table></div>
<p class="source">"Same segment" means the same subcategory and shade. Across all
{summary['sku_count']} products the two trends differ for {summary['competitor_differs_all']}.</p>
<h2>Questions for the retailer</h2>
<div class="table-wrap"><table class="guide"><thead><tr><th>Question</th><th>Why it matters</th></tr></thead>
<tbody>{question_rows}</tbody></table></div>
"""


def action_table(rows: pd.DataFrame, summary: dict, cfg: dict = CONFIG) -> str:
    """The full action list with the evidence fields, filterable by action."""
    number = {rule: n for n, rule in enumerate(cfg["report_order"], start=1)}
    chips = [f'<button type="button" data-filter="all" aria-pressed="true">All ({len(rows)})</button>']
    for rule in cfg["report_order"]:
        chips.append(
            f'<button type="button" data-filter="{number[rule]}" aria-pressed="false">'
            f'{number[rule]}. {esc(SHORT_LABELS[rule])} ({summary["rules"][rule]["skus"]})</button>'
        )
    body = []
    for _, row in rows.iterrows():
        notes = []
        if row["verify_first"]:
            notes.append(f'{check_flag(row)} {esc(row["verify_reason"])}')
        if row["competitor_trend_differs"]:
            notes.append(
                f'{differs_flag(row)} competitors {pct(row["competitor_trend"], signed=True)}, '
                f'market {pct(row["market_sales_trend_12w_pct"], signed=True)}'
            )
        if row["price_gap_note"]:
            notes.append(f'Price gap: {esc(row["price_gap_note"].replace("EUR ", "€"))}')
        for other in row["rules_triggered"].split(", "):
            if other != row["rule"]:
                notes.append(
                    f"Also qualifies for {SHORT_LABELS[other].lower()} (action {number[other]})"
                )
        if row["rule"] == "R5":
            notes.append(f'Frees {row["shelf_space_cm"]} cm of shelf')
        if row["rule"] == "R2":
            notes.append(f'Needs {row["shelf_space_cm"]} cm of shelf')
        if row["watchlist"]:
            notes.append("On the watchlist")
        body.append(
            f'<tr data-rec="{int(row["recommendation"])}">'
            f'<td class="nowrap">{int(row["recommendation"])}. {esc(row["action"])}</td>'
            f'<td><span class="sku-id">{esc(row["product_id"])}</span> {esc(row["product_name"])}'
            f'<br><span class="sub">{esc(row["subcategory"])}, {esc(STATUS_LABELS.get(row["status"], row["status"]))}, '
            f'{esc(STOCK_LABELS.get(row["stock_status"], row["stock_status"]))}</span></td>'
            f'<td class="num">{eur(row["channel_revenue_eur_12w"])}</td>'
            f'<td class="num">{pct(row["channel_margin_pct"], digits=1)}</td>'
            f'<td class="num">{pct(row["channel_sales_trend_12w_pct"], signed=True)}</td>'
            f'<td class="num">{pct(row["market_sales_trend_12w_pct"], signed=True)}</td>'
            f'<td class="num">{pct(row["share"], digits=1)}</td>'
            f'<td class="num"><strong>{eur(row["impact_eur"])}</strong><br>'
            f'<span class="sub">{esc(row["impact_type"]).lower()}</span></td>'
            f'<td>{"<br>".join(notes)}</td></tr>'
        )
    return (
        f'<div class="filters" role="group" aria-label="Filter by action">{"".join(chips)}</div>'
        '<div class="table-wrap"><table id="action-table"><thead><tr><th>Action</th><th>Product</th>'
        '<th class="num">Revenue,<br>12 weeks</th><th class="num">Margin</th>'
        '<th class="num">Trend,<br>our channel</th><th class="num">Trend,<br>market</th>'
        '<th class="num">Share of<br>market</th><th class="num">Estimate</th><th>Notes</th>'
        f"</tr></thead><tbody>{''.join(body)}</tbody></table></div>"
    )


REPORT_CSS = """
:root{--ink:#0b0d12;--night:#020409;--heading:#1e1e1e;--body:#404040;--muted:#6b665f;--sand:#a9a198;
--line:#e7dfd3;--line-strong:#d8cab7;--soft:#f6f3ee;--paper:#fff;
--orange:#f25722;--orange-ink:#b83a0c;--orange-soft:#fdece5;
--lilac:#4f5db5;--lilac-ink:#2f3a86;--lilac-light:#b7bfe6;--lilac-soft:#eef0fa;--grey:#b9b3aa;
--font:"Roobert","Inter",ui-sans-serif,system-ui,-apple-system,"Segoe UI","Helvetica Neue",Arial,sans-serif}
*{box-sizing:border-box}
html{color-scheme:light}
body{margin:0;background:var(--paper);color:var(--body);font:16px/1.55 var(--font);
font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased}
main{max-width:1120px;margin:0 auto;padding:44px 28px 72px}
h1,h2,h3{color:var(--heading);font-weight:500;line-height:1.15;margin:0}
h1{font-size:2.2rem;letter-spacing:-.03em;max-width:26ch}
h2{font-size:1.45rem;letter-spacing:-.02em;margin-top:52px;padding-top:24px;border-top:1px solid var(--line-strong)}
h3{font-size:1.12rem;letter-spacing:-.01em;margin-top:32px}
p{margin:.65em 0;max-width:76ch}
strong,b{font-weight:500;color:var(--heading)}
a{color:var(--heading);text-decoration-color:var(--orange);text-underline-offset:3px}
a:focus-visible,button:focus-visible{outline:2px solid var(--lilac);outline-offset:2px}
code{font-family:inherit;font-size:.92em;background:var(--soft);padding:1px 6px;border-radius:4px}
.lead{font-size:1.2rem;line-height:1.45;max-width:62ch;margin-top:16px}
.sub{color:var(--muted);font-size:.86em}
.source{color:var(--muted);font-size:.84rem;margin:10px 0 0}
.top{position:sticky;top:0;z-index:5;background:var(--night);color:#fff}
.top-inner{max-width:1120px;margin:0 auto;padding:18px 28px 0}
.top-row{display:flex;justify-content:space-between;align-items:center;gap:24px}
.brand-logo{height:28px;width:auto;flex:0 0 auto;filter:invert(1)}
.doc-title{font-size:1.2rem;font-weight:500;letter-spacing:-.02em;line-height:1.2;margin:0;color:#fff}
.doc-sub{color:var(--sand);font-size:.88rem;margin:3px 0 0;max-width:none}
.tabs{display:flex;flex-wrap:wrap;gap:2px 30px;margin-top:14px}
.tabs a{padding:9px 0 12px;color:rgba(255,255,255,.68);text-decoration:none;font-weight:500;
border-bottom:2px solid transparent}
.tabs a:hover{color:#fff}
.tabs a[aria-current=page]{color:#fff;border-bottom-color:var(--orange)}
.subtabs{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:36px}
.subtabs a{display:flex;align-items:center;gap:9px;padding:8px 18px 8px 9px;border:1px solid var(--line-strong);
border-radius:6px;background:var(--paper);color:var(--body);text-decoration:none;font-weight:500}
.subtabs a[aria-current=true]{background:var(--orange-soft);border-color:var(--orange);color:var(--orange-ink)}
.subtabs a[aria-current=true] .n{background:var(--orange)}
.n{display:inline-block;flex:0 0 auto;width:26px;height:26px;border-radius:50%;background:var(--ink);
color:#fff;font-size:.85rem;font-weight:500;line-height:26px;text-align:center}
.facts{display:grid;grid-template-columns:repeat(4,1fr);margin:30px 0 0;
border-top:1px solid var(--line-strong);border-bottom:1px solid var(--line-strong)}
.facts div{padding:16px 18px 16px 0}
.facts dt{font-size:.88rem;color:var(--muted)}
.facts dd{margin:2px 0 0;font-size:1.55rem;font-weight:500;letter-spacing:-.02em;color:var(--heading)}
.facts dd small{font-size:.88rem;font-weight:400;letter-spacing:0;color:var(--muted)}
.figure{font-size:1.45rem;font-weight:500;letter-spacing:-.02em;line-height:1.15;color:var(--heading)}
.figure-label{font-size:.88rem;color:var(--muted);margin:3px 0 8px}
.meter{height:6px;background:var(--soft);border-radius:0 3px 3px 0}
.meter i{display:block;height:100%;min-width:3px;background:var(--lilac);border-radius:0 3px 3px 0}
.meter.cost i{background:var(--grey)}
.summary-table td{padding:18px 14px;font-size:.95rem}
.summary-table td:last-child{min-width:210px}
.stage-cell{color:var(--heading);font-weight:500;width:112px;border-right:1px solid var(--line)}
.action-name{display:flex;gap:10px;align-items:flex-start;font-weight:500}
.action-head{display:grid;grid-template-columns:minmax(0,1fr) 290px;gap:40px;align-items:start}
.stage-tag{margin:0 0 8px;color:var(--orange-ink);font-weight:500;font-size:.92rem}
.keyfig{background:var(--night);border-radius:20px;padding:22px 24px}
.keyfig .figure{font-size:2rem;color:#fff}
.keyfig .figure-label{margin-bottom:0;color:var(--sand)}
.learning{margin-top:24px;padding:24px 28px 20px;background:var(--paper);border:1px solid var(--line);
border-radius:20px}
.learning h3{margin:0 0 4px;font-size:1.28rem;letter-spacing:-.02em;max-width:58ch}
.exhibit-no{margin:0 0 6px;color:var(--muted);font-size:.84rem;font-weight:500}
.learning .bars,.learning .shelf,.learning .chart,.learning .table-wrap{border:0;border-radius:0;padding:0;margin:14px 0 6px}
.bars,.shelf,.chart{background:var(--paper);border:1px solid var(--line);border-radius:12px;
padding:10px 18px;margin:14px 0}
.bar-row{display:grid;grid-template-columns:minmax(0,36%) minmax(0,1fr) minmax(0,25%);gap:16px;
align-items:center;padding:7px 0;font-size:.9rem}
.bar-row+.bar-row{border-top:1px dotted var(--line-strong)}
.sku-id{color:var(--muted);font-size:.84em}
.bar-track,.shelf-track{display:flex;align-items:center;gap:8px}
.bar-fill{height:14px;flex:0 0 auto;width:calc((100% - 78px) * var(--r));min-width:2px;
background:var(--lilac);border-radius:0 4px 4px 0}
.bar-fill.fill-light{background:var(--lilac-light)}
.bar-value{font-weight:500;color:var(--heading);white-space:nowrap}
.bar-note{color:var(--muted)}
.barcell i{display:block;height:5px;margin:5px 0 0 auto;width:calc(100% * var(--r));min-width:2px;
background:var(--lilac);border-radius:3px 0 0 3px}
.barcell{min-width:120px}
.flag{display:inline-block;font-size:.8rem;font-weight:500;line-height:1.55;padding:0 8px;border-radius:6px;
background:var(--lilac-soft);color:var(--lilac-ink);white-space:nowrap;text-decoration:none;vertical-align:middle;
letter-spacing:0}
.flag-alt{background:var(--orange-soft);color:var(--orange-ink)}
.cmp-track{position:relative;height:18px;margin:0 8px}
.cmp-track .zero{position:absolute;top:0;bottom:0;width:1px;background:#8d877e}
.cmp-track .link{position:absolute;top:8px;height:2px;background:var(--line-strong)}
.cmp-track .dot{position:absolute;top:3px;width:12px;height:12px;margin-left:-6px;border-radius:50%;
box-shadow:0 0 0 2px var(--paper)}
.dot-m,.sw-market{background:#8d877e}.dot-c{background:var(--lilac)}
.cmp-axis{position:relative;height:16px;margin:0 8px;font-size:.78rem;color:var(--muted)}
.cmp-axis span{position:absolute;transform:translateX(-50%);white-space:nowrap}
.legend{display:flex;flex-wrap:wrap;gap:6px 22px;font-size:.88rem;color:var(--muted);margin:6px 0 12px;max-width:none}
.sw{display:inline-block;width:12px;height:12px;border-radius:2px;margin-right:7px;vertical-align:-1px}
.sw-dot{border-radius:50%}
.sw-now{background:var(--lilac)}.sw-later{background:var(--lilac-light)}
.sw-need{background:var(--orange)}.sw-other{background:var(--grey)}
.shelf-row{display:grid;grid-template-columns:130px minmax(0,1fr);gap:14px;align-items:center;
padding:6px 0;font-size:.9rem}
.shelf-track{gap:2px}
.shelf-track .bar-value{margin-left:8px}
.seg{height:20px}
.shelf-track .seg:last-of-type{border-radius:0 4px 4px 0}
.chart svg{display:block;width:100%;height:auto}
.chart text{font-size:12px;fill:var(--muted)}
.chart .axis-title,.chart .ref-label{fill:var(--heading)}
.grid{stroke:var(--soft);stroke-width:1}
.ref{stroke:#8d877e;stroke-width:1}
.zone{fill:var(--lilac);opacity:.07}
.dot-other{fill:var(--grey);opacity:.8}
.dot-pick{fill:var(--lilac);stroke:#fff;stroke-width:2}
.table-wrap{overflow-x:auto;background:var(--paper);border:1px solid var(--line);border-radius:12px;margin:16px 0}
table{border-collapse:collapse;width:100%;font-size:.89rem}
th{font-weight:500;text-align:left;color:var(--muted);border-bottom:1px solid var(--line-strong);
padding:11px 12px;vertical-align:bottom;line-height:1.3}
td{padding:10px 12px;border-bottom:1px dotted var(--line-strong);vertical-align:top}
tr:last-child td{border-bottom:0}
.num{text-align:right;white-space:nowrap}
.num.wrap{white-space:normal}.nowrap{white-space:nowrap}
.guide td{padding:14px 12px;font-size:.95rem}
.guide td:first-child{white-space:nowrap;font-weight:500}
.action-products td:first-child{min-width:240px}
#action-table td:nth-child(2){min-width:230px}#action-table td:last-child{min-width:230px}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin:20px 0 0}
.filters button{font:inherit;font-size:.9rem;font-weight:500;padding:7px 14px;border:1px solid var(--line-strong);
border-radius:6px;background:var(--paper);color:var(--body);cursor:pointer}
.filters button[aria-pressed=true]{background:var(--orange-soft);border-color:var(--orange);color:var(--orange-ink)}
.plain{padding-left:20px;max-width:76ch}
.plain li{margin:.45em 0}
footer{max-width:1120px;margin:0 auto;padding:18px 28px 44px;color:var(--muted);font-size:.88rem;
border-top:1px solid var(--line)}
@media (max-width:780px){
main{padding:28px 16px 56px}.top-inner{padding:14px 16px 0}h1{font-size:1.7rem}
.tabs{gap:0 20px}
.facts{grid-template-columns:1fr 1fr}
.action-head{grid-template-columns:1fr;gap:18px}
.bar-row{grid-template-columns:1fr;gap:4px}
.shelf-row{grid-template-columns:1fr;gap:4px}}
@media print{.top{position:static;background:none;color:var(--heading)}.doc-title{color:var(--heading)}
.brand-logo{filter:none}.tabs,.subtabs,.filters{display:none}
main{max-width:none;padding:0}[data-panel],[data-subpanel]{display:block!important;break-before:page}
.learning,.bar-row,tr{break-inside:avoid}}
"""

REPORT_JS = """
(function () {
  var tabs = Array.prototype.slice.call(document.querySelectorAll('[data-tab]'));
  var panels = Array.prototype.slice.call(document.querySelectorAll('[data-panel]'));
  var subs = Array.prototype.slice.call(document.querySelectorAll('[data-sub]'));
  var subpanels = Array.prototype.slice.call(document.querySelectorAll('[data-subpanel]'));
  var currentSub = subpanels.length ? subpanels[0].dataset.subpanel : '';

  function show(hash) {
    var parts = (hash || '').replace('#', '').split('-');
    var tab = panels.some(function (p) { return p.dataset.panel === parts[0]; }) ? parts[0] : 'info';
    if (tab === 'actions' && subpanels.some(function (p) { return p.dataset.subpanel === parts[1]; })) {
      currentSub = parts[1];
    }
    tabs.forEach(function (t) {
      if (t.dataset.tab === tab) { t.setAttribute('aria-current', 'page'); } else { t.removeAttribute('aria-current'); }
    });
    panels.forEach(function (p) { p.hidden = p.dataset.panel !== tab; });
    subs.forEach(function (s) { s.setAttribute('aria-current', String(s.dataset.sub === currentSub)); });
    subpanels.forEach(function (p) { p.hidden = p.dataset.subpanel !== currentSub; });
    window.scrollTo(0, 0);
  }
  window.addEventListener('hashchange', function () { show(location.hash); });
  show(location.hash);

  document.querySelectorAll('[data-filter]').forEach(function (button) {
    button.addEventListener('click', function () {
      var wanted = button.dataset.filter;
      document.querySelectorAll('[data-filter]').forEach(function (other) {
        other.setAttribute('aria-pressed', String(other === button));
      });
      document.querySelectorAll('#action-table tbody tr').forEach(function (row) {
        row.hidden = wanted !== 'all' && row.dataset.rec !== wanted;
      });
    });
  });
})();
"""

ASSETS = Path(__file__).parent / "assets"


def font_face_css() -> str:
    """Embed the Inter font so the report looks the same offline.

    Inter is open licence (see assets/INTER-LICENSE.txt). If the file is
    missing the report falls back to the system font.
    """
    font = ASSETS / "inter-latin-variable.woff2"
    if not font.exists():
        return ""
    data = base64.b64encode(font.read_bytes()).decode("ascii")
    return (
        '@font-face{font-family:"Inter";font-style:normal;font-weight:100 900;font-display:swap;'
        f'src:url(data:font/woff2;base64,{data}) format("woff2")}}'
    )


def logo_html() -> str:
    """The Zenline logo for the top bar, from assets/zenline-logo.png.

    The file is dark on a transparent background, so the style sheet inverts it
    to white for the dark bar. If the file is missing, no logo is shown.
    """
    logo = ASSETS / "zenline-logo.png"
    if not logo.exists():
        return ""
    data = base64.b64encode(logo.read_bytes()).decode("ascii")
    return f'<img class="brand-logo" alt="Zenline AI" src="data:image/png;base64,{data}">'


TABS = [  # (id used in links, label)
    ("info", "Info"),
    ("summary", "Executive summary"),
    ("actions", "Actions"),
    ("open", "Open points to clarify"),
    ("list", "Full action list"),
]


def build_report(skus: pd.DataFrame, summary: dict, data_date: str, cfg: dict = CONFIG) -> str:
    """Assemble the tabbed report: info, executive summary, actions, open points, full list."""
    texts = recommendation_texts(skus, summary, cfg)
    flagged = flagged_in_order(skus)
    panels = {
        "info": info_tab(summary, data_date, cfg),
        "summary": summary_tab(skus, summary, texts, cfg),
        "actions": actions_tab(skus, summary, texts, data_date, cfg),
        "open": open_points_tab(skus, summary, cfg),
        "list": (
            "<h1>Full action list</h1>"
            f"<p>All {summary['action_count']} products with an action. The same list, with every "
            "evidence field, the owner and who to coordinate with, is in "
            "<code>action_list.csv</code> next to this report.</p>"
            + action_table(flagged, summary, cfg)
        ),
    }
    tab_links = "".join(
        f'<a href="#{tab_id}" data-tab="{tab_id}">{esc(label)}</a>' for tab_id, label in TABS
    )
    sections = "".join(
        f'<section data-panel="{tab_id}">{panels[tab_id]}</section>' for tab_id, _ in TABS
    )
    return (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(REPORT_TITLE)}: {esc(CATEGORY_NAME)}</title>"
        f"<style>{font_face_css()}{REPORT_CSS}</style></head><body>"
        '<header class="top"><div class="top-inner"><div class="top-row"><div>'
        f'<p class="doc-title">{esc(REPORT_TITLE)}</p>'
        f'<p class="doc-sub">{esc(CATEGORY_NAME)}. Data as of {long_date(data_date)}, covering the '
        f"previous 12 weeks.</p></div>{logo_html()}</div>"
        f'<nav class="tabs" aria-label="Sections">{tab_links}</nav></div></header>'
        f"<main>{sections}</main>"
        f"<footer>{esc(AUTHOR_LINE)}</footer>"
        f"<script>{REPORT_JS}</script></body></html>\n"
    )


# ---------------------------------------------------------------------------
# 6. Method file: assumptions and calculations behind the report
# ---------------------------------------------------------------------------
def build_method_doc(summary: dict, data_date: str, cfg: dict = CONFIG) -> str:
    """Markdown record of how every figure in the report was produced in this run."""
    lines = [
        "# Assumptions and calculations",
        "",
        f"Written by `review.py` from data as of {data_date}. This file documents "
        "how the figures in `category_review.html` and `action_list.csv` were produced. The "
        "decisions behind the rules, with their reasons, are in `docs/DECISIONS.md`.",
        "",
        "## Scope",
        "",
        f"- {summary['sku_count']} products: {summary['active_count']} active, "
        f"{summary['inactive_count']} inactive.",
        f"- 12-week channel revenue {eur(summary['channel_revenue'])}. Average share of the "
        f"tracked market {pct(summary['avg_share'], digits=2)} (total channel revenue ÷ total "
        "market revenue over all products).",
        "- Trend and margin columns are fractions in the data: 0.18 means 18%.",
        "",
        "## The five actions",
        "",
    ]
    for number, rule in enumerate(cfg["report_order"], start=1):
        method, info = rule_method(rule, summary, cfg), summary["rules"][rule]
        partner, task = RULES[rule]["coordinate_with"].split(": ", 1)
        lines += [
            f"### {number}. {RULES[rule]['name']} ({rule})",
            "",
            f"- Rule: {method['rule']}",
            "- Data fields used: " + ", ".join(f"`{name}`" for name in method["fields"]),
            f"- Estimate: {method['estimate']}",
            f"- Result in this run: {info['skus']} products as primary action "
            f"({info['triggered']} trigger the rule), {eur(info['impact_eur'])} "
            f"{RULES[rule]['impact_type'].lower()}.",
            f"- Owner: {OWNER}. Coordinate with {partner.lower()}: {task}.",
            "",
        ]
    lines += [
        "## One action per product",
        "",
        "Where several rules apply to a product, the first in this order is its primary action: "
        + ", ".join(f"{RULES[rule]['name']} ({rule})" for rule in cfg["precedence"])
        + ". The other rules it triggers are listed in the `also_qualifies_for` column of the "
        "action list. The order of the actions in the report is the order of execution, which "
        "is a separate choice.",
        "",
        "## Thresholds used in this run",
        "",
        "| Threshold | Value |",
        "|---|---|",
    ]
    lines += [f"| {name} | {value} |" for name, value in threshold_rows(summary, cfg)]
    lines += [
        "",
        "## How to read the estimates",
        "",
        "- All estimates are simple 12-week run rates. They assume no change in volume when a "
        "price changes and no effect of one action on another.",
        "- Sales trends are used as a direction only. No euro figure is derived from a trend.",
        "- The estimates measure different things (revenue at risk, extra revenue, revenue "
        "given up, incremental revenue) and are not added up.",
        "- Shelf space: delist candidates free their listed shelf space now, seasonal markdown "
        "candidates free theirs after sell-through.",
        "- Competitor data is matched by exact product name only. A price gap is noted when the "
        f"prices differ by more than {pct(cfg['r4b_price_gap'])}. "
        f"{summary['price_gap_notes']} such notes exist in this run.",
        "",
        "## Competitor data",
        "",
        "Every product is matched to the competitor file at the best available level. The "
        "first level that finds competitor products is used:",
        "",
        "| Match level | Products |",
        "|---|---|",
    ]
    lines += [
        f"| {label.capitalize()} | {count} |"
        for label, count in summary["competitor_match_counts"].items()
    ]
    lines += [
        "",
        "- The competitor trend, the popularity score (clipped to "
        f"{cfg['score_min']} to {cfg['score_max']}) and the competitor price are averaged over "
        "the matched competitor products. The number of products behind each signal is in "
        "`competitor_products`.",
        "- The match is thin: the competitor file covers "
        f"{summary['competitor_segments']} combinations of subcategory and shade, and "
        f"{summary['competitor_segments_single']} of them have a single product.",
        "- The competitor signal is evidence beside the rules. It changes no action and no "
        "estimate.",
        "- A product is flagged `competitor_trend_differs` when its competitor trend and its "
        "market trend (`market_sales_trend_12w_pct`) have opposite signs. In this run: "
        f"{summary['competitor_differs_all']} of {summary['sku_count']} products, "
        f"{summary['competitor_differs_with_action']} of the {summary['action_count']} with an "
        "action ("
        + ", ".join(
            f"{RULES[rule]['name'].lower()} {summary['rules'][rule]['competitor_differs']}"
            for rule in cfg["report_order"]
        )
        + "). These are listed in the report for discussion with the retailer.",
        "- Assortment gaps: combinations of subcategory and shade with competitor products, a "
        f"competitor trend above {pct(cfg['gap_competitor_trend_min'])} and no active product "
        f"of ours. {len(summary['assortment_gaps'])} in this run, not sized.",
        "",
        "**Pack size is a major assumption for anything that involves a competitor price.** "
        "The competitor file has no pack size. A price gap is calculated only where a "
        "competitor lists exactly our product name, and it assumes the competitor sells the "
        "same pack. For all other products the average competitor price of the matched "
        "products is shown as context only (`competitor_avg_price_eur`) and no gap is "
        "calculated.",
        "",
        "## Data checks",
        "",
        "A product whose figures contradict each other keeps its action and is marked "
        "\"Check first\" in the report (`verify_first` in the action list).",
        "",
        "| Check | Products |",
        "|---|---|",
    ]
    labels = {key: label.format(tol=cfg["check_price_tolerance"]) for key, label in SKU_CHECKS.items()}
    lines += [f"| {labels[key]} | {summary['checks'][key]} |" for key in SKU_CHECKS]
    lines += [
        "",
        f"{summary['verify_count']} products are marked in total, {summary['verify_with_action']} "
        "of them with an action. In the competitor file, "
        f"{summary['checks']['competitor_score_out_of_range']} popularity scores lie outside the "
        f"{cfg['score_min']} to {cfg['score_max']} range. They are clipped and affect no action.",
        "",
        "The run stops before any rule if a file or column is missing, a required field is "
        "blank, a status or stock label is unknown, an ID is duplicated, or the metadata does "
        "not match the SKU file one to one.",
        "",
        "## Open questions",
        "",
    ]
    lines += [f"- {question} {why}" for question, why in OPEN_QUESTIONS]
    return "\n".join(lines) + "\n"


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

    data_date = str(competitors["observed_date"].max())
    print(f"Loaded {len(skus)} SKUs and {len(competitors)} competitor rows from {args.input}/")
    print(f"Data date {data_date}")
    print("Consistency checks:")
    for key, count in check_counts.items():
        print(f"  {key:32s} {count}")

    skus, context = apply_rules(skus, competitors)
    summary = summarise(skus, context, check_counts)
    print(
        f"Average share {summary['avg_share']:.1%}, "
        f"R5 revenue cut-off EUR {summary['r5_revenue_cutoff']:,.0f}"
    )
    print("Rules (primary action per SKU; 'triggered' counts SKUs before precedence):")
    for number, rule in enumerate(CONFIG["report_order"], start=1):
        info = summary["rules"][rule]
        print(
            f"  {number}. {rule} {RULES[rule]['name']:20s} {info['skus']:3d} SKUs"
            f" ({info['triggered']} triggered)"
            f"  EUR {info['impact_eur']:>7,.0f}  {RULES[rule]['impact_type']}"
        )
    print(
        f"  {summary['action_count']} SKUs with an action, "
        f"{summary['verify_with_action']} to verify first"
    )
    print(f"  Price gap notes: {summary['price_gap_notes']}")
    print(
        f"  Watchlist: {summary['watchlist_count']} SKUs, "
        f"EUR {summary['watchlist_revenue']:,.0f} channel revenue"
    )

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    action_list = build_action_list(skus, data_date)
    action_list_path = output_dir / "action_list.csv"
    action_list.to_csv(action_list_path, index=False, encoding="utf-8")
    report_path = output_dir / "category_review.html"
    report_path.write_text(build_report(skus, summary, data_date), encoding="utf-8")
    method_path = output_dir / METHOD_FILE
    method_path.write_text(build_method_doc(summary, data_date), encoding="utf-8")
    print(f"Wrote {action_list_path} ({len(action_list)} SKUs)")
    print(f"Wrote {report_path}")
    print(f"Wrote {method_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
