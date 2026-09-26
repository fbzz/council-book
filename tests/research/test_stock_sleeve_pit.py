"""Tests PORTED from the lab study (stock-runner-research at commit d569049) for the ported module
src/council/stocks/pit.py (moved byte for byte from scripts/stock_sleeve_pit.py before the tag), plus
tests for the council-book additions at the end of this file.

Ported unchanged apart from imports: tests/test_fundamentals_pit.py (all), tests/test_fundamental_features.py
(all) and the three rank_average tests of tests/test_baselines.py.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from council.paths import REPO_ROOT
from council.stocks import pit as F

FUNDAMENTAL_FEATURE_KEYS = F.FUNDAMENTAL_FEATURE_KEYS
fundamental_features = F.fundamental_features
rank_average = F.rank_average


# ==================================================================================================
# PORTED: tests/test_fundamentals_pit.py
# ==================================================================================================
CIK = "0000000001"


def fact(end, val, accn, form, filed, start=None, fy=2023, fp="Q1"):
    row = {"end": end, "val": val, "accn": accn, "fy": fy, "fp": fp, "form": form, "filed": filed}
    if start is not None:
        row["start"] = start
    return row


def companyfacts(us_gaap: dict[str, list[dict]], dei: dict[str, list[dict]] | None = None) -> dict:
    facts: dict = {
        "us-gaap": {c: {"label": c, "units": {"USD": rows}} for c, rows in us_gaap.items()},
    }
    if dei:
        facts["dei"] = {c: {"label": c, "units": {"shares": rows}} for c, rows in dei.items()}
    return {"cik": 1, "entityName": "TEST", "facts": facts}


def build(us_gaap, dei=None):
    df, stats = F.fundamentals_for_company("TEST", CIK, companyfacts(us_gaap, dei))
    return df, stats


def q(period: str) -> tuple[str, str]:
    """(start, end) for a calendar quarter, e.g. '2023Q1' -> ('2023-01-01', '2023-03-31')."""
    p = pd.Period(period, freq="Q")
    return str(p.start_time.date()), str(p.end_time.date())


# --------------------------------------------------------------------------------------
# (a) restatements
# --------------------------------------------------------------------------------------
def test_restated_quarter_keeps_first_reported_value_and_date():
    s, e = q("2023Q1")
    rows = [
        fact(e, 100.0, "0001-23-000001", "10-Q", "2023-05-01", start=s),
        # the same quarter, restated downward in a filing a year later
        fact(e, 90.0, "0001-24-000009", "10-K", "2024-02-15", start=s, fp="FY"),
        # and repeated again, unchanged, as a comparative
        fact(e, 90.0, "0001-24-000020", "10-Q", "2024-05-01", start=s),
    ]
    df, _ = build({"Revenues": rows})
    assert len(df) == 1
    row = df.iloc[0]
    assert row["revenue"] == 100.0, "first-reported value must survive the restatement"
    assert row["available_at"] == pd.Timestamp("2023-05-01")
    assert row["accession"] == "0001-23-000001"
    assert row["form"] == "10-Q"
    assert not bool(row["is_derived_q4"])


def test_later_filing_never_supplies_the_value_even_when_it_comes_first_in_the_array():
    s, e = q("2023Q2")
    rows = [
        fact(e, 250.0, "0001-24-000009", "10-K", "2024-02-15", start=s, fp="FY"),
        fact(e, 200.0, "0001-23-000005", "10-Q", "2023-08-01", start=s, fp="Q2"),
    ]
    df, _ = build({"Revenues": rows})
    assert df.iloc[0]["revenue"] == 200.0
    assert df.iloc[0]["available_at"] == pd.Timestamp("2023-08-01")


# --------------------------------------------------------------------------------------
# (b) Q4 derivation
# --------------------------------------------------------------------------------------
def _fy2023_revenue_facts():
    q1s, q1e = q("2023Q1")
    q2s, q2e = q("2023Q2")
    q3s, q3e = q("2023Q3")
    return [
        fact(q1e, 100.0, "0001-23-000001", "10-Q", "2023-05-01", start=q1s, fp="Q1"),
        fact(q2e, 200.0, "0001-23-000005", "10-Q", "2023-08-01", start=q2s, fp="Q2"),
        fact(q3e, 300.0, "0001-23-000008", "10-Q", "2023-11-01", start=q3s, fp="Q3"),
        # the same three quarters as restated inside the 10-K itself
        fact(q1e, 110.0, "0001-24-000009", "10-K", "2024-02-15", start=q1s, fp="FY"),
        fact(q2e, 210.0, "0001-24-000009", "10-K", "2024-02-15", start=q2s, fp="FY"),
        fact(q3e, 310.0, "0001-24-000009", "10-K", "2024-02-15", start=q3s, fp="FY"),
        # the fiscal year
        fact("2023-12-31", 1000.0, "0001-24-000009", "10-K", "2024-02-15", start="2023-01-01", fp="FY"),
    ]


def test_q4_is_derived_from_the_quarters_in_that_same_10k():
    df, _ = build({"Revenues": _fy2023_revenue_facts()})
    q4 = df[df["period_end"] == pd.Timestamp("2023-12-31")].iloc[0]
    assert q4["revenue"] == pytest.approx(1000.0 - (110.0 + 210.0 + 310.0))
    assert bool(q4["is_derived_q4"]) is True
    assert q4["available_at"] == pd.Timestamp("2024-02-15"), "Q4 is knowable only at the 10-K"
    assert q4["form"] == "10-K"
    assert q4["fp"] == "Q4"
    assert q4["accession"] == "0001-24-000009"
    # the three interim rows keep their own first-reported values
    assert df[df["period_end"] == pd.Timestamp("2023-03-31")].iloc[0]["revenue"] == 100.0


def test_q4_falls_back_to_first_reported_quarters_when_the_10k_does_not_repeat_them():
    rows = [r for r in _fy2023_revenue_facts() if not (r["accn"] == "0001-24-000009" and r["end"] != "2023-12-31")]
    df, _ = build({"Revenues": rows})
    q4 = df[df["period_end"] == pd.Timestamp("2023-12-31")].iloc[0]
    assert q4["revenue"] == pytest.approx(1000.0 - 600.0)
    assert bool(q4["is_derived_q4"]) is True


def test_q4_stays_nan_when_a_quarter_is_missing():
    rows = [r for r in _fy2023_revenue_facts() if r["end"] != "2023-09-30"]
    df, _ = build({"Revenues": rows})
    q4 = df[df["period_end"] == pd.Timestamp("2023-12-31")]
    assert q4.empty or pd.isna(q4.iloc[0]["revenue"]), "never guess Q4 from an incomplete year"


def test_directly_reported_q4_is_not_flagged_as_derived():
    rows = _fy2023_revenue_facts()
    rows.append(fact("2023-12-31", 400.0, "0001-24-000009", "10-K", "2024-02-15", start="2023-10-01", fp="FY"))
    df, _ = build({"Revenues": rows})
    q4 = df[df["period_end"] == pd.Timestamp("2023-12-31")].iloc[0]
    assert q4["revenue"] == 400.0
    assert bool(q4["is_derived_q4"]) is False


# --------------------------------------------------------------------------------------
# (c) available_at filtering
# --------------------------------------------------------------------------------------
def test_quarter_is_invisible_to_a_snapshot_taken_before_the_10q_was_filed():
    df, _ = build({"Revenues": _fy2023_revenue_facts()})
    asof = pd.Timestamp("2023-04-15")  # Q1 ended, 10-Q not yet filed (filed 2023-05-01)
    visible = df[df["available_at"] <= asof]
    assert visible.empty
    later = df[df["available_at"] <= pd.Timestamp("2023-05-01")]
    assert list(later["period_end"]) == [pd.Timestamp("2023-03-31")]
    # and the fiscal year end is still invisible in January, before the 10-K
    jan = df[df["available_at"] <= pd.Timestamp("2024-01-31")]
    assert pd.Timestamp("2023-12-31") not in set(jan["period_end"])


# --------------------------------------------------------------------------------------
# (d) year-to-date facts
# --------------------------------------------------------------------------------------
def test_ytd_durations_are_never_read_as_quarterly_values():
    q1s, q1e = q("2023Q1")
    rows = [
        fact(q1e, 100.0, "0001-23-000001", "10-Q", "2023-05-01", start=q1s, fp="Q1"),
        fact("2023-06-30", 250.0, "0001-23-000005", "10-Q", "2023-08-01", start="2023-01-01", fp="Q2"),
        fact("2023-09-30", 460.0, "0001-23-000008", "10-Q", "2023-11-01", start="2023-01-01", fp="Q3"),
    ]
    df, _ = build({"NetCashProvidedByUsedInOperatingActivities": rows})
    by_end = df.set_index("period_end")["cfo"]
    assert by_end[pd.Timestamp("2023-03-31")] == 100.0
    assert by_end[pd.Timestamp("2023-06-30")] == pytest.approx(150.0), "H1 minus Q1, not the H1 total"
    assert by_end[pd.Timestamp("2023-09-30")] == pytest.approx(210.0), "9M minus H1, not the 9M total"


def test_lone_ytd_fact_yields_no_quarter():
    rows = [fact("2023-06-30", 250.0, "0001-23-000005", "10-Q", "2023-08-01", start="2023-01-01", fp="Q2")]
    df, _ = build({"Revenues": rows})
    assert df.empty, "a half-year fact with nothing to subtract is not a quarter"


def test_ytd_quarter_carries_the_reporting_filing_as_available_at():
    q1s, q1e = q("2023Q1")
    rows = [
        fact(q1e, 100.0, "0001-23-000001", "10-Q", "2023-05-01", start=q1s, fp="Q1"),
        fact("2023-06-30", 250.0, "0001-23-000005", "10-Q", "2023-08-01", start="2023-01-01", fp="Q2"),
    ]
    df, _ = build({"NetCashProvidedByUsedInOperatingActivities": rows})
    row = df[df["period_end"] == pd.Timestamp("2023-06-30")].iloc[0]
    assert row["available_at"] == pd.Timestamp("2023-08-01")
    assert row["accession"] == "0001-23-000005"


# --------------------------------------------------------------------------------------
# (e) shares outstanding
# --------------------------------------------------------------------------------------
def test_shares_are_summed_across_classes_and_duplicates_ignored():
    dei_rows = [
        fact("2023-03-31", 1000.0, "0001-23-000001", "10-Q", "2023-05-01"),  # class A
        fact("2023-03-31", 500.0, "0001-23-000001", "10-Q", "2023-05-01"),  # class B
        fact("2023-03-31", 500.0, "0001-23-000001", "10-Q", "2023-05-01"),  # same fact, repeated context
    ]
    df, _ = build(
        {"Revenues": [fact(q("2023Q1")[1], 100.0, "0001-23-000001", "10-Q", "2023-05-01", start=q("2023Q1")[0])]},
        dei={"EntityCommonStockSharesOutstanding": dei_rows},
    )
    assert df.iloc[0]["shares_outstanding"] == 1500.0


def test_shares_fall_back_to_the_cover_page_of_the_filing_that_supplied_available_at():
    # dei dates are cover-page dates, a few weeks after period end; the filing still ties them together
    dei_rows = [fact("2023-04-21", 2000.0, "0001-23-000001", "10-Q", "2023-05-01")]
    df, _ = build(
        {"Revenues": [fact(q("2023Q1")[1], 100.0, "0001-23-000001", "10-Q", "2023-05-01", start=q("2023Q1")[0])]},
        dei={"EntityCommonStockSharesOutstanding": dei_rows},
    )
    assert df.iloc[0]["shares_outstanding"] == 2000.0


# --------------------------------------------------------------------------------------
# instants, concept priority, masking
# --------------------------------------------------------------------------------------
def test_instants_are_taken_at_period_end_with_the_earliest_filing():
    q1s, q1e = q("2023Q1")
    rev = [fact(q1e, 100.0, "0001-23-000001", "10-Q", "2023-05-01", start=q1s)]
    cash = [
        fact(q1e, 50.0, "0001-23-000001", "10-Q", "2023-05-01"),
        fact(q1e, 55.0, "0001-24-000009", "10-K", "2024-02-15"),  # restated later
        fact("2023-06-30", 70.0, "0001-23-000005", "10-Q", "2023-08-01"),  # different date
    ]
    debt_lt = [fact(q1e, 800.0, "0001-23-000001", "10-Q", "2023-05-01")]
    debt_cur = [fact(q1e, 200.0, "0001-23-000001", "10-Q", "2023-05-01")]
    df, _ = build(
        {
            "Revenues": rev,
            "CashAndCashEquivalentsAtCarryingValue": cash,
            "LongTermDebt": debt_lt,
            "DebtCurrent": debt_cur,
        }
    )
    row = df.iloc[0]
    assert row["cash"] == 50.0
    assert row["debt_total"] == 1000.0


def test_concept_priority_first_with_data_wins():
    q1s, q1e = q("2023Q1")
    df, _ = build(
        {
            "Revenues": [fact(q1e, 100.0, "0001-23-000001", "10-Q", "2023-05-01", start=q1s)],
            "SalesRevenueNet": [fact(q1e, 111.0, "0001-23-000001", "10-Q", "2023-05-01", start=q1s)],
        }
    )
    assert df.iloc[0]["revenue"] == 100.0
    # with the priority concept absent, the next one supplies the period
    df2, _ = build({"SalesRevenueNet": [fact(q1e, 111.0, "0001-23-000001", "10-Q", "2023-05-01", start=q1s)]})
    assert df2.iloc[0]["revenue"] == 111.0


def test_gross_profit_falls_back_to_revenue_minus_cost_of_revenue():
    q1s, q1e = q("2023Q1")
    df, _ = build(
        {
            "Revenues": [fact(q1e, 100.0, "0001-23-000001", "10-Q", "2023-05-01", start=q1s)],
            "CostOfRevenue": [fact(q1e, 40.0, "0001-23-000001", "10-Q", "2023-05-01", start=q1s)],
        }
    )
    assert df.iloc[0]["gross_profit"] == pytest.approx(60.0)


def test_a_field_first_reported_later_than_the_row_is_masked_not_leaked():
    q1s, q1e = q("2023Q1")
    df, stats = build(
        {
            "Revenues": [fact(q1e, 100.0, "0001-23-000001", "10-Q", "2023-05-01", start=q1s)],
            # capex for the same quarter shows up only in next year's 10-K
            "PaymentsToAcquirePropertyPlantAndEquipment": [
                fact(q1e, 7.0, "0001-24-000009", "10-K", "2024-02-15", start=q1s, fp="FY")
            ],
        }
    )
    row = df.iloc[0]
    assert row["available_at"] == pd.Timestamp("2023-05-01")
    assert pd.isna(row["capex"]), "a value filed after available_at would be lookahead"
    assert stats["fields_masked"] == 1


def test_ifrs_filer_uses_the_ifrs_concept_names():
    q1s, q1e = q("2023Q1")
    cf = {
        "cik": 1,
        "facts": {
            "ifrs-full": {
                "Revenue": {
                    "units": {"USD": [fact(q1e, 500.0, "0001-23-000001", "6-K", "2023-05-01", start=q1s)]}
                }
            }
        },
    }
    df, stats = F.fundamentals_for_company("TEST", CIK, cf)
    assert stats["taxonomy"] == "ifrs-full"
    assert df.iloc[0]["revenue"] == 500.0


def test_company_with_no_recognised_facts_produces_no_rows():
    df, stats = F.fundamentals_for_company("TEST", CIK, {"cik": 1, "facts": {}})
    assert df.empty
    assert stats["taxonomy"] == "none"
    assert list(df.columns) == F.FUNDAMENTALS_COLUMNS


def test_output_columns_and_sort_order_match_the_contract():
    df, _ = build({"Revenues": _fy2023_revenue_facts()})
    assert list(df.columns) == F.FUNDAMENTALS_COLUMNS
    assert df["period_end"].is_monotonic_increasing
    assert df["available_at"].dtype.kind == "M"
    assert df["is_derived_q4"].dtype == bool


# ==================================================================================================
# PORTED: tests/test_fundamental_features.py
# ==================================================================================================
def make_fund() -> pd.DataFrame:
    """Build the synthetic fundamentals DataFrame used in most tests."""
    period_ends = [
        "2023-03-31",
        "2023-06-30",
        "2023-09-30",
        "2023-12-31",
        "2024-03-31",
        "2024-06-30",
        "2024-09-30",
        "2024-12-31",
        "2025-03-31",
    ]
    revenue = [100, 110, 120, 130, 150, 176, 204, 234, 270]
    gross_profit = [0.6 * r for r in revenue]
    gross_profit[1] = np.nan  # 2023-06-30
    operating_income = [0.1 * r for r in revenue]
    cfo = [0.2 * r for r in revenue]
    capex = [0.05 * r for r in revenue]
    cash = [1000 + 10 * i for i in range(9)]
    debt_total = [300] * 9
    debt_total[-1] = np.nan  # 2025-03-31

    rows = []
    for i, pe in enumerate(period_ends):
        pe_ts = pd.Timestamp(pe)
        is_q4 = pe.endswith("12-31")
        avail = pe_ts + pd.Timedelta(days=60 if is_q4 else 40)
        rows.append(
            {
                "ticker": "AAA",
                "cik": "0001234567",
                "period_end": pe_ts,
                "fy": pe_ts.year,
                "fp": "Q4" if is_q4 else f"Q{(pe_ts.month - 1) // 3 + 1}",
                "form": "10-K" if is_q4 else "10-Q",
                "accession": f"0001234567-{pe_ts.year}-{i:04d}",
                "available_at": avail,
                "revenue": revenue[i],
                "gross_profit": gross_profit[i],
                "operating_income": operating_income[i],
                "net_income": operating_income[i],
                "cfo": cfo[i],
                "capex": capex[i],
                "cash": cash[i],
                "debt_total": debt_total[i],
                "shares_outstanding": 100_000_000 + i,
                "is_derived_q4": is_q4,
            }
        )

    # Add two rows for a different ticker
    rows.append(
        {
            "ticker": "BBB",
            "cik": "0007654321",
            "period_end": pd.Timestamp("2024-03-31"),
            "fy": 2024,
            "fp": "Q1",
            "form": "10-Q",
            "accession": "0007654321-2024-0001",
            "available_at": pd.Timestamp("2024-05-10"),
            "revenue": 500,
            "gross_profit": 300,
            "operating_income": 50,
            "net_income": 40,
            "cfo": 100,
            "capex": 20,
            "cash": 2000,
            "debt_total": 500,
            "shares_outstanding": 50_000_000,
            "is_derived_q4": False,
        }
    )
    rows.append(
        {
            "ticker": "BBB",
            "cik": "0007654321",
            "period_end": pd.Timestamp("2024-06-30"),
            "fy": 2024,
            "fp": "Q2",
            "form": "10-Q",
            "accession": "0007654321-2024-0002",
            "available_at": pd.Timestamp("2024-08-09"),
            "revenue": 550,
            "gross_profit": 330,
            "operating_income": 55,
            "net_income": 45,
            "cfo": 110,
            "capex": 22,
            "cash": 2100,
            "debt_total": 500,
            "shares_outstanding": 50_000_000,
            "is_derived_q4": False,
        }
    )

    return pd.DataFrame(rows)


def test_keys():
    fund = make_fund()
    result = fundamental_features(fund, "AAA", pd.Timestamp("2025-03-15"))
    assert list(result.keys()) == list(FUNDAMENTAL_FEATURE_KEYS)


def test_growth_hand_check():
    fund = make_fund()
    result = fundamental_features(fund, "AAA", pd.Timestamp("2025-03-15"))
    assert result["revenue_growth_yoy"] == pytest.approx(234 / 130 - 1)
    assert result["revenue_growth_qoq"] == pytest.approx(234 / 204 - 1)
    assert result["revenue_growth_yoy_prev"] == pytest.approx(204 / 120 - 1)
    assert result["revenue_growth_acceleration"] == pytest.approx(
        result["revenue_growth_yoy"] - result["revenue_growth_yoy_prev"]
    )
    assert result["gross_margin"] == pytest.approx(0.6)
    assert result["gross_margin_change_yoy"] == pytest.approx(0.0)
    assert result["operating_margin_change_yoy"] == pytest.approx(0.0)
    assert result["fcf_margin"] == pytest.approx(0.15)


def test_no_lookahead_agent_example():
    fund = make_fund()
    # Before the 2025-03-31 filing is available
    result = fundamental_features(fund, "AAA", pd.Timestamp("2025-04-15"))
    assert result["latest_period_end"] == pd.Timestamp("2024-12-31")
    # After it becomes available
    result2 = fundamental_features(fund, "AAA", pd.Timestamp("2025-05-12"))
    assert result2["latest_period_end"] == pd.Timestamp("2025-03-31")


def test_derived_q4_visibility():
    fund = make_fund()
    result = fundamental_features(fund, "AAA", pd.Timestamp("2024-02-20"))
    assert result["latest_period_end"] == pd.Timestamp("2023-09-30")


def test_missing_yearago_margin():
    fund = make_fund()
    result = fundamental_features(fund, "AAA", pd.Timestamp("2024-09-01"))
    assert np.isfinite(result["gross_margin"])
    assert np.isnan(result["gross_margin_change_yoy"])
    assert np.isfinite(result["revenue_growth_yoy"])


def test_net_cash_nan_when_debt_missing():
    fund = make_fund()
    result = fundamental_features(fund, "AAA", pd.Timestamp("2025-06-01"))
    assert np.isfinite(result["cash"])
    assert np.isnan(result["debt_total"])
    assert np.isnan(result["net_cash"])


def test_empty_before_first_filing():
    fund = make_fund()
    result = fundamental_features(fund, "AAA", pd.Timestamp("2023-01-01"))
    for key in FUNDAMENTAL_FEATURE_KEYS:
        if key in ("quarters_of_history",):
            assert result[key] == 0
        elif key in ("latest_period_end", "latest_available_at"):
            assert pd.isna(result[key])
        else:
            assert np.isnan(result[key])


def test_other_ticker_ignored():
    fund = make_fund()
    result_with_bbb = fundamental_features(fund, "AAA", pd.Timestamp("2025-03-15"))
    fund_no_bbb = fund[fund["ticker"] != "BBB"].reset_index(drop=True)
    result_without_bbb = fundamental_features(fund_no_bbb, "AAA", pd.Timestamp("2025-03-15"))
    assert result_with_bbb == result_without_bbb


def test_fiscal_offset_matching():
    # Fiscal quarters with 52/53-week offsets
    period_ends = ["2023-01-29", "2023-04-30", "2023-07-30", "2023-10-29", "2024-01-28"]
    revenue = [100, 110, 120, 130, 150]
    rows = []
    for i, pe in enumerate(period_ends):
        pe_ts = pd.Timestamp(pe)
        rows.append(
            {
                "ticker": "AAA",
                "cik": "0001234567",
                "period_end": pe_ts,
                "fy": pe_ts.year,
                "fp": f"Q{i+1}",
                "form": "10-Q",
                "accession": f"acc-{i}",
                "available_at": pe_ts + pd.Timedelta(days=40),
                "revenue": revenue[i],
                "gross_profit": 0.6 * revenue[i],
                "operating_income": 0.1 * revenue[i],
                "net_income": 0.1 * revenue[i],
                "cfo": 0.2 * revenue[i],
                "capex": 0.05 * revenue[i],
                "cash": 1000,
                "debt_total": 300,
                "shares_outstanding": 100_000_000,
                "is_derived_q4": False,
            }
        )
    fund = pd.DataFrame(rows)
    result = fundamental_features(fund, "AAA", pd.Timestamp("2024-03-15"))
    assert np.isfinite(result["revenue_growth_yoy"])
    assert np.isfinite(result["revenue_growth_qoq"])


def test_counts():
    fund = make_fund()
    result = fundamental_features(fund, "AAA", pd.Timestamp("2025-03-15"))
    assert result["quarters_of_history"] == 8
    assert result["days_since_last_filing"] == 14


def test_input_not_mutated():
    fund = make_fund()
    original = fund.copy()
    fundamental_features(fund, "AAA", pd.Timestamp("2025-03-15"))
    pd.testing.assert_frame_equal(fund, original)


# ==================================================================================================
# PORTED: tests/test_baselines.py (rank_average)
# ==================================================================================================
def test_rank_average_hand_values() -> None:
    df = pd.DataFrame({"a": [3, 1, np.nan, 2], "b": [1, 2, 3, 4]})

    score_a = rank_average(df, ["a"])
    assert score_a == pytest.approx([1.0, 1 / 3, 2 / 3, 2 / 3])

    score_ab = rank_average(df, ["a", "b"])
    expected_ab = [
        (1.0 + 0.25) / 2,
        (1 / 3 + 0.5) / 2,
        (2 / 3 + 0.75) / 2,
        (2 / 3 + 1.0) / 2,
    ]
    assert score_ab == pytest.approx(expected_ab)

    score_flip = rank_average(df, ["a", "b"], signs=(-1, 1))
    expected_flip = [
        (1 / 3 + 0.25) / 2,
        (1.0 + 0.5) / 2,
        (2 / 3 + 0.75) / 2,
        (2 / 3 + 1.0) / 2,
    ]
    assert score_flip == pytest.approx(expected_flip)

def test_rank_average_missing_column_raises_keyerror() -> None:
    df = pd.DataFrame({"a": [1, 2]})
    with pytest.raises(KeyError) as excinfo:
        rank_average(df, ["a", "b"])
    assert "b" in str(excinfo.value)

def test_rank_average_all_nan_column_is_half() -> None:
    df = pd.DataFrame({"a": [np.nan, np.nan, np.nan]})
    score = rank_average(df, ["a"])
    assert np.allclose(score, [0.5, 0.5, 0.5])


# ==================================================================================================
# council-book additions
# ==================================================================================================
def _retagged_revenue():
    """Q2 2017 first filed under SalesRevenueNet; a 2018 10-K re-tags it under Revenues (restated)."""
    s, e = q("2017Q2")
    return {
        "SalesRevenueNet": [fact(e, 100.0, "0001-17-000005", "10-Q", "2017-08-02", start=s, fp="Q2")],
        "Revenues": [fact(e, 98.0, "0001-18-000030", "10-K", "2018-11-05", start=s, fp="FY")],
    }


def test_lab_resolver_dates_a_retagged_quarter_at_the_retagging_filing():
    df, _ = F.fundamentals_for_company("TEST", CIK, companyfacts(_retagged_revenue()))
    row = df.iloc[0]
    assert row["available_at"] == pd.Timestamp("2018-11-05") and row["revenue"] == 98.0


def test_earliest_resolver_keeps_the_first_reported_value_and_date():
    df, _ = F.fundamentals_first_reported("TEST", CIK, companyfacts(_retagged_revenue()))
    row = df.iloc[0]
    assert row["available_at"] == pd.Timestamp("2017-08-02")
    assert row["revenue"] == 100.0
    assert row["form"] == "10-Q"


def test_earliest_resolver_ties_go_to_the_lab_priority():
    s, e = q("2023Q1")
    cf = companyfacts({
        "Revenues": [fact(e, 120.0, "0001-23-000001", "10-Q", "2023-05-01", start=s)],
        "SalesRevenueNet": [fact(e, 111.0, "0001-23-000001", "10-Q", "2023-05-01", start=s)],
    })
    df, _ = F.fundamentals_first_reported("TEST", CIK, cf)
    assert df.iloc[0]["revenue"] == 120.0


def test_earliest_resolver_prefers_the_original_q4_derivation_over_a_later_direct_q4():
    q1s, q1e = q("2016Q1")
    q2s, q2e = q("2016Q2")
    q3s, q3e = q("2016Q3")
    rows = [
        fact(q1e, 100.0, "0001-16-000001", "10-Q", "2016-05-01", start=q1s, fp="Q1"),
        fact(q2e, 100.0, "0001-16-000002", "10-Q", "2016-08-01", start=q2s, fp="Q2"),
        fact(q3e, 100.0, "0001-16-000003", "10-Q", "2016-11-01", start=q3s, fp="Q3"),
        fact("2016-12-31", 450.0, "0001-17-000004", "10-K", "2017-02-15", start="2016-01-01", fp="FY"),
    ]
    later_direct = [fact("2016-12-31", 149.0, "0001-18-000009", "10-K", "2018-02-15", start="2016-10-01", fp="FY")]
    cf = companyfacts({"SalesRevenueNet": rows, "Revenues": later_direct})
    df, _ = F.fundamentals_first_reported("TEST", CIK, cf)
    q4 = df[df["period_end"] == pd.Timestamp("2016-12-31")].iloc[0]
    assert q4["available_at"] == pd.Timestamp("2017-02-15")
    assert q4["revenue"] == pytest.approx(150.0)
    lab, _ = F.fundamentals_for_company("TEST", CIK, cf)
    assert lab[lab["period_end"] == pd.Timestamp("2016-12-31")].iloc[0]["available_at"] == pd.Timestamp("2018-02-15")


def test_cost_of_goods_sold_is_the_last_gross_profit_fallback_in_the_council_build_only():
    s, e = q("2016Q1")
    cf = companyfacts({
        "SalesRevenueNet": [fact(e, 100.0, "0001-16-000001", "10-Q", "2016-05-01", start=s)],
        "CostOfGoodsSold": [fact(e, 70.0, "0001-16-000001", "10-Q", "2016-05-01", start=s)],
    })
    council, _ = F.fundamentals_first_reported("TEST", CIK, cf)
    lab, _ = F.fundamentals_for_company("TEST", CIK, cf)
    assert council.iloc[0]["gross_profit"] == pytest.approx(30.0)
    assert pd.isna(lab.iloc[0]["gross_profit"])


def test_the_council_build_restores_the_lab_globals():
    F.fundamentals_first_reported("TEST", CIK, companyfacts(_retagged_revenue()))
    assert F.resolve_concept_list is F._LAB_RESOLVER
    assert F.COST_CONCEPTS is F._LAB_COST_CONCEPTS
    assert "CostOfGoodsSold" not in F.COST_CONCEPTS[F.US_GAAP]


@pytest.mark.parametrize("asof", ["2024-02-20", "2024-09-01", "2025-03-15", "2025-06-01"])
def test_rule_quarters_are_the_quarters_the_features_compare(asof):
    fund = make_fund()
    ts = pd.Timestamp(asof)
    feats = fundamental_features(fund, "AAA", ts)
    rq = F.rule_quarters(fund, "AAA", ts)
    if np.isfinite(feats["revenue_growth_yoy"]):
        assert rq["revenue_L"] / rq["revenue_Y"] - 1 == pytest.approx(feats["revenue_growth_yoy"])
    if np.isfinite(feats["revenue_growth_yoy_prev"]):
        assert rq["revenue_P"] / rq["revenue_PY"] - 1 == pytest.approx(feats["revenue_growth_yoy_prev"])
    assert rq["form_L"] in F.DOMESTIC_FORMS


def test_rule_quarters_empty_before_the_first_filing():
    rq = F.rule_quarters(make_fund(), "AAA", pd.Timestamp("2023-01-01"))
    assert np.isnan(rq["revenue_L"]) and rq["form_L"] is None


# --------------------------------------------------------------------------------------------------
# change (c): comparable year-ago values
# --------------------------------------------------------------------------------------------------
SEC_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "sec" / "companyfacts_trimmed.json"


@pytest.mark.parametrize("asof", ["2024-02-20", "2024-05-15", "2024-09-01", "2025-03-15", "2025-06-01"])
def test_comparable_features_equal_the_lab_features_when_the_year_ago_values_are_the_first_reports(asof):
    fund = make_fund()
    ts = pd.Timestamp(asof)
    lab = fundamental_features(fund, "AAA", ts)
    cmp_ = F.comparable_features(F.year_ago_from_rows(fund), "AAA", ts)
    for k in F.FUNDAMENTAL_COLUMNS:
        a, b = float(lab[k]), float(cmp_[k])
        assert (np.isnan(a) and np.isnan(b)) or a == pytest.approx(b), k
    assert cmp_["latest_available_at"] == lab["latest_available_at"]
    assert cmp_["quarters_of_history"] == lab["quarters_of_history"]


def _retag_with_comparative():
    """2015Q1 first filed under Revenues with a partial figure (40); the 2016Q1 10-Q tags both
    quarters under SalesRevenueNet (100 and 90). The lab pairs 100 with 40; change (c) with 90."""
    s15, e15 = q("2015Q1")
    s16, e16 = q("2016Q1")
    return {
        "Revenues": [fact(e15, 40.0, "0001-15-000001", "10-Q", "2015-05-01", start=s15)],
        "SalesRevenueNet": [fact(e15, 90.0, "0001-16-000001", "10-Q", "2016-05-02", start=s15),
                            fact(e16, 100.0, "0001-16-000001", "10-Q", "2016-05-02", start=s16)],
        "GrossProfit": [fact(e15, 36.0, "0001-15-000001", "10-Q", "2015-05-01", start=s15),
                        fact(e15, 36.0, "0001-16-000001", "10-Q", "2016-05-02", start=s15),
                        fact(e16, 45.0, "0001-16-000001", "10-Q", "2016-05-02", start=s16)],
    }


def test_year_ago_comes_from_the_compared_quarters_own_filing_and_concept():
    df, _ = F.fundamentals_comparable("TEST", CIK, companyfacts(_retag_with_comparative()))
    l16 = df[df["period_end"] == pd.Timestamp("2016-03-31")].iloc[0]
    y15 = df[df["period_end"] == pd.Timestamp("2015-03-31")].iloc[0]
    assert y15["revenue"] == 40.0                       # the year-ago row keeps its own first report
    assert l16["ya_period_end"] == pd.Timestamp("2015-03-31")
    assert l16["revenue_ya"] == 90.0                    # same concept, as printed in the 2016 filing
    assert l16["gross_profit_ya"] == 36.0
    first_reported, _ = F.fundamentals_first_reported("TEST", CIK, companyfacts(_retag_with_comparative()))
    lab = fundamental_features(first_reported, "TEST", pd.Timestamp("2016-06-01"))
    assert lab["revenue_growth_yoy"] == pytest.approx(100 / 40 - 1)


def test_a_restated_year_ago_q4_is_derived_from_the_restated_figures():
    """FY2017 first reported (old standard) with quarters of 20 and a year of 100 (Q4 = 40). The FY2018
    10-Qs print restated 2017 quarters of 25; the FY2018 10-K prints the restated 2017 year (120) and
    2018 (160 with quarters of 30): L = Q4 2018 = 70 against the restated Q4 2017 = 120 - 75 = 45."""
    ga = "RevenueFromContractWithCustomerExcludingAssessedTax"
    old, new = [], []
    for k in (1, 2, 3):
        s17, e17 = q(f"2017Q{k}")
        s18, e18 = q(f"2018Q{k}")
        old.append(fact(e17, 20.0, f"0001-17-00000{k}", "10-Q", f"2017-{3 * k + 2:02d}-01", start=s17))
        new.append(fact(e17, 25.0, f"0001-18-00000{k}", "10-Q", f"2018-{3 * k + 2:02d}-01", start=s17))
        new.append(fact(e18, 30.0, f"0001-18-00000{k}", "10-Q", f"2018-{3 * k + 2:02d}-01", start=s18))
    old.append(fact("2017-12-31", 100.0, "0001-18-000010", "10-K", "2018-02-15", start="2017-01-01", fp="FY"))
    new.append(fact("2017-12-31", 120.0, "0001-19-000010", "10-K", "2019-02-15", start="2017-01-01", fp="FY"))
    new.append(fact("2018-12-31", 160.0, "0001-19-000010", "10-K", "2019-02-15", start="2018-01-01", fp="FY"))
    df, _ = F.fundamentals_comparable("TEST", CIK, companyfacts({"SalesRevenueNet": old, ga: new}))
    q4 = df[df["period_end"] == pd.Timestamp("2018-12-31")].iloc[0]
    assert q4["revenue"] == pytest.approx(70.0)
    assert q4["revenue_ya"] == pytest.approx(45.0)
    first = df[df["period_end"] == pd.Timestamp("2017-12-31")].iloc[0]
    assert first["revenue"] == pytest.approx(40.0)      # the lab would compare 70 with 40


def test_value_as_printed_prefers_the_own_filing_and_ignores_later_filings():
    s, e = q("2020Q1")
    facts = [F.Fact("R", pd.Timestamp(s), pd.Timestamp(e), 10.0, "A", 2020, "Q1", "10-Q", pd.Timestamp("2020-05-01"), "USD"),
             F.Fact("R", pd.Timestamp(s), pd.Timestamp(e), 11.0, "B", 2021, "Q1", "10-Q", pd.Timestamp("2021-05-01"), "USD"),
             F.Fact("R", pd.Timestamp(s), pd.Timestamp(e), 12.0, "C", 2022, "Q1", "10-Q", pd.Timestamp("2022-05-01"), "USD")]
    idx = F.PrintedIndex(facts)
    end = pd.Timestamp(e)
    assert F.value_as_printed(idx, end, pd.Timestamp("2021-06-01"), "B") == 11.0
    assert F.value_as_printed(idx, end, pd.Timestamp("2021-06-01"), "Z") == 11.0     # latest by the date
    assert F.value_as_printed(idx, end, pd.Timestamp("2020-06-01"), "C") == 10.0     # C is not filed yet
    assert F.value_as_printed(idx, end, pd.Timestamp("2019-06-01"), "A") is None


@pytest.mark.parametrize(("rev", "gp", "oi", "need", "ok"), [
    (100.0, 40.0, 10.0, True, True),
    (100.0, 120.0, 10.0, True, False),          # gross profit above revenue
    (100.0, -5.0, 10.0, True, False),           # negative gross profit
    (100.0, 40.0, -150.0, True, False),         # operating loss above revenue
    (100.0, np.nan, 10.0, True, False),         # a compared margin is missing
    (100.0, np.nan, np.nan, False, True),       # P and PY need only revenue
    (0.0, 0.0, 0.0, False, False),
])
def test_plausibility_guards(rev, gp, oi, need, ok):
    assert F.plausible_quarter(rev, gp, oi, need_margins=need) is ok


def _fixture_features(label: str, asof: str) -> dict:
    import json

    doc = json.loads(SEC_FIXTURE.read_text())["companies"][label]
    df, _ = F.fundamentals_comparable(label, str(doc["cik"]), doc["companyfacts"])
    ts = pd.Timestamp(asof)
    vis = df[df["available_at"] < ts]
    return {"cmp": F.comparable_features(vis, label, ts),
            "lab": fundamental_features(vis[F.FUNDAMENTALS_COLUMNS], label, ts)}


@pytest.mark.parametrize(("label", "asof", "lab_growth", "comparable_growth"), [
    ("HAS", "2016-05-20", 1.719, 0.165),        # the lab paired SalesRevenueNet with a partial Revenues figure
    ("HAS", "2016-08-22", 1.423, 0.102),
    ("HAS", "2016-11-21", 3.6305, 0.142),
    ("MSFT", "2018-08-20", 0.290, 0.175),       # FY18 (ASC 606) against FY17 as first reported (ASC 605)
    ("PM", "2018-08-20", -0.600, 0.117),
])
def test_regression_real_filers_compare_like_with_like(label, asof, lab_growth, comparable_growth):
    """`lab`: the lab feature function on the change (a)+(b) rows (the pairs the red team measured)."""
    f = _fixture_features(label, asof)
    assert float(f["lab"]["revenue_growth_yoy"]) == pytest.approx(lab_growth, abs=5e-4)
    assert float(f["cmp"]["revenue_growth_yoy"]) == pytest.approx(comparable_growth, abs=5e-4)
    assert f["cmp"]["plausible"]
    assert abs(float(f["cmp"]["gross_margin_change_yoy"])) < 0.05


def test_regression_has_2016_gross_margin_is_no_longer_a_120_point_jump():
    f = _fixture_features("HAS", "2016-11-21")
    assert float(f["lab"]["gross_margin_change_yoy"]) > 1.0
    assert abs(float(f["cmp"]["gross_margin_change_yoy"])) < 0.01
