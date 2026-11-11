"""Observation-process helpers shared by the Digital Twin notebook and tests."""
from __future__ import annotations

import math

def trace_level_counts(rng, trace_populations, frontend_index, sampling, no_trace_indices=()):
    """Sample correlated per-service trace counts from one root sampling decision.

    Conditional on the sampled root count, each downstream service count is a
    Hypergeometric thinning of the root traces that visit that service.  Its
    unconditional marginal is Binomial(N_i, p), while counts share the random
    root decision instead of being sampled independently per service.
    """
    populations = [max(0, int(round(float(value)))) for value in trace_populations]
    if not 0.0 <= float(sampling) <= 1.0:
        raise ValueError("sampling must be in [0,1]")
    root_population = int(populations[frontend_index])
    sampled_root = int(rng.binomial(root_population, sampling))
    counts = [0 for _ in populations]
    counts[frontend_index] = sampled_root
    for index, population in enumerate(populations):
        if index == frontend_index or index in no_trace_indices:
            continue
        # A trace either visits a service or does not. Multiple spans/calls in
        # one root trace are deliberately not reinterpreted as extra traces.
        visiting = min(int(population), root_population)
        if visiting and sampled_root:
            counts[index] = rng.hypergeometric(
                visiting, root_population - visiting, sampled_root
            )
    for index in no_trace_indices:
        counts[index] = 0
    return counts


def update_latency_ewma(previous, p95_raw, trace_count, threshold):
    """Update reward-only P95 EWMA without ever evaluating 0 * NaN.

    Missing raw P95 remains statistically missing. The returned value is finite
    because the reward filter starts from the SLO supplied by its caller as
    ``previous`` when no earlier observation exists.
    """
    count = int(trace_count)
    threshold = int(threshold)
    if count < 0 or threshold <= 0:
        raise ValueError("trace_count must be >=0 and threshold must be >0")
    if count == 0:
        if previous is None:
            raise ValueError("previous must be initialized for an empty trace window")
        value = float(previous)
        if not math.isfinite(value):
            raise FloatingPointError("previous EWMA must be finite")
        return value, value, 0.0
    raw = float(p95_raw)
    if not math.isfinite(raw):
        raise FloatingPointError("non-empty trace window requires finite raw P95")
    old = raw if previous is None else float(previous)
    if not math.isfinite(old):
        raise FloatingPointError("previous EWMA must be finite")
    alpha = min(1.0, float(count) / float(threshold))
    updated = alpha * raw + (1.0 - alpha) * old
    if not math.isfinite(updated):
        raise FloatingPointError("latency EWMA became non-finite")
    return updated, updated, alpha
