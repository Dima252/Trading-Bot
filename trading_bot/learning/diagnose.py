"""Is the ranking function doing anything?

The shadow book answers a question the trade log cannot: across the FULL range
of candidates the scanner produced -- taken and untaken -- does a higher score
actually predict a better outcome?

Three possible answers, and they lead to very different work:

    monotonic    scoring works; the problem is elsewhere
    flat         scoring adds nothing; replace it with random selection and
                 lose nothing, then go find a real edge
    inverted     the scorer is systematically picking the WORST candidates out
                 of its own pool, and fixing it is worth more than any amount
                 of setup tuning

Every candidate is measured the same way -- a forward simulation of its own
entry, stop and target -- so taken and untaken are directly comparable. That
uniformity is the whole point; realized P&L is not usable here because taken
trades also exit via time stops and rotation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean

from ..backtest.records import ShadowRecord

MIN_BUCKET = 20

# A rank correlation's noise floor shrinks as 1/sqrt(n), so a FIXED cutoff is
# wrong in both directions: it calls noise a finding on small samples and misses
# real effects on large ones. Everything below is judged by how many standard
# errors the correlation sits from zero. |z| >= 2 is roughly 95%.
MIN_Z = 2.0


@dataclass(frozen=True)
class Bucket:
    label: str
    n: int
    mean_r: float
    win_rate: float
    lo: float
    hi: float


@dataclass(frozen=True)
class FeatureDiagnosis:
    name: str
    n: int
    correlation: float
    top_minus_bottom: float
    buckets: list[Bucket] = field(default_factory=list)

    @property
    def z_score(self) -> float:
        """Standard errors from zero, under the null of no association."""
        if self.n < 4:
            return 0.0
        return round(self.correlation * ((self.n - 1) ** 0.5), 2)

    @property
    def verdict(self) -> str:
        if self.n < MIN_BUCKET * 4:
            return "too few samples"
        z = self.z_score
        if z <= -MIN_Z:
            return "ANTI-PREDICTIVE -- higher is worse"
        if z >= MIN_Z:
            return "predictive"
        return "no signal -- indistinguishable from random"


def spearman(pairs: list[tuple[float, float]]) -> float:
    """Rank correlation. Pearson on ranks, ties averaged.

    Rank rather than raw correlation because R multiples have fat tails -- one
    +8R outlier would otherwise dominate the answer.
    """
    if len(pairs) < 3:
        return 0.0

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            shared = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = shared
            i = j + 1
        return out

    xs = ranks([p[0] for p in pairs])
    ys = ranks([p[1] for p in pairs])
    mx, my = mean(xs), mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    return round(num / (dx * dy), 4) if dx and dy else 0.0


def bucketize(pairs: list[tuple[float, float]], n_buckets: int = 10) -> list[Bucket]:
    """Split by value into equal-count buckets and measure each one."""
    if len(pairs) < n_buckets * 2:
        return []

    ordered = sorted(pairs, key=lambda p: p[0])
    size = len(ordered) // n_buckets
    out: list[Bucket] = []

    for i in range(n_buckets):
        start = i * size
        end = len(ordered) if i == n_buckets - 1 else (i + 1) * size
        chunk = ordered[start:end]
        if len(chunk) < MIN_BUCKET:
            continue
        outcomes = [r for _, r in chunk]
        out.append(
            Bucket(
                label=f"D{i + 1}",
                n=len(chunk),
                mean_r=round(mean(outcomes), 4),
                win_rate=round(len([r for r in outcomes if r > 0]) / len(chunk), 3),
                lo=round(chunk[0][0], 2),
                hi=round(chunk[-1][0], 2),
            )
        )
    return out


def diagnose_feature(
    records: list[ShadowRecord], name: str, extract
) -> FeatureDiagnosis:
    pairs: list[tuple[float, float]] = []
    for record in records:
        if record.hypothetical_r is None:
            continue
        value = extract(record)
        if value is None:
            continue
        pairs.append((float(value), float(record.hypothetical_r)))

    buckets = bucketize(pairs)
    spread = 0.0
    if len(buckets) >= 2:
        spread = round(buckets[-1].mean_r - buckets[0].mean_r, 4)

    return FeatureDiagnosis(
        name=name,
        n=len(pairs),
        correlation=spearman(pairs),
        top_minus_bottom=spread,
        buckets=buckets,
    )


def _feature(key: str):
    def extract(record: ShadowRecord):
        return record.features.get(key)

    return extract


def diagnose(records: list[ShadowRecord]) -> list[FeatureDiagnosis]:
    """The composite score first, then each input that feeds it."""
    resolved = [r for r in records if r.hypothetical_r is not None]

    out = [
        diagnose_feature(resolved, "score (composite)", lambda r: r.score),
        diagnose_feature(resolved, "setup_quality", lambda r: r.setup_quality),
        diagnose_feature(resolved, "reward_risk", lambda r: r.reward_risk),
    ]

    # Per-setup feature detail: the heuristics were invented without evidence,
    # so each one is a suspect until measured.
    seen: set[str] = set()
    for record in resolved:
        seen.update(record.features)
    for key in sorted(seen):
        if any(
            isinstance(r.features.get(key), (int, float)) for r in resolved
        ):
            out.append(diagnose_feature(resolved, f"  feature: {key}", _feature(key)))

    return [d for d in out if d.n >= MIN_BUCKET]


def format_diagnosis(
    diagnoses: list[FeatureDiagnosis], records: list[ShadowRecord]
) -> str:
    lines: list[str] = []
    resolved = [r for r in records if r.hypothetical_r is not None]

    lines.append("=" * 74)
    lines.append("RANKING DIAGNOSTIC")
    lines.append("=" * 74)
    lines.append(
        f"  {len(resolved)} candidates with a simulated outcome, "
        f"{len([r for r in resolved if r.not_taken_reason == 'taken'])} of them taken"
    )
    lines.append("")
    lines.append(
        f"  {'input':<26}{'n':>7}{'rho':>8}{'z':>7}{'top-bottom':>12}   verdict"
    )
    for d in diagnoses:
        lines.append(
            f"  {d.name:<26}{d.n:>7}{d.correlation:>8.3f}{d.z_score:>7.1f}"
            f"{d.top_minus_bottom:>+12.3f}   {d.verdict}"
        )

    headline = next((d for d in diagnoses if d.name.startswith("score")), None)
    if headline and headline.buckets:
        lines.append("")
        lines.append("  composite score by decile (D1 = lowest scores)")
        lines.append(
            f"    {'':<5}{'range':>16}{'n':>7}{'mean R':>10}{'win%':>8}"
        )
        for b in headline.buckets:
            bar = "#" * min(30, int(abs(b.mean_r) * 40))
            sign = "" if b.mean_r >= 0 else "-"
            lines.append(
                f"    {b.label:<5}{f'{b.lo:.1f}-{b.hi:.1f}':>16}{b.n:>7}"
                f"{b.mean_r:>10.3f}{b.win_rate:>8.1%}  {sign}{bar}"
            )

    lines.append("")
    lines.append("  what to do")
    if headline is None:
        lines.append("    not enough resolved candidates to judge")
    elif headline.z_score <= -MIN_Z:
        lines.append("    The scorer ranks GOOD candidates LOW. Whatever is driving")
        lines.append("    that is worth more than any setup tuning -- check the")
        lines.append("    anti-predictive inputs above and invert or drop them.")
    elif abs(headline.z_score) < MIN_Z:
        lines.append("    Scoring carries no information. Ranking is currently a")
        lines.append("    coin flip dressed as a decision -- either find inputs that")
        lines.append("    predict, or accept that selection is not where the edge is.")
    else:
        lines.append("    Scoring predicts. The weakness is elsewhere -- look at")
        lines.append("    regime gating, exits, or the setups themselves.")
    lines.append("=" * 74)
    return "\n".join(lines)
