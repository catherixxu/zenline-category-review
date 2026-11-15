"""Tests for review.py. Run with: pytest

The expected values are the figures recorded in docs/DECISIONS.md for the
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


# --- Competitor data: matched at the best available level, evidence beside the rules ---
def test_competitor_match_levels(run):
    """Exact name first, then subcategory and shade, then shade only (A6, B22)."""
    skus, summary = run
    assert summary["competitor_match_counts"] == {
        "exact name": 2, "subcategory and shade": 175, "shade only": 58,
    }
    assert skus.loc["HC0006", "competitor_match"] == "exact name"
    assert skus.loc["HC0006", "competitor_trend"] == pytest.approx(0.33)  # its own match, CM003
    assert skus.loc["HC0015", "competitor_match"] == "subcategory and shade"
    assert skus.loc["HC0015", "competitor_products"] == 1  # the count behind a signal is kept (B25)
    assert skus["competitor_trend"].notna().all()  # every product has a signal


def test_competitor_differs_flag(run):
    """Flag when competitor trend and market trend have opposite signs; actions unchanged (B20, B21)."""
    skus, summary = run
    assert summary["competitor_differs_all"] == 112
    assert summary["competitor_differs_with_action"] == 26
    assert {rule: info["competitor_differs"] for rule, info in summary["rules"].items()} == {
        "R1": 2, "R4": 1, "R5": 19, "R2": 3, "R3": 1,
    }
    # HC0043: market +1%, competitors -15%. It stays a restock product.
    assert skus.loc["HC0043", "competitor_trend_differs"]
    assert skus.loc["HC0043", "rule"] == "R1"
    assert not skus.loc["HC0006", "competitor_trend_differs"]


def test_assortment_gaps(run):
    """Rising competitor segments where we have no active product (B24)."""
    skus, summary = run
    gaps = summary["assortment_gaps"]
    assert len(gaps) == 11
    assert all(gap["competitor_trend"] > 0 for gap in gaps)
    active = skus[skus["status"] == "aktiv"]
    covered = set(zip(active["subcategory"], active["shade_group"]))
    assert all((gap["subcategory"], gap["shade_group"]) not in covered for gap in gaps)
    assert (gaps[0]["subcategory"], gaps[0]["shade_group"]) == ("Color Care Add-on", "Pastel")


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

    page = (out / "category_review.html").read_text(encoding="utf-8")
    for figure in ["€61,646", "€2,347", "€6,689", "€30,364", "€20,486"]:
        assert figure in page, f"{figure} missing from the report"
    for tab_id, _ in review.TABS:  # info, summary, actions, open points, full list
        assert f'data-panel="{tab_id}"' in page
    for keyword in review.KEYWORDS.values():  # one sub-tab per action
        assert f'data-subpanel="{keyword.lower()}"' in page
    # self-contained: no scripts, styles, fonts or images loaded from the web
    assert not re.search(r'(src|href)="https?://', page)
    assert "url(http" not in page
    # outputs carry the data date but no creation date, so reruns give the same files
    assert "Data as of 1 July 2026" in page
    assert "Generated" not in page
    assert set(actions["data_date"]) == {"2026-07-01"}


def test_method_details_are_in_the_method_file_not_the_report(tmp_path):
    """Rules, data fields and formulas are documented next to the report, not inside it (B14)."""
    out = tmp_path / "out"
    review.main(["--input", str(DATA), "--output", str(out)])
    page = (out / "category_review.html").read_text(encoding="utf-8")
    method = (out / review.METHOD_FILE).read_text(encoding="utf-8")
    for detail in ["Data fields used", "channel_margin_pct", "Owner: Category manager"]:
        assert detail in method
        assert detail not in page
    # the pack-size assumption behind competitor prices is spelled out in both
    assert "Pack size is a major assumption" in method
    assert "come without pack size" in page
    assert "Assortment gaps" in page and "Competitor differs" in page
    for rule in review.CONFIG["report_order"]:
        assert f"({rule})" in method


def test_provisional_estimates(run):
    """Relist and promote rest on open data points for more than 10% of their estimate."""
    skus, summary = run
    texts = review.recommendation_texts(skus.reset_index(), summary)
    assert {rule for rule, text in texts.items() if text["provisional"]} == {"R2", "R3"}


def test_rerun_gives_identical_files(tmp_path):
    """No output carries a creation date: the same data gives the same three files (B19)."""
    first, second = tmp_path / "first", tmp_path / "second"
    review.main(["--input", str(DATA), "--output", str(first)])
    review.main(["--input", str(DATA), "--output", str(second)])
    for name in ["action_list.csv", "category_review.html", review.METHOD_FILE]:
        assert (first / name).read_bytes() == (second / name).read_bytes()
    assert "run_date" not in review.ACTION_LIST_COLUMNS
