"""Explainable baseline diagnosis for agent traces."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass

from autoharness.models import (
    AgentTrace,
    EventKind,
    EventStatus,
    Evidence,
    FailureDiagnosis,
    FailureType,
)


@dataclass(frozen=True)
class FailureRule:
    failure_type: FailureType
    event_kinds: frozenset[EventKind]
    terms: tuple[str, ...]
    causes: tuple[str, ...]
    checks: tuple[str, ...]


RULES = (
    FailureRule(
        FailureType.RETRIEVAL,
        frozenset({EventKind.RETRIEVAL}),
        ("retriev", "embedding", "vector", "rerank", "recall", "top_k", "document", "chunk"),
        ("Relevant context was not retrieved", "Retrieved context was ranked incorrectly"),
        (
            "Inspect retrieved documents and scores",
            "Verify embedding, filters, top_k, and reranking",
        ),
    ),
    FailureRule(
        FailureType.TOOL,
        frozenset({EventKind.TOOL_CALL, EventKind.ERROR}),
        ("tool", "api", "timeout", "429", "500", "exception", "connection", "rate limit"),
        ("A tool returned an error or timed out", "Tool arguments or retry policy are invalid"),
        ("Replay the failing tool call", "Check arguments, credentials, timeout, and retry policy"),
    ),
    FailureRule(
        FailureType.PLANNING,
        frozenset({EventKind.PLAN}),
        ("plan", "step", "decompos", "route", "loop", "missing step"),
        ("The task was decomposed or routed incorrectly", "A required execution step was omitted"),
        (
            "Compare the plan with task requirements",
            "Check routing rules and termination conditions",
        ),
    ),
    FailureRule(
        FailureType.MEMORY,
        frozenset({EventKind.MEMORY}),
        ("memory", "history", "stale", "context pollution", "wrong context"),
        (
            "Stale or irrelevant memory influenced the run",
            "Memory retrieval or eviction is incorrect",
        ),
        ("Inspect memories injected into context", "Verify memory selection and expiration rules"),
    ),
    FailureRule(
        FailureType.VALIDATION,
        frozenset({EventKind.VALIDATION}),
        ("validat", "assert", "test", "metric", "threshold", "schema"),
        (
            "The output failed a validation gate",
            "The validator or acceptance threshold is incorrect",
        ),
        ("Inspect the failed validator", "Compare actual and expected outputs"),
    ),
    FailureRule(
        FailureType.REASONING,
        frozenset({EventKind.RESPONSE, EventKind.OBSERVATION, EventKind.FEEDBACK}),
        ("incorrect", "wrong", "hallucin", "contradict", "incomplete", "reason"),
        ("The response is unsupported or logically inconsistent", "Available evidence was misused"),
        (
            "Compare the response with observations",
            "Add an evidence or consistency verification step",
        ),
    ),
)


class FailureDiagnoser:
    """Classify failures with deterministic rules that expose their evidence."""

    def diagnose(self, trace: AgentTrace) -> FailureDiagnosis:
        scores: dict[FailureType, float] = defaultdict(float)
        evidence: dict[FailureType, list[Evidence]] = defaultdict(list)

        for index, event in enumerate(trace.events):
            event_text = self._event_text(event.model_dump(mode="json"))
            source = f"event[{index}].{event.kind.value}"
            for rule in RULES:
                if event.kind in rule.event_kinds:
                    weight = 2.0 if event.status == EventStatus.FAILURE or event.error else 0.7
                    scores[rule.failure_type] += weight
                    evidence[rule.failure_type].append(
                        Evidence(
                            source=source,
                            excerpt=self._excerpt(event_text),
                            signal=event.kind.value,
                        )
                    )
                matches = [term for term in rule.terms if term in event_text]
                if matches:
                    scores[rule.failure_type] += min(2.0, len(matches) * 0.5)
                    evidence[rule.failure_type].append(
                        Evidence(
                            source=source,
                            excerpt=self._excerpt(event_text),
                            signal=", ".join(matches[:3]),
                        )
                    )

        unstructured = " ".join([*trace.logs, trace.feedback or "", trace.expected or ""]).lower()
        for rule in RULES:
            matches = [term for term in rule.terms if term in unstructured]
            if matches:
                scores[rule.failure_type] += min(3.0, len(matches) * 0.75)
                evidence[rule.failure_type].append(
                    Evidence(
                        source="logs/feedback",
                        excerpt=self._excerpt(unstructured),
                        signal=", ".join(matches[:3]),
                    )
                )

        if not scores:
            return FailureDiagnosis(
                failure_type=FailureType.UNKNOWN,
                confidence=0.2,
                summary="The trace does not contain enough failure evidence to classify.",
                likely_causes=[
                    "The trace is incomplete or the failure is outside the current taxonomy"
                ],
                evidence=[],
                recommended_checks=[
                    "Capture failed events, errors, outputs, and explicit user feedback"
                ],
                search_terms=self._identifiers(trace.task),
            )

        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0].value))
        failure_type, top_score = ranked[0]
        total = sum(scores.values())
        confidence = min(0.98, 0.45 + 0.55 * (top_score / total))
        rule = next(rule for rule in RULES if rule.failure_type == failure_type)
        selected_evidence = self._deduplicate_evidence(evidence[failure_type])[:6]
        terms = self._identifiers(" ".join([trace.task, unstructured]))
        matched_terms = [
            term for term in rule.terms if term in self._event_text(trace.model_dump())
        ]

        return FailureDiagnosis(
            failure_type=failure_type,
            confidence=round(confidence, 3),
            summary=f"Trace signals indicate a {failure_type.value.replace('_', ' ')}.",
            likely_causes=list(rule.causes),
            evidence=selected_evidence,
            recommended_checks=list(rule.checks),
            search_terms=list(dict.fromkeys([*matched_terms, *terms]))[:16],
        )

    @staticmethod
    def _event_text(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, default=str).lower()

    @staticmethod
    def _excerpt(text: str, limit: int = 220) -> str:
        clean = " ".join(text.split())
        return clean if len(clean) <= limit else f"{clean[: limit - 1]}…"

    @staticmethod
    def _identifiers(text: str) -> list[str]:
        stopwords = {
            "about",
            "after",
            "agent",
            "answer",
            "because",
            "before",
            "could",
            "did",
            "error",
            "failure",
            "from",
            "have",
            "into",
            "not",
            "question",
            "should",
            "system",
            "that",
            "the",
            "their",
            "there",
            "these",
            "this",
            "trace",
            "user",
            "with",
            "wrong",
        }
        words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", text.lower())
        return list(dict.fromkeys(word for word in words if word not in stopwords))[:20]

    @staticmethod
    def _deduplicate_evidence(items: list[Evidence]) -> list[Evidence]:
        seen: set[tuple[str, str]] = set()
        result: list[Evidence] = []
        for item in items:
            key = (item.source, item.signal)
            if key not in seen:
                seen.add(key)
                result.append(item)
        return result
