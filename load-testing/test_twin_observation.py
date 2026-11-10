import importlib.util
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "twin_observation_test", ROOT / "modeling-common" / "twin_observation.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_empty_nan_window_keeps_finite_previous_ewma():
    ema, reward_p95, alpha = MODULE.update_latency_ewma(275.0, math.nan, 0, 30)
    assert ema == 275.0
    assert reward_p95 == 275.0
    assert alpha == 0.0
    assert math.isfinite(ema)


def test_low_count_uses_continuous_linear_alpha():
    ema, reward_p95, alpha = MODULE.update_latency_ewma(200.0, 320.0, 9, 30)
    assert math.isclose(alpha, 0.3)
    assert math.isclose(ema, 236.0)
    assert math.isclose(reward_p95, 236.0)


def test_count_at_or_above_threshold_uses_raw_p95():
    for count in (30, 45):
        ema, reward_p95, alpha = MODULE.update_latency_ewma(200.0, 320.0, count, 30)
        assert alpha == 1.0
        assert ema == 320.0
        assert reward_p95 == 320.0


def test_trace_counts_share_root_sample_and_respect_marginals():
    class DeterministicRng:
        def binomial(self, population, sampling):
            return min(10, population)

        def hypergeometric(self, visiting, not_visiting, sampled_root):
            return min(visiting, sampled_root)

    rng = DeterministicRng()
    counts = MODULE.trace_level_counts(rng, [100, 70, 40, 120], 0, 0.10, (3,))
    assert 0 <= counts[1] <= counts[0] <= 100
    assert 0 <= counts[2] <= counts[0]
    assert counts[3] == 0


if __name__ == "__main__":
    test_empty_nan_window_keeps_finite_previous_ewma()
    test_low_count_uses_continuous_linear_alpha()
    test_count_at_or_above_threshold_uses_raw_p95()
    test_trace_counts_share_root_sample_and_respect_marginals()
    print("4 twin observation tests: PASS")
