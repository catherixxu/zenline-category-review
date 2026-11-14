"""Tests for review.py. Run with: pytest

The expected values are the figures approved in docs/DECISIONS.md for the
provided data drop. If a rule or threshold changes, these tests should fail.
"""
import re
import shutil
from pathlib import Path

import pandas as pd
import pytest

import review

DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="module")
def run():
    """Run load, checks and rules once on the provided data."""
    skus, competitors = review.load_data(DATA)
    skus, competitors, check_counts = review.run_checks(skus, competitors)
    skus, context = review.apply_rules(skus, competitors)
    summary = review.summarise(skus, context, check_counts)
    return skus.set_index("product_id"), summary


# --- Known cases: a named product gets the expected action -------------------
@pytest.mark.parametrize(
    "product_id, rule, action",
    [
        ("HC0006", "R1", "Restock"),       # out of stock, market +21%
        ("HC0007", "R2", "Relist"),        # inactive, market +36%
        ("HC0004", "R3", "Promote"),       # market +42%, share 0.2%
        ("HC0015", "R4", "Review price"),  # margin 20.8%, both trends up
        ("HC0045", "R5", "Delist"),        # evergreen, low revenue, both trends down
        ("HC0038", "R5", "Mark down"),     # same, but seasonal (A5)
        ("HC0024", "", ""),                # out of stock, but its market is shrinking
    ],
)
def test_known_cases(run, product_id, rule, action):
    skus, _ = run
    assert skus.loc[product_id, "rule"] == rule
    assert skus.loc[product_id, "action"] == action


def test_precedence_gives_one_primary_action(run):
    """HC0070 is out of stock and low margin: restock wins, price review is noted (A9, B7)."""
    skus, _ = run
    assert skus.loc["HC0070", "rule"] == "R1"
    assert skus.loc["HC0070", "rules_triggered"] == "R1, R4"
    assert "Price review" in skus.loc["HC0070", "also_qualifies_for"]


def test_price_gap_notes(run):
    """R4b is a note only: it never changes the primary action."""
    skus, summary = run
    assert summary["price_gap_notes"] == 2
    assert skus.loc["HC0006", "price_gap_pct"] == pytest.approx(-0.400, abs=0.001)
    assert "rossmann.de" in skus.loc["HC0006", "price_gap_note"]
    assert skus.loc["HC0007", "price_gap_pct"] == pytest.approx(0.578, abs=0.001)
    assert skus.loc["HC0007", "rule"] == "R2"


def test_verify_first_markers(run):
    skus, _ = run
    assert skus.loc["HC0001", "verify_first"]  # revenue with a -99% trend
    assert skus.loc["HC0002", "verify_first"]  # active, in stock, zero revenue (B9)
    assert skus.loc["HC0002", "rule"] == "R3"  # flagged rows keep their action (A8)
    assert not skus.loc["HC0006", "verify_first"]


# --- Headline figures per rule ------------------------------------------------
@pytest.mark.parametrize(
    "rule, sku_count, triggered, impact_eur",
    [
        ("R1", 9, 9, 61646),
        ("R2", 15, 15, 30364),
        ("R3", 13, 13, 20486),
        ("R4", 11, 16, 2347),
        ("R5", 19, 19, 6689),
    ],
)
def test_rule_counts_and_estimates(run, rule, sku_count, triggered, impact_eur):
    _, summary = run
    info = summary["rules"][rule]
    assert info["skus"] == sku_count
    assert info["triggered"] == triggered
    assert info["impact_eur"] == pytest.approx(impact_eur, abs=1)


def test_summary_figures(run):
    _, summary = run
    assert summary["sku_count"] == 235
    assert summary["action_count"] == 67
    assert summary["avg_share"] == pytest.approx(0.0629, abs=0.0001)
    assert summary["r5_revenue_cutoff"] == pytest.approx(1306, abs=1)
    assert (summary["delist_count"], summary["markdown_count"]) == (15, 4)
    assert summary["shelf_freed_now_cm"] == 100
    assert summary["shelf_freed_later_cm"] == 31
    assert summary["shelf_needed_cm"] == 105
    assert summary["watchlist_count"] == 13
    assert summary["watchlist_revenue"] == pytest.approx(100098, abs=1)


def test_consistency_check_counts(run):
    _, summary = run
    assert summary["checks"] == {
        "zero_revenue_trend": 7,
        "collapse_with_revenue": 1,
        "revenue_without_units": 1,
        "implied_price_gap": 3,
        "active_without_revenue": 5,
        "competitor_score_out_of_range": 4,
        "skus_to_verify": 13,
    }
    assert summary["verify_with_action"] == 10


# --- Hard failures: a broken input stops the run before any rule --------------
def broken_copy(tmp_path, filename, change):
    """Copy the data folder and apply `change` to the lines of one file."""
    folder = tmp_path / "data"
    shutil.copytree(DATA, folder)
    path = folder / filename
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(change(lines)), encoding="utf-8")
    return folder


@pytest.mark.parametrize(
    "filename, change, message",
    [
        ("sku_performance.csv",
         lambda lines: [lines[0].replace("channel_margin_pct", "margin")] + lines[1:],
         "missing columns"),
        ("sku_performance.csv", lambda lines: lines + [lines[1]], "duplicated IDs"),
        ("product_metadata.csv", lambda lines: lines[:4] + lines[5:], "one to one"),
        ("sku_performance.csv",
         lambda lines: lines[:2] + [lines[2].replace(",aktiv,", ",active,")] + lines[3:],
         "unknown status"),
        ("sku_performance.csv",
         lambda lines: lines[:3] + [lines[3].replace(",out_of_stock,", ",,")] + lines[4:],
         "blank values"),
    ],
)
def test_hard_failures(tmp_path, filename, change, message):
    folder = broken_copy(tmp_path, filename, change)
    with pytest.raises(review.DataError, match=message):
        review.load_data(folder)
    assert review.main(["--input", str(folder), "--output", str(tmp_path / "out")]) == 2
    assert not (tmp_path / "out").exists()  # nothing is produced


# --- Outputs: a full run writes three files that agree with each other --------
def test_full_run_writes_consistent_outputs(tmp_path):
    out = tmp_path / "out"
    assert review.main(["--input", str(DATA), "--output", str(out)]) == 0

    actions = pd.read_csv(out / "action_list.csv")
    assert list(actions.columns) == review.ACTION_LIST_COLUMNS
    assert len(actions) == 67
    assert actions["product_id"].is_unique  # one primary action per SKU
    assert set(actions["owner"]) == {"Category manager"}
    assert actions["recommendation"].is_monotonic_increasing  # order of execution
    assert (actions["verify_first"] == "yes").sum() == 10

    for name in ["category_review.html", "executive_summary.html"]:
        page = (out / name).read_text(encoding="utf-8")
        for figure in ["€61,646", "€2,347", "€6,689", "€30,364", "€20,486"]:
            assert figure in page, f"{figure} missing from {name}"
        # self-contained: no scripts, styles, fonts or images loaded from the web
        assert not re.search(r'(src|href)="https?://', page)
