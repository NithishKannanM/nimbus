"""Statistics for comparing scheduling strategies.

The protocol here is not chosen per result. It is the one fixed in
docs/experiments.md section 2.3, written in Phase 1 before any strategy existed
to favour, and it is applied unchanged whichever way the numbers come out:

- **Mann-Whitney U, two-sided.** Non-parametric, because makespan under external
  pressure is bimodal -- a run either catches a straggler or it does not -- and
  a t-test on that distribution is testing an assumption the data violates.
- **Cliff's delta.** An effect size that is also non-parametric. A p-value at
  N≈9 says "probably not chance"; it does not say "large", and reporting
  significance without magnitude is how small effects get oversold.
- **Bonferroni correction.** Three baselines means three comparisons, so
  α = 0.05/3 = 0.0167. Uncorrected, the chance of at least one spurious
  "significant" result across three tests is about 14%.
- **Median and range, never mean and standard deviation.** For the same reason
  the test is non-parametric.

Everything is implemented here rather than pulled from scipy. Not for lack of
respect for scipy -- but the project's dependency list is part of its
reproducibility story, and these are a hundred lines of well-specified
arithmetic that anyone can check against the definitions. The exact-vs-normal
crossover and the continuity correction are stated explicitly below for the
same reason.

Note on what these functions do *not* do: none of them decides whether a result
may be reported. That is what the pre-registered criteria in
docs/adaptive-v1-preregistration.md are for, and the Phase 3 experience --
where the pre-registered criterion said "no difference" and the standing
statistical protocol said p=0.0039 with a large effect -- is exactly why the two
are kept separate and both are published.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

#: Two-sided α before correction.
ALPHA = 0.05


@dataclass(frozen=True)
class Comparison:
    """One strategy against one baseline, on one metric."""

    metric: str
    treatment: str
    baseline: str
    n_treatment: int
    n_baseline: int
    median_treatment: float
    median_baseline: float
    median_delta_pct: float
    u_statistic: float
    p_value: float
    cliffs_delta: float
    effect_label: str
    alpha: float
    significant: bool
    #: Whether the per-run ranges are disjoint. Reported because it is the
    #: pre-registered criterion, not because it is a good one -- see
    #: `ranges_overlap_note`.
    ranges_disjoint: bool
    treatment_range: tuple[float, float]
    baseline_range: tuple[float, float]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        mark = "significant" if self.significant else "not significant"
        return (
            f"{self.treatment} vs {self.baseline} on {self.metric}: "
            f"{self.median_delta_pct:+.1f}% median "
            f"(p={self.p_value:.4f}, {mark} at alpha={self.alpha:.4f}; "
            f"Cliff's delta={self.cliffs_delta:+.2f} {self.effect_label})"
        )


# --------------------------------------------------------------------------- #
# Mann-Whitney U
# --------------------------------------------------------------------------- #
def _rank(values: Sequence[float]) -> list[float]:
    """Ranks with ties averaged, which is what the tie-corrected U requires."""
    indexed = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(indexed):
        end = position
        while end + 1 < len(indexed) and values[indexed[end + 1]] == values[indexed[position]]:
            end += 1
        average = (position + end) / 2.0 + 1.0
        for i in range(position, end + 1):
            ranks[indexed[i]] = average
        position = end + 1
    return ranks


#: Above this many (n_a x n_b) rank orderings, fall back to the normal
#: approximation. The exact enumeration is O(n_a * n_b * u), which is trivial
#: at the N<=20 this project runs at and pointless beyond it, since the
#: approximation is excellent once both groups exceed ~20.
EXACT_LIMIT = 400


def _exact_u_pmf(n_a: int, n_b: int) -> list[int]:
    """Counts of rank orderings producing each U in ``0..n_a*n_b``.

    The standard recurrence, tabulated over both group sizes::

        N(m, n, u) = N(m-1, n, u-n) + N(m, n-1, u)
        N(m, 0, 0) = N(0, n, 0) = 1

    Exact because it counts orderings rather than approximating their
    distribution, which is what makes a p-value trustworthy at the N=8-9 this
    project reports at. Verified against brute-force enumeration of every
    interleaving up to 6x6 in tests/unit/test_analysis.py -- a recurrence like
    this is easy to write down subtly wrong, and an off-by-one here would shift
    every p-value in the final report.
    """
    # table[m][n] = the pmf for those group sizes, as counts indexed by u.
    table: list[list[list[int]]] = [
        [[] for _ in range(n_b + 1)] for _ in range(n_a + 1)
    ]
    for m in range(n_a + 1):
        for n in range(n_b + 1):
            size = m * n + 1
            if m == 0 or n == 0:
                table[m][n] = [1] + [0] * (size - 1)
                continue
            counts = [0] * size
            left = table[m - 1][n]      # N(m-1, n, u-n)
            below = table[m][n - 1]     # N(m, n-1, u)
            for u in range(size):
                total = 0
                if 0 <= u - n < len(left):
                    total += left[u - n]
                if u < len(below):
                    total += below[u]
                counts[u] = total
            table[m][n] = counts
    return table[n_a][n_b]


def _exact_p(u: float, n_a: int, n_b: int) -> float:
    """Two-sided exact p for the smaller-tail statistic ``u``."""
    counts = _exact_u_pmf(n_a, n_b)
    total = sum(counts)
    if total == 0:
        return 1.0
    tail = sum(counts[: int(u) + 1])
    return min(1.0, 2.0 * tail / total)


def mann_whitney_u(a: Sequence[float], b: Sequence[float]) -> tuple[float, float]:
    """Two-sided Mann-Whitney U. Returns ``(U, p)``.

    Two p-value paths, chosen by the data rather than by preference:

    * **Exact**, by enumerating the null distribution of U, when there are no
      ties and the groups are small. This is the path the project's own results
      take -- N=8-9 per strategy, continuous makespans, no ties -- and it is
      what makes a p-value at that N mean what it says.
    * **Normal approximation** with continuity and tie corrections otherwise.
      Adequate from about n=20 per group, and the only option once ties are
      present, since ties destroy the exact enumeration's assumption that every
      ordering is equally likely.

    Below ``MIN_N`` the caller is not permitted to claim significance either
    way; `compare` enforces that.
    """
    n_a, n_b = len(a), len(b)
    if n_a == 0 or n_b == 0:
        return 0.0, 1.0

    combined = list(a) + list(b)
    ranks = _rank(combined)
    rank_sum_a = sum(ranks[:n_a])

    u_a = rank_sum_a - n_a * (n_a + 1) / 2.0
    u_b = n_a * n_b - u_a
    u = min(u_a, u_b)

    has_ties = len(set(combined)) != len(combined)
    if not has_ties and n_a * n_b <= EXACT_LIMIT:
        return u, round(_exact_p(u, n_a, n_b), 10)

    mean_u = n_a * n_b / 2.0

    # Tie correction. Without it, heavily tied data (which placement-share
    # metrics produce readily) gets an inflated variance and an understated
    # p-value.
    counts: dict[float, int] = {}
    for value in combined:
        counts[value] = counts.get(value, 0) + 1
    tie_term = sum(c**3 - c for c in counts.values())
    n = n_a + n_b
    if n < 2:
        return u, 1.0
    variance = (n_a * n_b / 12.0) * ((n + 1) - tie_term / (n * (n - 1)))
    if variance <= 0:
        return u, 1.0

    z = (abs(u - mean_u) - 0.5) / math.sqrt(variance)  # continuity correction
    p = 2.0 * (1.0 - _normal_cdf(z))
    return u, max(0.0, min(1.0, p))


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# --------------------------------------------------------------------------- #
# Cliff's delta
# --------------------------------------------------------------------------- #
def cliffs_delta(a: Sequence[float], b: Sequence[float]) -> float:
    """P(a > b) - P(a < b), in [-1, 1].

    Negative means ``a`` tends to be smaller. For makespan, smaller is better,
    so a large negative delta is a win for ``a`` -- which reads backwards at
    first glance and is why every reported delta carries its metric's direction.
    """
    if not a or not b:
        return 0.0
    greater = sum(1 for x in a for y in b if x > y)
    lesser = sum(1 for x in a for y in b if x < y)
    return (greater - lesser) / (len(a) * len(b))


def effect_label(delta: float) -> str:
    """Romano et al.'s thresholds: |d| < 0.147 negligible, < 0.33 small,
    < 0.474 medium, else large."""
    magnitude = abs(delta)
    if magnitude < 0.147:
        return "negligible"
    if magnitude < 0.33:
        return "small"
    if magnitude < 0.474:
        return "medium"
    return "large"


# --------------------------------------------------------------------------- #
# Bootstrap
# --------------------------------------------------------------------------- #
def bootstrap_ci(
    values: Sequence[float],
    *,
    statistic=statistics.median,
    confidence: float = 0.95,
    resamples: int = 10_000,
    seed: int = 1337,
) -> tuple[float, float]:
    """Percentile bootstrap CI. Seeded, so a report regenerates identically.

    An unseeded bootstrap would make two runs of the analysis over the *same
    archived data* print different confidence intervals, which is exactly the
    kind of irreproducibility this project is trying not to have.
    """
    if len(values) < 2:
        value = float(values[0]) if values else 0.0
        return value, value
    rng = random.Random(seed)
    n = len(values)
    estimates = sorted(
        statistic([values[rng.randrange(n)] for _ in range(n)]) for _ in range(resamples)
    )
    lower = estimates[int((1 - confidence) / 2 * resamples)]
    upper = estimates[min(resamples - 1, int((1 + confidence) / 2 * resamples))]
    return round(lower, 6), round(upper, 6)


# --------------------------------------------------------------------------- #
# the comparison the report uses
# --------------------------------------------------------------------------- #
#: Below this, the normal approximation is optimistic and significance is not
#: claimed. docs/experiments.md section 2.3 requires >= 5 trials; 8 is where the
#: approximation becomes trustworthy, so between the two the result is reported
#: with its p-value but never labelled significant.
MIN_N = 8

#: Metrics where a smaller value is better. Used only to phrase the summary;
#: the arithmetic is direction-agnostic.
LOWER_IS_BETTER = frozenset(
    {"makespan_s", "completion_p50_ms", "completion_p95_ms", "queue_wait_p95_ms",
     "utilisation_cv", "job_share", "switch_rate"}
)


def compare(
    treatment: Sequence[float],
    baseline: Sequence[float],
    *,
    metric: str,
    treatment_name: str,
    baseline_name: str,
    comparisons: int = 1,
) -> Comparison:
    """One pre-registered comparison, with correction applied.

    ``comparisons`` is the family size for the Bonferroni correction -- pass the
    number of baselines being tested against, not 1, or the correction silently
    does nothing and the family-wise error rate is back to ~14% for three tests.
    """
    alpha = ALPHA / max(1, comparisons)
    u, p = mann_whitney_u(treatment, baseline)
    delta = cliffs_delta(treatment, baseline)

    median_t = statistics.median(treatment) if treatment else 0.0
    median_b = statistics.median(baseline) if baseline else 0.0
    delta_pct = ((median_t - median_b) / median_b * 100.0) if median_b else 0.0

    range_t = (min(treatment), max(treatment)) if treatment else (0.0, 0.0)
    range_b = (min(baseline), max(baseline)) if baseline else (0.0, 0.0)
    disjoint = range_t[1] < range_b[0] or range_b[1] < range_t[0]

    powered = len(treatment) >= MIN_N and len(baseline) >= MIN_N

    return Comparison(
        metric=metric,
        treatment=treatment_name,
        baseline=baseline_name,
        n_treatment=len(treatment),
        n_baseline=len(baseline),
        median_treatment=round(median_t, 4),
        median_baseline=round(median_b, 4),
        median_delta_pct=round(delta_pct, 2),
        u_statistic=u,
        p_value=round(p, 6),
        cliffs_delta=round(delta, 4),
        effect_label=effect_label(delta),
        alpha=round(alpha, 6),
        significant=bool(powered and p < alpha),
        ranges_disjoint=disjoint,
        treatment_range=(round(range_t[0], 4), round(range_t[1], 4)),
        baseline_range=(round(range_b[0], 4), round(range_b[1], 4)),
    )


@dataclass
class Series:
    """Per-run values for one strategy on one metric, plus its summary."""

    strategy: str
    metric: str
    values: list[float] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.values)

    @property
    def median(self) -> float:
        return round(statistics.median(self.values), 4) if self.values else 0.0

    @property
    def iqr(self) -> tuple[float, float]:
        if len(self.values) < 4:
            return (self.median, self.median)
        quartiles = statistics.quantiles(self.values, n=4, method="inclusive")
        return (round(quartiles[0], 4), round(quartiles[2], 4))

    def ci(self, seed: int = 1337) -> tuple[float, float]:
        return bootstrap_ci(self.values, seed=seed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "metric": self.metric,
            "n": self.n,
            "median": self.median,
            "iqr": list(self.iqr),
            "ci95": list(self.ci()),
            "range": [round(min(self.values), 4), round(max(self.values), 4)]
            if self.values
            else [0.0, 0.0],
            "values": [round(v, 4) for v in self.values],
        }


def compare_all(
    series_by_strategy: dict[str, Series],
    *,
    treatment: str,
    metric: str,
) -> list[Comparison]:
    """``treatment`` against every other strategy, Bonferroni-corrected for the
    number of baselines actually present."""
    baselines = sorted(k for k in series_by_strategy if k != treatment)
    if treatment not in series_by_strategy or not baselines:
        return []
    return [
        compare(
            series_by_strategy[treatment].values,
            series_by_strategy[name].values,
            metric=metric,
            treatment_name=treatment,
            baseline_name=name,
            comparisons=len(baselines),
        )
        for name in baselines
    ]
