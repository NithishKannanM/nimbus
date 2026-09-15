"""The statistics.

These are the numbers the project's conclusions rest on, so the tests check
them against definitions and brute force rather than against previously
observed output. A statistics module that only agrees with itself is not
verified.
"""

from __future__ import annotations

import itertools
from collections import Counter

import pytest

from benchmark.analysis import (
    ALPHA,
    MIN_N,
    Series,
    _exact_u_pmf,
    _rank,
    bootstrap_ci,
    cliffs_delta,
    compare,
    compare_all,
    effect_label,
    mann_whitney_u,
)


# --------------------------------------------------------------------------- #
# the exact null distribution
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("n_a,n_b", [(1, 1), (2, 3), (3, 3), (4, 4), (5, 4), (6, 5)])
def test_exact_distribution_matches_brute_force(n_a: int, n_b: int):
    """Enumerate every interleaving and count U directly.

    The recurrence is easy to write down subtly wrong, and an off-by-one here
    would shift every p-value in the final report by a little -- which is worse
    than shifting them by a lot, because nobody would notice.
    """
    brute: Counter[int] = Counter()
    for positions in itertools.combinations(range(n_a + n_b), n_a):
        chosen = set(positions)
        u = sum(
            1
            for a in positions
            for b in range(n_a + n_b)
            if b not in chosen and b < a
        )
        brute[u] += 1
    expected = [brute.get(u, 0) for u in range(n_a * n_b + 1)]
    assert _exact_u_pmf(n_a, n_b) == expected


def test_exact_distribution_is_symmetric_and_complete():
    counts = _exact_u_pmf(5, 5)
    assert counts == counts[::-1], "the null distribution of U must be symmetric"
    from math import comb

    assert sum(counts) == comb(10, 5)


@pytest.mark.parametrize(
    "a,b,expected_p",
    [
        # Fully separated 3v3: 2 * (1/20) = 0.1
        ([1, 2, 3], [4, 5, 6], 0.1),
        # Fully separated 4v4: 2 * (1/70) = 0.028571...
        ([1, 2, 3, 4], [5, 6, 7, 8], 2 / 70),
    ],
)
def test_exact_p_values_are_the_textbook_ones(a, b, expected_p):
    _, p = mann_whitney_u(a, b)
    assert p == pytest.approx(expected_p, abs=1e-9)


def test_identical_groups_are_not_significant():
    _, p = mann_whitney_u([1, 2, 3, 4, 5], [1, 2, 3, 4, 5])
    assert p == pytest.approx(1.0)


def test_ties_force_the_normal_approximation():
    """The exact enumeration assumes every ordering is equally likely. Ties
    break that assumption, so tied data must take the corrected normal path."""
    tied_a = [1.0, 2.0, 2.0, 3.0, 4.0]
    tied_b = [2.0, 3.0, 3.0, 5.0, 6.0]
    _, p = mann_whitney_u(tied_a, tied_b)
    assert 0.0 < p <= 1.0


def test_ranks_average_over_ties():
    assert _rank([10.0, 20.0, 20.0, 30.0]) == [1.0, 2.5, 2.5, 4.0]


def test_the_statistic_is_symmetric_in_its_arguments():
    a, b = [1.0, 4.0, 7.0, 9.0], [2.0, 3.0, 5.0, 8.0]
    assert mann_whitney_u(a, b) == mann_whitney_u(b, a)


def test_empty_input_is_not_significant():
    assert mann_whitney_u([], [1, 2, 3]) == (0.0, 1.0)


# --------------------------------------------------------------------------- #
# Cliff's delta
# --------------------------------------------------------------------------- #
def test_complete_dominance_is_plus_or_minus_one():
    assert cliffs_delta([4, 5, 6], [1, 2, 3]) == 1.0
    assert cliffs_delta([1, 2, 3], [4, 5, 6]) == -1.0


def test_identical_distributions_have_zero_effect():
    assert cliffs_delta([1, 2, 3], [1, 2, 3]) == 0.0


@pytest.mark.parametrize(
    "delta,label",
    [(0.10, "negligible"), (0.20, "small"), (0.40, "medium"), (0.90, "large")],
)
def test_effect_labels_follow_romano_thresholds(delta: float, label: str):
    assert effect_label(delta) == label
    assert effect_label(-delta) == label, "the label must not depend on direction"


# --------------------------------------------------------------------------- #
# bootstrap
# --------------------------------------------------------------------------- #
def test_the_bootstrap_is_seeded_and_reproducible():
    """An unseeded bootstrap would print different confidence intervals on two
    runs of the analysis over the *same archived data*, which is exactly the
    irreproducibility this project is trying not to have."""
    values = [11.3, 11.4, 11.5, 11.6, 11.7, 12.2, 31.9]
    assert bootstrap_ci(values, seed=1337) == bootstrap_ci(values, seed=1337)


def test_the_seed_actually_reaches_the_resampler():
    """Checked at a low resample count, where sampling noise is still visible.

    At the default 10,000 resamples the percentile interval has converged onto
    the sample's order statistics and is identical for every seed -- which is
    the property you want from the default and precisely why it cannot be used
    to prove the seed is wired up. A seed silently ignored would look exactly
    like a well-converged estimate.
    """
    values = [1.0, 2.0, 3.0, 10.0, 20.0, 30.0, 40.0, 50.0, 100.0, 250.0, 900.0]
    intervals = {bootstrap_ci(values, seed=s, resamples=50) for s in range(1, 8)}
    assert len(intervals) > 1, "the seed is not reaching the resampler"


def test_the_default_resample_count_has_converged():
    """The flip side: at the default, the interval is stable across seeds, so a
    report regenerates identically no matter who runs it."""
    values = [11.3, 11.4, 11.5, 11.6, 11.7, 12.2, 31.9, 40.1]
    assert len({bootstrap_ci(values, seed=s) for s in range(1, 6)}) == 1


def test_the_interval_brackets_the_point_estimate():
    import statistics

    values = [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0]
    low, high = bootstrap_ci(values)
    assert low <= statistics.median(values) <= high


def test_a_single_value_has_a_degenerate_interval():
    assert bootstrap_ci([7.0]) == (7.0, 7.0)


# --------------------------------------------------------------------------- #
# the reported comparison
# --------------------------------------------------------------------------- #
def test_bonferroni_divides_alpha_by_the_family_size():
    c = compare([1.0], [2.0], metric="m", treatment_name="a", baseline_name="b", comparisons=3)
    assert c.alpha == pytest.approx(ALPHA / 3, abs=1e-6)


def test_significance_is_refused_below_the_power_floor():
    """A p-value at n=3 per group is arithmetic, not evidence."""
    c = compare(
        [1.0, 2.0, 3.0], [10.0, 11.0, 12.0],
        metric="makespan_s", treatment_name="adaptive", baseline_name="fifo",
    )
    assert c.n_treatment < MIN_N
    assert not c.significant, "significance claimed below the power floor"


def test_a_real_effect_at_adequate_n_is_significant():
    treatment = [10.0, 10.5, 11.0, 11.2, 11.4, 11.6, 11.8, 12.0]
    baseline = [30.0, 32.0, 34.0, 36.0, 38.0, 40.0, 42.0, 44.0]
    c = compare(
        treatment, baseline,
        metric="makespan_s", treatment_name="adaptive", baseline_name="fifo",
        comparisons=3,
    )
    assert c.significant
    assert c.effect_label == "large"
    assert c.median_delta_pct < -50
    assert c.ranges_disjoint


def test_disjoint_ranges_and_significance_are_reported_separately():
    """The Phase 3 lesson. On a bimodal distribution one unlucky treatment run
    and one lucky baseline run overlap the ranges while leaving the medians far
    apart, so the pre-registered criterion fails while the statistics do not.
    Both verdicts have to survive to the report."""
    treatment = [11.3, 11.4, 11.5, 11.6, 11.7, 11.75, 12.2, 31.9]
    baseline = [11.9, 12.7, 13.3, 15.9, 31.0, 40.4, 53.5, 53.8, 54.4]
    c = compare(
        treatment, baseline,
        metric="makespan_s", treatment_name="adaptive",
        baseline_name="static_resource_aware", comparisons=3,
    )
    assert not c.ranges_disjoint, "the pre-registered criterion should fail here"
    assert c.significant, "the standing protocol should still find an effect"
    assert c.effect_label == "large"


def test_the_published_phase_3_effect_sizes_reproduce():
    """Cliff's delta and the medians from the README, recomputed here.

    Pinned because these are the numbers the project's headline claim rests on:
    if a change to this module moved them, the claim would silently become a
    different claim.
    """
    adaptive = [11.3, 11.4, 11.5, 11.6, 11.7, 11.7, 12.2, 31.9]
    cases = {
        "static_resource_aware": ([11.9, 12.7, 13.3, 15.9, 31.0, 40.4, 53.5, 53.8, 54.4],
                                  -0.83, -62.5),
        "fifo": ([11.4, 12.4, 32.2, 36.4, 38.2, 44.9, 45.9, 46.1, 46.2], -0.79, -69.5),
        "static_resource_aware_binpack": ([11.9, 12.4, 45.5, 45.9, 46.0, 46.4, 46.4, 46.7],
                                          -0.91, -74.7),
    }
    for baseline_name, (baseline, delta, median_pct) in cases.items():
        c = compare(
            adaptive, baseline, metric="makespan_s",
            treatment_name="adaptive", baseline_name=baseline_name, comparisons=3,
        )
        assert c.cliffs_delta == pytest.approx(delta, abs=0.02), baseline_name
        assert c.median_delta_pct == pytest.approx(median_pct, abs=0.2), baseline_name
        assert c.significant, baseline_name


def test_comparing_against_every_baseline_corrects_for_all_of_them():
    series = {
        name: Series(strategy=name, metric="makespan_s", values=values)
        for name, values in {
            "adaptive": [11.0] * 8,
            "fifo": [40.0] * 8,
            "static_resource_aware": [30.0] * 8,
            "static_resource_aware_binpack": [45.0] * 8,
        }.items()
    }
    comparisons = compare_all(series, treatment="adaptive", metric="makespan_s")
    assert len(comparisons) == 3
    assert all(c.alpha == pytest.approx(ALPHA / 3, abs=1e-6) for c in comparisons)
    assert {c.baseline for c in comparisons} == {
        "fifo", "static_resource_aware", "static_resource_aware_binpack"
    }


def test_a_treatment_with_no_baselines_yields_no_comparisons():
    series = {"adaptive": Series(strategy="adaptive", metric="m", values=[1.0])}
    assert compare_all(series, treatment="adaptive", metric="m") == []
    assert compare_all(series, treatment="missing", metric="m") == []


# --------------------------------------------------------------------------- #
# Series
# --------------------------------------------------------------------------- #
def test_series_reports_median_iqr_range_and_ci():
    series = Series(strategy="adaptive", metric="makespan_s",
                    values=[11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 40.0])
    payload = series.as_dict()
    assert payload["n"] == 8
    assert payload["median"] == pytest.approx(14.5)
    assert payload["range"] == [11.0, 40.0]
    assert payload["iqr"][0] <= payload["median"] <= payload["iqr"][1]
    assert len(payload["ci95"]) == 2
