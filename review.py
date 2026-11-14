#!/usr/bin/env python3
"""Category opportunity review: ZenBeauty Retail, Beauty > Hair Coloration.

Usage:
    python review.py --input data --output output

Steps: load and validate -> consistency checks -> rules -> outputs.
Every rule and threshold is documented in docs/DECISIONS.md.
"""
from __future__ import annotations

import argparse
import html
import math
import sys
from datetime import date, datetime
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
    # Order of recommendations in the outputs: sequence of execution (DECISIONS.md section 4)
    "report_order": ["R1", "R4", "R5", "R2", "R3"],
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
    "price_gap_note", "verify_first", "verify_reason", "watchlist",
    "owner", "coordinate_with", "data_date", "run_date",
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
    }


def build_action_list(
    skus: pd.DataFrame, data_date: str, run_date: str, cfg: dict = CONFIG
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
    rows["data_date"] = data_date
    rows["run_date"] = run_date
    return rows[ACTION_LIST_COLUMNS].reset_index(drop=True)


# ---------------------------------------------------------------------------
# 5. HTML report: one self-contained file, no external assets
# ---------------------------------------------------------------------------
STAGES = {
    "R1": "Fix the existing range",
    "R4": "Fix the existing range",
    "R5": "Change the range",
    "R2": "Change the range",
    "R3": "Grow",
}
SHORT_LABELS = {  # column headers and filter buttons
    "R1": "Restock", "R4": "Review price", "R5": "Delist or mark down",
    "R2": "Relist", "R3": "Promote",
}
STOCK_LABELS = {"in_stock": "In stock", "low_stock": "Low stock", "out_of_stock": "Out of stock"}
STATUS_LABELS = {"aktiv": "Active", "inaktiv": "Inactive"}

# DECISIONS.md section 6
OPEN_QUESTIONS = [
    "What does the status \"inaktiv\" mean in practice? We read it as being phased out "
    "while the remaining stock sells down.",
    "How is the sales trend calculated? We use it as a direction only and never derive "
    "euro figures from it.",
    "Do inactive products still occupy the shelf space listed for them? If they do, "
    "relisting needs no new space.",
    "Is there a minimum revenue per product per week that should replace the "
    "bottom-quarter cut-off used for delisting?",
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
    if not row["verify_first"]:
        return ""
    return f'<span class="flag" title="{esc(row["verify_reason"])}">! Check first</span>'


def recommendation_texts(skus: pd.DataFrame, summary: dict, cfg: dict = CONFIG) -> dict:
    """Headline, estimate and reasoning for each recommendation, in business language."""
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
    gap_note = ""
    if summary["price_gap_notes"]:
        gap_note = (
            f" {summary['price_gap_notes']} products with an exact competitor match are "
            f"priced more than {pct(cfg['r4b_price_gap'])} away from that competitor."
        )

    def check_note(rule: str) -> str:
        """Say how much of an estimate rests on products whose figures need checking."""
        info = rules[rule]
        if not info["verify_first"]:
            return ""
        return (
            f" {info['verify_first']} of them, carrying {eur(info['verify_impact_eur'])} of the "
            "estimate, need a check first because their sales figures do not add up."
        )

    return {
        "R1": {
            "title": f"Restock {rules['R1']['skus']} out-of-stock products",
            "figure": eur(rules["R1"]["impact_eur"]),
            "figure_label": "12-week revenue at risk",
            "why": "These products are active and out of stock while their market is "
            "growing. This is revenue the range already earns, so it comes first.",
        },
        "R4": {
            "title": f"Review the price of {rules['R4']['skus']} low-margin products",
            "figure": eur(rules["R4"]["impact_eur"]),
            "figure_label": f"extra revenue from a {pct(cfg['r4_price_increase'])} price increase",
            "why": f"Margin is below {pct(cfg['r4_margin_max'])} while sales grow both in our "
            f"channel and in the market, which leaves room to test a higher price.{r4_note}{gap_note}",
        },
        "R5": {
            "title": f"Delist {summary['delist_count']} products and mark down "
            f"{summary['markdown_count']} seasonal ones",
            "figure": eur(rules["R5"]["impact_eur"]),
            "figure_label": f"revenue given up, for {summary['shelf_freed_now_cm']} cm of shelf "
            f"now and {summary['shelf_freed_later_cm']} cm later",
            "why": "Their revenue is in the bottom quarter of the range (below "
            f"{eur(summary['r5_revenue_cutoff'])} in 12 weeks) and sales are falling both in our "
            "channel and in the market. Seasonal products are marked down, not delisted.",
        },
        "R2": {
            "title": f"Relist {rules['R2']['skus']} inactive products",
            "figure": eur(rules["R2"]["impact_eur"]),
            "figure_label": "incremental revenue",
            "why": f"These products are inactive, yet their market grew by more than "
            f"{pct(cfg['r2_market_trend_min'])}. Relisting them needs "
            f"{summary['shelf_needed_cm']} cm of shelf, and action 3 frees "
            f"{summary['shelf_freed_now_cm']} cm now.{check_note('R2')}",
        },
        "R3": {
            "title": f"Promote {rules['R3']['skus']} products with strong demand and a weak share",
            "figure": eur(rules["R3"]["impact_eur"]),
            "figure_label": "incremental revenue",
            "why": f"The market for these products grew by more than "
            f"{pct(cfg['r3_market_trend_min'])}, but our share is below half of our "
            f"{pct(summary['avg_share'], digits=1)} average.{check_note('R3')}",
        },
    }


def rule_method(rule: str, summary: dict, cfg: dict = CONFIG) -> dict:
    """How each recommendation was found: rule, fields used, estimate formula."""
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


def bar_note(rule: str, row: pd.Series) -> str:
    """The one piece of evidence shown beside each bar."""
    market = f"market {pct(row['market_sales_trend_12w_pct'], signed=True)}"
    notes = {
        "R1": market.capitalize(),
        "R4": f"Margin {pct(row['channel_margin_pct'], digits=1)}",
        "R5": f"{row['action']}, frees {row['shelf_space_cm']} cm",
        "R2": f"{market.capitalize()}, needs {row['shelf_space_cm']} cm",
        "R3": f"Share {pct(row['share'], digits=1)}, {market}",
    }
    return notes[rule]


def bars_html(rule: str, rows: pd.DataFrame) -> str:
    """Horizontal bars, one per SKU, sized by the estimate. Hover shows the evidence."""
    top = rows["impact_eur"].max() or 1
    parts = []
    for _, row in rows.iterrows():
        tooltip = (
            f"{row['product_id']} {row['product_name']}: revenue {eur(row['channel_revenue_eur_12w'])}, "
            f"margin {pct(row['channel_margin_pct'], digits=1)}, "
            f"trend in our channel {pct(row['channel_sales_trend_12w_pct'], signed=True)}, "
            f"market trend {pct(row['market_sales_trend_12w_pct'], signed=True)}, "
            f"share {pct(row['share'], digits=1)}"
        )
        gap = ""
        if row["price_gap_note"]:
            gap = f' <span class="flag flag-info" title="{esc(row["price_gap_note"])}">Price gap</span>'
        parts.append(
            f'<div class="bar-row" title="{esc(tooltip)}">'
            f'<div class="bar-label"><span class="sku-id">{esc(row["product_id"])}</span> '
            f'{esc(row["product_name"])}</div>'
            f'<div class="bar-track"><div class="bar-fill" style="--r:{row["impact_eur"] / top:.4f}"></div>'
            f'<span class="bar-value">{eur(row["impact_eur"])}</span></div>'
            f'<div class="bar-note">{esc(bar_note(rule, row))} {check_flag(row)}{gap}</div>'
            "</div>"
        )
    return '<div class="bars">' + "".join(parts) + "</div>"


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
        xx = x(step / 100)
        out.append(
            f'<text x="{xx:.1f}" y="{height - bottom + 18}" text-anchor="middle">'
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
    out.extend(dot(row, "dot-promote", 5.5) for _, row in promote.iterrows())
    out.append("</svg>")
    legend = (
        '<p class="legend">'
        f'<span><i class="sw sw-now sw-dot"></i>Promote candidates ({len(promote)})</span>'
        f'<span><i class="sw sw-other sw-dot"></i>Other active products in stock ({len(others)})</span>'
        "</p>"
    )
    return f'<figure class="chart">{legend}{"".join(out)}</figure>'


def price_gap_table(skus: pd.DataFrame) -> str:
    rows = skus[skus["price_gap_note"] != ""]
    if rows.empty:
        return ""
    body = "".join(
        f"<tr><td>{esc(row['product_id'])} {esc(row['product_name'])}</td>"
        f"<td class='num'>€{row['price_eur']:.2f}</td>"
        f"<td class='num'>€{row['competitor_price_eur']:.2f}</td>"
        f"<td>{esc(row['competitor_source'])}</td>"
        f"<td class='num'>{pct(row['price_gap_pct'], signed=True)}</td>"
        f"<td>{esc(row['action'])} (action {int(row['recommendation'])})</td></tr>"
        for _, row in rows.iterrows()
    )
    return (
        "<p>Price gaps against an exact competitor match. These are notes for the price "
        "review and do not change the product's own action.</p>"
        '<div class="table-wrap"><table><thead><tr><th>Product</th><th class="num">Our price</th>'
        '<th class="num">Competitor price</th><th>Seen at</th><th class="num">Gap</th>'
        "<th>Its own action</th></tr></thead><tbody>" + body + "</tbody></table></div>"
    )


def subcategory_table(skus: pd.DataFrame, cfg: dict = CONFIG) -> str:
    """Where the actions sit: one row per subcategory."""
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
        counts = "".join(
            f'<td class="num">{int((group["rule"] == rule).sum()) or "–"}</td>'
            for rule in cfg["report_order"]
        )
        body.append(
            f"<tr><td class='nowrap'>{esc(name)}</td><td class='num'>{len(group)}</td>"
            f"<td class='num'>{eur(revenue)}</td><td class='num'>{pct(revenue / market, digits=1)}</td>"
            f"{counts}</tr>"
        )
    return (
        '<div class="table-wrap"><table><thead><tr><th>Subcategory</th><th class="num">Products</th>'
        '<th class="num wrap">Revenue, 12 weeks</th><th class="num wrap">Share of market</th>'
        f"{header}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"
    )


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
:root{--page:#F4F3F7;--paper:#fff;--ink:#1E1B26;--muted:#625D6E;--rule:#DDD9E3;--soft:#F0EEF3;
--accent:#6B2D5C;--blue:#2a78d6;--blue-light:#9ec5f4;--orange:#eb6834;--grey:#B4B1BC;
--warn:#fab219;--warn-soft:#FFF4D6}
*{box-sizing:border-box}
html{color-scheme:light}
body{margin:0;background:var(--page);color:var(--ink);font:16px/1.55 "Segoe UI",system-ui,
-apple-system,"Helvetica Neue",Arial,sans-serif;font-variant-numeric:tabular-nums}
main{max-width:1080px;margin:0 auto;padding:44px 28px 88px}
h1,h2,h3{font-family:Georgia,"Iowan Old Style","Times New Roman",serif;line-height:1.2;margin:0}
h1{font-size:2.4rem;letter-spacing:-.01em;max-width:20ch}
h2{font-size:1.6rem;margin-top:68px;padding-top:18px;border-top:2px solid var(--ink)}
h3{font-size:1.22rem;margin-top:40px}
p{margin:.65em 0;max-width:74ch}
a{color:var(--accent)}
a:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
code{font-family:inherit;font-size:.92em;background:var(--soft);padding:1px 5px;border-radius:3px}
.meta{color:var(--muted);margin-top:10px}
.lead{font-size:1.22rem;line-height:1.45;max-width:60ch;margin-top:22px}
nav{display:flex;flex-wrap:wrap;gap:6px 22px;margin-top:22px;font-size:.95rem}
.facts{display:grid;grid-template-columns:repeat(4,1fr);margin:30px 0 0;
border-top:1px solid var(--rule);border-bottom:1px solid var(--rule)}
.facts div{padding:14px 18px 14px 0}
.facts dt{font-size:.88rem;color:var(--muted)}
.facts dd{margin:2px 0 0;font-size:1.3rem;font-weight:600}
.facts dd small{font-size:.88rem;font-weight:400;color:var(--muted)}
.sequence{position:relative;margin-top:8px}
.sequence::before{content:"";position:absolute;left:17px;top:44px;bottom:30px;width:2px;background:var(--accent)}
.stage{position:relative;margin:28px 0 10px 54px;font:600 .95rem/1.3 "Segoe UI",system-ui,sans-serif;color:var(--accent)}
.step{position:relative;display:grid;grid-template-columns:36px minmax(0,1fr);gap:18px;margin-bottom:10px}
.step:last-child::after{content:"";position:absolute;left:8px;top:52px;bottom:-12px;width:20px;background:var(--page)}
.step-num{width:36px;height:36px;margin-top:14px;border-radius:50%;background:var(--accent);color:#fff;
font:700 1.05rem/36px Georgia,serif;text-align:center}
.step-card{display:grid;grid-template-columns:minmax(0,1fr) 290px;gap:28px;background:var(--paper);
border:1px solid var(--rule);border-radius:6px;padding:16px 22px 18px}
.step-card h3{margin:0;font-size:1.2rem}
.step-card h3 a{color:var(--ink);text-decoration-color:var(--rule);text-underline-offset:3px}
.step-card p{margin:.45em 0 0;font-size:.97rem}
.figure{font-size:1.55rem;font-weight:700;line-height:1.15}
.figure-label{font-size:.88rem;color:var(--muted);margin:3px 0 9px}
.meter{height:8px;background:var(--soft);border-radius:0 4px 4px 0}
.meter i{display:block;height:100%;min-width:3px;background:var(--blue);border-radius:0 4px 4px 0}
.meter.cost i{background:var(--grey)}
.links{margin:22px 0 0 54px;padding-left:18px;font-size:.97rem;max-width:72ch}
.links li{margin:.4em 0}
.method{display:grid;grid-template-columns:150px minmax(0,1fr);gap:6px 18px;margin:14px 0 18px;
font-size:.94rem;max-width:860px}
.method dt{color:var(--muted)}
.method dd{margin:0}
.bars,.shelf,.chart{background:var(--paper);border:1px solid var(--rule);border-radius:6px;
padding:10px 18px;margin:14px 0}
.bar-row{display:grid;grid-template-columns:minmax(0,36%) minmax(0,1fr) minmax(0,27%);gap:16px;
align-items:center;padding:6px 0;font-size:.9rem}
.bar-row+.bar-row{border-top:1px solid var(--soft)}
.sku-id{color:var(--muted);font-size:.84em}
.bar-track,.shelf-track{display:flex;align-items:center;gap:8px}
.bar-fill{height:14px;flex:0 0 auto;width:calc((100% - 78px) * var(--r));min-width:2px;
background:var(--blue);border-radius:0 4px 4px 0}
.bar-value{font-weight:600;white-space:nowrap}
.bar-note{color:var(--muted)}
.flag{display:inline-block;font-size:.8rem;font-weight:600;line-height:1.5;padding:0 7px;border-radius:3px;
background:var(--warn-soft);border-left:3px solid var(--warn);color:var(--ink);white-space:nowrap}
.flag-info{background:var(--soft);border-left-color:var(--accent)}
.legend{display:flex;flex-wrap:wrap;gap:6px 22px;font-size:.88rem;color:var(--muted);margin:6px 0 12px;max-width:none}
.sw{display:inline-block;width:12px;height:12px;border-radius:2px;margin-right:7px;vertical-align:-1px}
.sw-dot{border-radius:50%}
.sw-now{background:var(--blue)}.sw-later{background:var(--blue-light)}
.sw-need{background:var(--orange)}.sw-other{background:var(--grey)}
.shelf-row{display:grid;grid-template-columns:130px minmax(0,1fr);gap:14px;align-items:center;
padding:6px 0;font-size:.9rem}
.shelf-track{gap:2px}
.shelf-track .bar-value{margin-left:8px}
.seg{height:20px}
.shelf-track .seg:last-of-type{border-radius:0 4px 4px 0}
.chart{margin-left:0;margin-right:0}
.chart svg{display:block;width:100%;height:auto}
.chart text{font-size:12px;fill:var(--muted)}
.chart .axis-title,.chart .ref-label{fill:var(--ink)}
.grid{stroke:var(--soft);stroke-width:1}
.ref{stroke:var(--muted);stroke-width:1}
.zone{fill:var(--blue);opacity:.07}
.dot-other{fill:var(--grey);opacity:.75}
.dot-promote{fill:var(--blue);stroke:#fff;stroke-width:2}
.table-wrap{overflow-x:auto;background:var(--paper);border:1px solid var(--rule);border-radius:6px;margin:14px 0}
table{border-collapse:collapse;width:100%;font-size:.89rem}
th{font-weight:600;text-align:left;color:var(--muted);border-bottom:1px solid var(--ink);
padding:10px 12px;vertical-align:bottom;line-height:1.3}
td{padding:9px 12px;border-bottom:1px solid var(--rule);vertical-align:top}
tr:last-child td{border-bottom:0}
.num{text-align:right;white-space:nowrap}
.num.wrap{white-space:normal}.nowrap{white-space:nowrap}
#action-table td:nth-child(2){min-width:230px}#action-table td:last-child{min-width:230px}
.sub{color:var(--muted);font-size:.86em}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0 0}
.filters button{font:inherit;font-size:.9rem;padding:6px 13px;border:1px solid var(--rule);
border-radius:999px;background:var(--paper);color:var(--ink);cursor:pointer}
.filters button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff}
.plain{padding-left:20px;max-width:74ch}
.plain li{margin:.45em 0}
footer{margin-top:64px;padding-top:16px;border-top:1px solid var(--rule);color:var(--muted);font-size:.9rem}
.exec main{max-width:1000px}
.exec h1{max-width:24ch}
.exec-table{margin-top:30px}
.exec-table td{padding:16px 14px;font-size:.95rem}
.exec-table td p{margin:.3em 0 0}
.exec-table h3{margin:0;font-size:1.08rem}
.exec-table h3 a{color:var(--ink);text-decoration-color:var(--rule);text-underline-offset:3px}
.exec-table .figure{font-size:1.3rem}
.exec-table .figure-label{margin:2px 0 7px}
.exec-table td:nth-child(n+3):last-child{min-width:170px}
.stage-cell{color:var(--accent);font-weight:600;width:118px;border-right:1px solid var(--rule)}
.n{display:inline-block;width:30px;height:30px;border-radius:50%;background:var(--accent);color:#fff;
font:700 1rem/30px Georgia,serif;text-align:center}
.notes{display:grid;grid-template-columns:repeat(3,1fr);gap:28px;margin-top:30px}
.notes h3{margin:0 0 6px;font-size:1.05rem}
.notes p{margin:0;font-size:.94rem}
@media (max-width:780px){
.notes{grid-template-columns:1fr}
main{padding:28px 16px 60px}h1{font-size:1.9rem}
.facts{grid-template-columns:1fr 1fr}
.step-card{grid-template-columns:1fr;gap:14px}
.bar-row{grid-template-columns:1fr;gap:4px}
.method{grid-template-columns:1fr;gap:2px}.method dd{margin-bottom:8px}
.shelf-row{grid-template-columns:1fr;gap:4px}}
@media print{body{background:#fff}main{max-width:none;padding:0}
.filters{display:none}.step,.bar-row,tr{break-inside:avoid}h2{break-after:avoid}}
"""

REPORT_JS = """
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
"""


def build_report(
    skus: pd.DataFrame, summary: dict, data_date: str, run_date: str, cfg: dict = CONFIG
) -> str:
    """Assemble the report: recommendations, evidence, checks, full list, method."""
    rules = summary["rules"]
    texts = recommendation_texts(skus, summary, cfg)
    flagged = flagged_in_order(skus)
    number = {rule: n for n, rule in enumerate(cfg["report_order"], start=1)}
    largest = max(info["impact_eur"] for info in rules.values()) or 1

    # --- Recommendations, in order of execution ---
    steps, last_stage = [], None
    for rule in cfg["report_order"]:
        if STAGES[rule] != last_stage:
            last_stage = STAGES[rule]
            steps.append(f'<p class="stage">{esc(last_stage)}</p>')
        text = texts[rule]
        meter_class = "meter cost" if rule == "R5" else "meter"
        steps.append(
            f'<div class="step"><div class="step-num">{number[rule]}</div><div class="step-card">'
            f'<div><h3><a href="#evidence-{rule.lower()}">{esc(text["title"])}</a></h3>'
            f'<p>{esc(text["why"])}</p></div>'
            f'<div><div class="figure">{text["figure"]}</div>'
            f'<p class="figure-label">Estimate: {esc(text["figure_label"])}</p>'
            f'<div class="{meter_class}"><i style="width:{rules[rule]["impact_eur"] / largest * 100:.1f}%"></i></div>'
            "</div></div></div>"
        )

    links = [
        f"Actions 3 and 4 work as a pair. Delisting frees {summary['shelf_freed_now_cm']} cm now "
        f"and {summary['shelf_freed_later_cm']} cm once the marked-down products have sold "
        f"through, and the relist candidates need {summary['shelf_needed_cm']} cm. This holds "
        "only if inactive products no longer occupy their shelf space, which is an open "
        "question for ZenBeauty."
    ]
    for _, row in skus[(skus["rule"] == "R1") & (skus["price_gap_note"] != "")].iterrows():
        links.append(
            f"Actions 1 and 2 meet in {row['product_id']} {row['product_name']}: it is out of "
            f"stock and priced at {row['price_gap_note'].replace('EUR ', '€')}. Restock it "
            "first, then review its price."
        )

    # --- Evidence, one section per recommendation ---
    evidence = []
    for rule in cfg["report_order"]:
        method = rule_method(rule, summary, cfg)
        group = flagged[flagged["rule"] == rule]
        fields = ", ".join(f"<code>{esc(name)}</code>" for name in method["fields"])
        partner, task = RULES[rule]["coordinate_with"].split(": ", 1)
        extra = ""
        if rule == "R4":
            extra = price_gap_table(skus)
        if rule == "R5":
            extra = (
                "<p>Shelf space freed by this action, against the space the relist candidates "
                "of action 4 need.</p>" + shelf_html(summary)
            )
        if rule == "R3":
            extra = (
                "<p>Each dot is an active product that is in stock. Promote candidates sit in "
                "the shaded area: the market is growing and our share is weak.</p>"
                + scatter_svg(skus, summary, cfg)
            )
        evidence.append(
            f'<section id="evidence-{rule.lower()}"><h3>{number[rule]}. {esc(texts[rule]["title"])}</h3>'
            '<dl class="method">'
            f'<dt>Rule</dt><dd>{esc(method["rule"])}</dd>'
            f"<dt>Data fields used</dt><dd>{fields}</dd>"
            f'<dt>Estimate</dt><dd>{esc(method["estimate"])}</dd>'
            f'<dt>Owner</dt><dd>{esc(OWNER)}. Coordinate with {esc(partner.lower())}: {esc(task)}.</dd>'
            f"</dl>{extra}"
            f"<p>The {len(group)} products, largest estimate first. Hover over a row for the "
            "full evidence.</p>"
            f"{bars_html(rule, group)}</section>"
        )

    # --- Checks ---
    check_labels = {
        key: label.format(tol=cfg["check_price_tolerance"]) for key, label in SKU_CHECKS.items()
    }
    check_rows = "".join(
        f"<tr><td>{esc(check_labels[key])}</td><td class='num'>{summary['checks'][key]}</td></tr>"
        for key in SKU_CHECKS
    )
    to_verify = skus[skus["verify_first"]].sort_values(["recommendation", "product_id"])
    verify_rows = "".join(
        f"<tr><td><span class='sku-id'>{esc(row['product_id'])}</span> {esc(row['product_name'])}</td>"
        f"<td class='nowrap'>{esc(str(int(row['recommendation'])) + '. ' + row['action']) if row['rule'] else 'No action'}</td>"
        f"<td>{esc(row['verify_reason'])}</td></tr>"
        for _, row in to_verify.iterrows()
    )

    # --- Watchlist ---
    watch = skus[skus["watchlist"]].sort_values("channel_revenue_eur_12w", ascending=False)
    watch_rows = "".join(
        f"<tr><td><span class='sku-id'>{esc(row['product_id'])}</span> {esc(row['product_name'])}</td>"
        f"<td class='num'>{eur(row['channel_revenue_eur_12w'])}</td>"
        f"<td class='num'>{pct(row['market_sales_trend_12w_pct'], signed=True)}</td>"
        f"<td>{esc(str(int(row['recommendation'])) + '. ' + row['action']) if row['rule'] else 'None'}</td></tr>"
        for _, row in watch.iterrows()
    )

    thresholds = [
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
    ]
    threshold_rows = "".join(f"<tr><td>{esc(a)}</td><td>{esc(b)}</td></tr>" for a, b in thresholds)
    questions = "".join(f"<li>{esc(q)}</li>" for q in OPEN_QUESTIONS)

    body = f"""
<header>
<h1>Hair Coloration: what needs attention, and in what order</h1>
<p class="meta">Category review for ZenBeauty Retail, Beauty &gt; Hair Coloration. Data as of
{long_date(data_date)}, covering the previous 12 weeks. Report generated {long_date(run_date)}.
A one-page version is in the <a href="executive_summary.html">executive summary</a>.</p>
<p class="lead">{summary['action_count']} of {summary['sku_count']} products need an action. Start
with availability: {rules['R1']['skus']} products in growing markets are out of stock, which puts
{eur(rules['R1']['impact_eur'])} of 12-week revenue at risk.</p>
<nav aria-label="Sections"><a href="#actions">Five actions</a><a href="#subcategories">By subcategory</a>
<a href="#evidence">Evidence</a><a href="#checks">Check before acting</a><a href="#watchlist">Watchlist</a>
<a href="#list">Full action list</a><a href="#method">Method and open questions</a></nav>
<dl class="facts">
<div><dt>Products reviewed</dt><dd>{summary['sku_count']} <small>{summary['active_count']} active,
{summary['inactive_count']} inactive</small></dd></div>
<div><dt>Channel revenue, 12 weeks</dt><dd>{eur(summary['channel_revenue'])}</dd></div>
<div><dt>Our share of the tracked market</dt><dd>{pct(summary['avg_share'], digits=1)}</dd></div>
<div><dt>Products with an action</dt><dd>{summary['action_count']} <small>{summary['verify_with_action']}
to check first</small></dd></div>
</dl>
</header>

<section id="actions">
<h2>Five actions, in the order to carry them out</h2>
<p>The order is the sequence of execution, not a ranking by size: fix the range you have, then
change it, then grow it. Each product appears under one action only. The euro figures are 12-week
run-rate estimates with their formula shown in the evidence. They measure different things, so
they are shown side by side and not added up.</p>
<div class="sequence">{''.join(steps)}</div>
<ul class="links">{''.join(f'<li>{esc(item)}</li>' for item in links)}</ul>
</section>

<section id="subcategories">
<h2>Where the actions sit</h2>
<p>Number of products per action in each subcategory, largest subcategory first.</p>
{subcategory_table(skus, cfg)}
</section>

<section id="evidence">
<h2>Evidence for each action</h2>
<p>Every figure below comes from the three data files provided. Field names are the column names
in those files.</p>
{''.join(evidence)}
</section>

<section id="checks">
<h2>Check before acting</h2>
<p>{summary['verify_count']} products have figures that contradict each other,
{summary['verify_with_action']} of them with an action. They keep their action but are marked
"Check first": confirm the numbers with the data owner before acting on them.</p>
<div class="table-wrap"><table><thead><tr><th>Check</th><th class="num">Products</th></tr></thead>
<tbody>{check_rows}</tbody></table></div>
<div class="table-wrap"><table><thead><tr><th>Product</th><th>Action</th><th>What to check</th></tr></thead>
<tbody>{verify_rows}</tbody></table></div>
<p>In the competitor file, {summary['checks']['competitor_score_out_of_range']} popularity scores
lie outside the 0 to 100 range. They are capped at 100 and do not affect any action.</p>
</section>

<section id="watchlist">
<h2>Watchlist</h2>
<p>Not a recommendation. These {summary['watchlist_count']} active products are low on stock while
their market grows by more than {pct(cfg['watch_market_trend_min'])}. Together they earned
{eur(summary['watchlist_revenue'])} in 12 weeks, so they are the next candidates for action 1
if stock runs out.</p>
<div class="table-wrap"><table><thead><tr><th>Product</th><th class="num">Revenue, 12 weeks</th>
<th class="num">Market trend</th><th>Current action</th></tr></thead><tbody>{watch_rows}</tbody></table></div>
</section>

<section id="list">
<h2>Full action list</h2>
<p>All {summary['action_count']} products with an action. The same list, with every evidence field,
the owner and who to coordinate with, is in <code>action_list.csv</code> next to this report.</p>
{action_table(flagged, summary, cfg)}
</section>

<section id="method">
<h2>Method and open questions</h2>
<h3>Thresholds used in this run</h3>
<div class="table-wrap"><table><tbody>{threshold_rows}</tbody></table></div>
<h3>How to read the estimates</h3>
<ul class="plain">
<li>All estimates are simple 12-week run rates. They assume no change in volume when a price
changes and no effect of one action on another.</li>
<li>Sales trends are used as a direction only. No euro figure is derived from a trend.</li>
<li>Each product has one primary action. Where several rules apply, the order is restock, relist,
promote, price review, delist.</li>
<li>Competitor data is matched by exact product name only, which is why just
{summary['price_gap_notes']} price gaps are shown.</li>
</ul>
<h3>Open questions for ZenBeauty</h3>
<ul class="plain">{questions}</ul>
</section>

<footer>Generated by review.py from the files in the input folder. Rules and thresholds are
documented in docs/DECISIONS.md.</footer>
"""
    return html_page("Hair Coloration category review", body, script=REPORT_JS)


def html_page(title: str, body: str, body_class: str = "", script: str = "") -> str:
    """Wrap a body in one self-contained HTML document."""
    script_tag = f"<script>{script}</script>" if script else ""
    return (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)}</title>"
        f'<style>{REPORT_CSS}</style></head><body class="{body_class}"><main>{body}</main>'
        f"{script_tag}</body></html>\n"
    )


# ---------------------------------------------------------------------------
# 6. Executive summary: one page for a decision maker
# ---------------------------------------------------------------------------
EXEC_DECISIONS = {  # what each action asks the decision maker for
    "R1": "Reorder now and confirm delivery dates.",
    "R4": "Agree a price test on these products.",
    "R5": "Approve the delist and markdown list.",
    "R2": "Approve the relist list.",
    "R3": "Agree placement and promotion slots.",
}


def build_executive_summary(
    skus: pd.DataFrame, summary: dict, data_date: str, run_date: str, cfg: dict = CONFIG
) -> str:
    """The five actions on one page: the decision, the estimate, what is still unverified."""
    rules = summary["rules"]
    texts = recommendation_texts(skus, summary, cfg)
    largest = max(info["impact_eur"] for info in rules.values()) or 1
    order = cfg["report_order"]

    rows, seen_stages = [], set()
    for number, rule in enumerate(order, start=1):
        info, text = rules[rule], texts[rule]
        stage_cell = ""
        if STAGES[rule] not in seen_stages:
            seen_stages.add(STAGES[rule])
            span = sum(1 for other in order if STAGES[other] == STAGES[rule])
            stage_cell = f'<td class="stage-cell" rowspan="{span}">{esc(STAGES[rule])}</td>'
        partner = RULES[rule]["coordinate_with"].split(": ", 1)[0].lower()
        decision = EXEC_DECISIONS[rule]
        if info["verify_first"]:
            check = (
                f"{info['verify_first']} products, carrying {eur(info['verify_impact_eur'])} of "
                "the estimate"
            )
            decision += " Check the flagged products first."
        else:
            check = "None"
        meter_class = "meter cost" if rule == "R5" else "meter"
        rows.append(
            f"<tr>{stage_cell}"
            f'<td><span class="n">{number}</span></td>'
            f'<td><h3><a href="category_review.html#evidence-{rule.lower()}">{esc(text["title"])}</a></h3>'
            f'<p>{esc(decision)}</p><p class="sub">{esc(OWNER)}, with {esc(partner)}</p></td>'
            f'<td><div class="figure">{text["figure"]}</div>'
            f'<p class="figure-label">Estimate: {esc(text["figure_label"])}</p>'
            f'<div class="{meter_class}"><i style="width:{info["impact_eur"] / largest * 100:.1f}%"></i></div></td>'
            f"<td>{esc(check)}</td></tr>"
        )

    body = f"""
<header>
<h1>Hair Coloration: five actions, starting with availability</h1>
<p class="meta">Executive summary for ZenBeauty Retail. Data as of {long_date(data_date)}, covering
the previous 12 weeks. Generated {long_date(run_date)}.</p>
<p class="lead">{summary['action_count']} of {summary['sku_count']} products need an action. Start
with availability: {rules['R1']['skus']} products in growing markets are out of stock, which puts
{eur(rules['R1']['impact_eur'])} of 12-week revenue at risk.</p>
</header>

<div class="table-wrap exec-table"><table>
<thead><tr><th>Stage</th><th></th><th>Action and decision needed</th><th>Estimate, 12 weeks</th>
<th>To verify before acting</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>

<div class="notes">
<div><h3>Actions 3 and 4 are a pair</h3>
<p>Delisting frees {summary['shelf_freed_now_cm']} cm of shelf now and
{summary['shelf_freed_later_cm']} cm once the markdowns have sold through. The relist candidates
need {summary['shelf_needed_cm']} cm. This assumes inactive products no longer hold shelf space,
which ZenBeauty needs to confirm.</p></div>
<div><h3>What needs checking first</h3>
<p>{summary['verify_with_action']} of the {summary['action_count']} products have figures that
contradict each other. They carry {eur(rules['R2']['verify_impact_eur'])} of the relist estimate
and {eur(rules['R3']['verify_impact_eur'])} of the promote estimate, so treat those two figures
as provisional until the data is confirmed.</p></div>
<div><h3>How to read the figures</h3>
<p>Estimates are simple 12-week run rates. They measure different things, so they are not added
up. Separately, {summary['watchlist_count']} products are low on stock in growing markets and sit
on a watchlist.</p></div>
</div>

<footer>The evidence for every figure is in the <a href="category_review.html">in-depth category
review</a>. The product-level list is in <a href="action_list.csv">action_list.csv</a>.</footer>
"""
    return html_page("Hair Coloration executive summary", body, body_class="exec")


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
    run_date = date.today().isoformat()
    print(f"Loaded {len(skus)} SKUs and {len(competitors)} competitor rows from {args.input}/")
    print(f"Data date {data_date}, run date {run_date}")
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
    action_list = build_action_list(skus, data_date, run_date)
    action_list_path = output_dir / "action_list.csv"
    action_list.to_csv(action_list_path, index=False, encoding="utf-8")
    report_path = output_dir / "category_review.html"
    report_path.write_text(build_report(skus, summary, data_date, run_date), encoding="utf-8")
    summary_path = output_dir / "executive_summary.html"
    summary_path.write_text(
        build_executive_summary(skus, summary, data_date, run_date), encoding="utf-8"
    )
    print(f"Wrote {action_list_path} ({len(action_list)} SKUs)")
    print(f"Wrote {report_path}")
    print(f"Wrote {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
