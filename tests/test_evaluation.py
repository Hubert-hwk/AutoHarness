from autoharness.evaluation import EvaluationGate
from autoharness.models import (
    EvaluationPolicy,
    EvaluationSnapshot,
    MetricDirection,
    MetricRule,
)


def test_accepts_improvement_within_regression_budgets() -> None:
    baseline = EvaluationSnapshot(
        metrics={"success_rate": 0.72, "latency_ms": 1000, "cost_usd": 0.10}
    )
    candidate = EvaluationSnapshot(
        metrics={"success_rate": 0.86, "latency_ms": 1050, "cost_usd": 0.105}
    )
    policy = EvaluationPolicy(
        rules=[
            MetricRule(
                metric="success_rate",
                direction=MetricDirection.HIGHER_IS_BETTER,
                threshold=0.8,
            ),
            MetricRule(
                metric="latency_ms",
                direction=MetricDirection.LOWER_IS_BETTER,
                max_regression_ratio=0.1,
            ),
            MetricRule(
                metric="cost_usd",
                direction=MetricDirection.LOWER_IS_BETTER,
                max_regression_ratio=0.1,
            ),
        ]
    )

    result = EvaluationGate().evaluate(baseline, candidate, policy)

    assert result.accepted
    assert result.rejection_reasons == []
    assert all(comparison.accepted for comparison in result.comparisons)


def test_rejects_failed_tests_and_metric_regression() -> None:
    baseline = EvaluationSnapshot(metrics={"success_rate": 0.8})
    candidate = EvaluationSnapshot(
        metrics={"success_rate": 0.6},
        tests_passed=False,
        failed_tests=["test_retrieval"],
    )
    policy = EvaluationPolicy(
        rules=[
            MetricRule(
                metric="success_rate",
                direction=MetricDirection.HIGHER_IS_BETTER,
                max_regression_ratio=0.05,
            )
        ]
    )

    result = EvaluationGate().evaluate(baseline, candidate, policy)

    assert not result.accepted
    assert any("test_retrieval" in reason for reason in result.rejection_reasons)
    assert any("regressed" in reason for reason in result.rejection_reasons)


def test_required_and_optional_missing_metrics() -> None:
    baseline = EvaluationSnapshot(metrics={})
    candidate = EvaluationSnapshot(metrics={})
    policy = EvaluationPolicy(
        rules=[
            MetricRule(
                metric="required_metric",
                direction=MetricDirection.HIGHER_IS_BETTER,
            ),
            MetricRule(
                metric="optional_metric",
                direction=MetricDirection.HIGHER_IS_BETTER,
                required=False,
            ),
        ]
    )

    result = EvaluationGate().evaluate(baseline, candidate, policy)

    assert not result.accepted
    assert not result.comparisons[0].accepted
    assert result.comparisons[1].accepted


def test_zero_baseline_regression_is_rejected_without_division_error() -> None:
    policy = EvaluationPolicy(
        rules=[
            MetricRule(
                metric="cost_usd",
                direction=MetricDirection.LOWER_IS_BETTER,
                max_regression_ratio=0.1,
            )
        ]
    )

    result = EvaluationGate().evaluate(
        EvaluationSnapshot(metrics={"cost_usd": 0}),
        EvaluationSnapshot(metrics={"cost_usd": 0.01}),
        policy,
    )

    assert not result.accepted
    assert result.comparisons[0].improvement_ratio is None
