"""Before/after evaluation gates for repair and harness candidates."""

from __future__ import annotations

from autoharness.models import (
    EvaluationPolicy,
    EvaluationResult,
    EvaluationSnapshot,
    MetricComparison,
    MetricDirection,
    MetricRule,
)


class EvaluationGate:
    """Accept only candidates that satisfy tests, thresholds, and regression budgets."""

    def evaluate(
        self,
        baseline: EvaluationSnapshot,
        candidate: EvaluationSnapshot,
        policy: EvaluationPolicy,
    ) -> EvaluationResult:
        rejection_reasons: list[str] = []

        if policy.require_tests and not candidate.tests_passed:
            failed = ", ".join(candidate.failed_tests) or "unspecified tests"
            rejection_reasons.append(f"Candidate tests failed: {failed}")

        comparisons = [
            self._compare(rule, baseline.metrics, candidate.metrics) for rule in policy.rules
        ]
        for comparison in comparisons:
            if not comparison.accepted:
                rejection_reasons.extend(
                    f"{comparison.metric}: {reason}" for reason in comparison.reasons
                )

        accepted = not rejection_reasons
        passed_count = sum(comparison.accepted for comparison in comparisons)
        summary = (
            f"Candidate accepted; {passed_count}/{len(comparisons)} metric gates passed."
            if accepted
            else f"Candidate rejected; {len(rejection_reasons)} gate violation(s)."
        )
        return EvaluationResult(
            accepted=accepted,
            summary=summary,
            comparisons=comparisons,
            rejection_reasons=rejection_reasons,
        )

    def _compare(
        self,
        rule: MetricRule,
        baseline_metrics: dict[str, float],
        candidate_metrics: dict[str, float],
    ) -> MetricComparison:
        baseline = baseline_metrics.get(rule.metric)
        candidate = candidate_metrics.get(rule.metric)
        reasons: list[str] = []

        if baseline is None or candidate is None:
            missing = []
            if baseline is None:
                missing.append("baseline")
            if candidate is None:
                missing.append("candidate")
            if rule.required:
                reasons.append(f"required metric missing from {' and '.join(missing)}")
            return MetricComparison(
                metric=rule.metric,
                direction=rule.direction,
                baseline=baseline,
                candidate=candidate,
                accepted=not rule.required,
                reasons=reasons,
            )

        delta = candidate - baseline
        signed_improvement = delta if rule.direction == MetricDirection.HIGHER_IS_BETTER else -delta
        improvement_ratio = signed_improvement / abs(baseline) if baseline != 0 else None

        if rule.threshold is not None:
            if rule.direction == MetricDirection.HIGHER_IS_BETTER and candidate < rule.threshold:
                reasons.append(f"{candidate:g} is below minimum {rule.threshold:g}")
            if rule.direction == MetricDirection.LOWER_IS_BETTER and candidate > rule.threshold:
                reasons.append(f"{candidate:g} exceeds maximum {rule.threshold:g}")

        if (
            signed_improvement < 0
            and self._regression_ratio(signed_improvement, baseline) > rule.max_regression_ratio
        ):
            actual = self._regression_ratio(signed_improvement, baseline)
            reasons.append(f"regressed by {actual:.2%}; allowed {rule.max_regression_ratio:.2%}")

        return MetricComparison(
            metric=rule.metric,
            direction=rule.direction,
            baseline=baseline,
            candidate=candidate,
            delta=delta,
            improvement_ratio=improvement_ratio,
            accepted=not reasons,
            reasons=reasons,
        )

    @staticmethod
    def _regression_ratio(signed_improvement: float, baseline: float) -> float:
        if signed_improvement >= 0:
            return 0.0
        if baseline == 0:
            return 1.0
        return abs(signed_improvement) / abs(baseline)
