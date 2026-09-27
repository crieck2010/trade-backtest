"""Tests for the adversarial hidden-data-validation suite.

Every test runs the REAL guards and the REAL pipeline (real Bars, real
ListDataHandler, real BacktestEngine, real AdjustedDataHandler) -- nothing
about the guards is mocked. Synthetic fixtures only; no network, no data
files.
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from trade_backtest.adversarial import (
    CASES,
    AdversarialFailure,
    guard_corporate_actions,
    guard_ffill_gap,
    guard_future_return_leakage,
    guard_timestamp_alignment,
    inject_ffill_gap,
    inject_future_return_leakage,
    inject_timestamp_shift,
    inject_unadjusted_corporate_action,
    main,
    make_clean_feed,
    run_adversarial_suite,
    run_case,
    run_guards,
)
from trade_backtest.total_return import Dividend, Split


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _clean(n: int = 300, seed: int = 7) -> list[dict]:
    return make_clean_feed(n=n, seed=seed)


# ---------------------------------------------------------------------------
# the sandbox feed itself
# ---------------------------------------------------------------------------

class TestCleanFeed:
    def test_shape_and_keys(self):
        feed = _clean()
        assert len(feed) == 300
        for row in feed:
            assert set(row) >= {"symbol", "timestamp", "open", "high", "low",
                                "close", "volume", "features"}
            assert set(row["features"]) >= {"mom5", "noise_a", "noise_b"}

    def test_timestamps_monotonic(self):
        feed = _clean()
        ts = [r["timestamp"] for r in feed]
        assert all(b > a for a, b in zip(ts, ts[1:]))

    def test_mom5_causal_and_recomputable(self):
        # mom5[i] uses only bars <= i, and matches the guard's recomputation.
        from trade_backtest.adversarial import _recompute_mom5
        feed = _clean()
        closes = [r["close"] for r in feed]
        recomputed = _recompute_mom5(closes)
        for r, m in zip(feed, recomputed):
            assert (r["features"]["mom5"] is None) == (m is None)
            if m is not None:
                assert r["features"]["mom5"] == pytest.approx(m)

    def test_closes_strictly_vary(self):
        # No accidental flat bars in the clean fixture.
        feed = _clean()
        closes = [r["close"] for r in feed]
        assert all(b != a for a, b in zip(closes, closes[1:]))


# ---------------------------------------------------------------------------
# injectors plant what they claim
# ---------------------------------------------------------------------------

class TestInjectors:
    def test_future_leak_feature_is_future_computed(self):
        feed = _clean()
        out = inject_future_return_leakage(feed, horizon=5)
        closes = [r["close"] for r in feed]
        for i in range(len(feed) - 5):
            expected = math.log(closes[i + 5] / closes[i])
            assert out[i]["features"]["future_leak"] == pytest.approx(expected, abs=0.05)
        # Trailing horizon rows: no future left, feature is None.
        assert all(out[i]["features"]["future_leak"] is None
                   for i in range(len(feed) - 5, len(feed)))
        # The input feed is untouched (pure function).
        assert "future_leak" not in feed[0]["features"]

    def test_timestamp_shift_moves_features_not_prices(self):
        feed = _clean()
        out = inject_timestamp_shift(feed, shift_bars=1)
        assert len(out) == len(feed)
        for a, b in zip(feed, out):
            assert a["timestamp"] == b["timestamp"]
            assert a["close"] == b["close"]
        # Features come from one bar in the future.
        for i in range(len(feed) - 1):
            assert out[i]["features"]["mom5"] == feed[i + 1]["features"]["mom5"]

    def test_timestamp_shift_rejects_bad_shift(self):
        feed = _clean()
        with pytest.raises(ValueError):
            inject_timestamp_shift(feed, shift_bars=0)
        with pytest.raises(ValueError):
            inject_timestamp_shift(feed, shift_bars=len(feed))

    def test_ffill_gap_flattens_to_future_close(self):
        feed = _clean()
        out = inject_ffill_gap(feed, start_frac=0.4, gap_bars=6)
        s = int(300 * 0.4)
        future_close = feed[s + 6]["close"]
        for i in range(s, s + 6):
            assert out[i]["open"] == out[i]["high"] == out[i]["low"] \
                == out[i]["close"] == future_close
            assert out[i]["volume"] == 0.0
        # Outside the gap the feed is identical.
        assert out[0]["close"] == feed[0]["close"]
        assert out[-1]["close"] == feed[-1]["close"]

    def test_split_jump_is_unadjusted(self):
        feed = _clean()
        out = inject_unadjusted_corporate_action(feed, kind="split", ratio=2.0,
                                                at_frac=0.5)
        m = 150
        assert out[0]["close"] == pytest.approx(feed[0]["close"] / 2.0)
        assert out[m]["close"] == pytest.approx(feed[m]["close"])
        # The bar at the event prints ~ +ln(2): the unadjusted discontinuity.
        r = math.log(out[m]["close"] / out[m - 1]["close"])
        assert r == pytest.approx(math.log(2.0), abs=0.05)

    def test_dividend_jump_and_volume_spike(self):
        feed = _clean()
        out = inject_unadjusted_corporate_action(feed, kind="dividend",
                                                dividend_yield=0.03,
                                                at_frac=0.5)
        m = 150
        assert out[m]["close"] == pytest.approx(feed[m]["close"] * 0.97)
        assert out[m]["volume"] == pytest.approx(feed[m]["volume"] * 5.0)
        # No rebound the next bar: the markdown persists.
        assert out[m + 1]["close"] == pytest.approx(feed[m + 1]["close"] * 0.97)

    def test_corporate_action_rejects_bad_kind(self):
        with pytest.raises(ValueError):
            inject_unadjusted_corporate_action(_clean(), kind="merger")


# ---------------------------------------------------------------------------
# guards: flag the planted leak, stay silent on clean data
# ---------------------------------------------------------------------------

class TestGuardsCatch:
    def test_future_return_leakage_caught(self):
        out = inject_future_return_leakage(_clean())
        f = guard_future_return_leakage(out, feature="future_leak", horizon=5)
        assert f["leakage_class"] == "future_return_leakage"
        assert f["flagged"] is True
        assert f["evidence"]["correlation"] > 0.9
        assert f["evidence"]["correlation"] > f["evidence"]["threshold"]

    def test_timestamp_shift_caught_with_lag_signature(self):
        out = inject_timestamp_shift(_clean(), shift_bars=1)
        f = guard_timestamp_alignment(out, feature="mom5", max_lag=5)
        assert f["leakage_class"] == "timestamp_shift"
        assert f["flagged"] is True
        # The mechanism signature: peak at lag +1, correlation ~1.
        assert f["evidence"]["peak_lag_bars"] == 1
        assert f["evidence"]["peak_correlation"] == pytest.approx(1.0)

    def test_timestamp_shift_two_bars(self):
        out = inject_timestamp_shift(_clean(), shift_bars=2)
        f = guard_timestamp_alignment(out, feature="mom5", max_lag=5)
        assert f["flagged"] is True
        assert f["evidence"]["peak_lag_bars"] == 2

    def test_ffill_gap_caught(self):
        out = inject_ffill_gap(_clean())
        f = guard_ffill_gap(out)
        assert f["leakage_class"] == "ffill_gap"
        assert f["flagged"] is True
        runs = f["evidence"]["flat_runs"]
        assert len(runs) == 1
        assert runs[0]["length"] == 6
        assert runs[0]["smuggles_future"] is True
        assert runs[0]["equals_pre_run_close"] is False

    def test_unadjusted_split_caught(self):
        out = inject_unadjusted_corporate_action(_clean(), kind="split")
        f = guard_corporate_actions(out, actions=(), basis="none")
        assert f["leakage_class"] == "unadjusted_corporate_action"
        assert f["flagged"] is True
        assert len(f["evidence"]["split_candidates"]) == 1
        hit = f["evidence"]["split_candidates"][0]
        assert hit["implied_ratio"] == pytest.approx(2.0)

    def test_unadjusted_dividend_caught(self):
        out = inject_unadjusted_corporate_action(_clean(), kind="dividend")
        f = guard_corporate_actions(out, actions=(), basis="none")
        assert f["flagged"] is True
        assert len(f["evidence"]["dividend_candidates"]) == 1
        assert f["evidence"]["dividend_candidates"][0]["drop"] == \
            pytest.approx(0.03, abs=0.005)


class TestGuardsSilentOnClean:
    def test_future_leak_guard_silent_on_noise_features(self):
        feed = _clean()
        for feat in ("noise_a", "noise_b", "mom5"):
            f = guard_future_return_leakage(feed, feature=feat, horizon=5)
            assert f["flagged"] is False, feat

    def test_timestamp_guard_silent_on_clean(self):
        f = guard_timestamp_alignment(_clean())
        assert f["flagged"] is False
        assert f["evidence"]["peak_lag_bars"] == 0

    def test_ffill_guard_silent_on_clean(self):
        f = guard_ffill_gap(_clean())
        assert f["flagged"] is False
        assert f["evidence"]["flat_runs"] == []

    def test_corporate_guard_silent_on_clean(self):
        f = guard_corporate_actions(_clean(), actions=(), basis="none")
        assert f["flagged"] is False

    def test_run_guards_silent_on_clean(self):
        findings = run_guards(_clean())
        assert findings and all(not f["flagged"] for f in findings)


class TestDeclaredActionsClearTheFlag:
    def _split_date(self, feed):
        out = inject_unadjusted_corporate_action(feed, kind="split", ratio=2.0,
                                                at_frac=0.5)
        return out, out[150]["timestamp"].date()

    def test_declared_split_clears_flag_via_real_adjustment(self):
        # The real AdjustedDataHandler backward-adjusts the declared split
        # away, so the guard -- which scans the adjusted stream -- goes quiet.
        feed = _clean()
        out, ex = self._split_date(feed)
        f = guard_corporate_actions(out, actions=[Split("ADV", ex, 2.0)],
                                   basis="unadjusted_with_events")
        assert f["flagged"] is False

    def test_declared_dividend_clears_flag_via_declaration_check(self):
        feed = _clean()
        out = inject_unadjusted_corporate_action(feed, kind="dividend",
                                                at_frac=0.5)
        ex = out[150]["timestamp"].date()
        assert isinstance(ex, date)
        f = guard_corporate_actions(
            out, actions=[Dividend("ADV", ex, 3.0)],
            basis="unadjusted_with_events")
        assert f["flagged"] is False

    def test_wrong_date_declaration_does_not_clear(self):
        feed = _clean()
        out, ex = self._split_date(feed)
        wrong = date(ex.year - 1, ex.month, ex.day)
        f = guard_corporate_actions(out, actions=[Split("ADV", wrong, 2.0)],
                                   basis="unadjusted_with_events")
        assert f["flagged"] is True


# ---------------------------------------------------------------------------
# runner: individually addressable cases + full suite + CLI
# ---------------------------------------------------------------------------

class TestRunner:
    def test_cases_registry_covers_four_classes(self):
        assert sorted(CASES) == ["ffill_gap", "future_return_leakage",
                                 "timestamp_shift",
                                 "unadjusted_corporate_action"]
        for name, case in CASES.items():
            assert {"description", "injector", "inject_kwargs",
                    "guard", "guard_kwargs"} <= set(case), name

    @pytest.mark.parametrize("name", sorted(CASES))
    def test_run_case_passes(self, name):
        report = run_case(name)
        assert report["case"] == name
        assert report["flagged"] is True
        assert report["finding"]["flagged"] is True
        # The poisoned feed went through the real engine.
        assert report["engine"]["bars"] > 0
        assert report["engine"]["assumptions"]["adjustment_basis"] == "none"

    def test_run_case_unknown_name_raises_with_name(self):
        with pytest.raises(AdversarialFailure, match="bogus_class"):
            run_case("bogus_class")

    def test_leaky_feature_is_material_through_real_engine(self):
        # The planted leak is not academic: a strategy trading the leaky
        # feature through the real BacktestEngine prints an absurd Sharpe --
        # which is exactly why the guard must exist.
        report = run_case("future_return_leakage")
        assert report["engine"]["sharpe_ratio"] > 2.0
        assert report["engine"]["trades"] > 0

    def test_suite_passes_and_reports(self):
        report = run_adversarial_suite()
        assert report["suite"] == "adversarial_hidden_data_validation"
        assert report["passed"] is True
        assert sorted(report["cases"]) == sorted(CASES)
        assert report["clean_control"] == {"flagged": [],
                                           "n_findings": len(run_guards(_clean()))}

    def test_suite_subset(self):
        report = run_adversarial_suite(cases=["ffill_gap"])
        assert sorted(report["cases"]) == ["ffill_gap"]

    def test_suite_deterministic(self):
        r1 = run_adversarial_suite(seed=7)
        r2 = run_adversarial_suite(seed=7)
        for name in CASES:
            assert (r1["cases"][name]["finding"]["evidence"] ==
                    r2["cases"][name]["finding"]["evidence"])

    @pytest.mark.parametrize("seed", [1, 42])
    def test_suite_robust_across_seeds(self, seed):
        # Guards must not be overfit to the default seed.
        assert run_adversarial_suite(seed=seed)["passed"] is True


class TestCLI:
    def test_list(self, capsys):
        assert main(["--list"]) == 0
        out = capsys.readouterr().out
        for name in CASES:
            assert name in out

    def test_single_case(self, capsys):
        assert main(["--case", "ffill_gap"]) == 0
        assert "PASS [ffill_gap]" in capsys.readouterr().out

    def test_unknown_case_fails(self, capsys):
        assert main(["--case", "nope"]) == 1
        err = capsys.readouterr().err
        assert "FAIL" in err and "nope" in err

    def test_json_and_quiet(self, capsys):
        import json as _json
        assert main(["--json", "--case", "timestamp_shift"]) == 0
        report = _json.loads(capsys.readouterr().out)
        assert report["case_report"]["flagged"] is True
        assert main(["--quiet"]) == 0
        assert capsys.readouterr().out.strip() == "PASS"
