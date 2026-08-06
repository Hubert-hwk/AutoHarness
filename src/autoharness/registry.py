"""Safe, explainable retrieval over locally learned repair Skills."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from autoharness.code_graph import PythonCodeGraph
from autoharness.diagnosis import FailureDiagnoser
from autoharness.models import (
    AgentTrace,
    FailureDiagnosis,
    FailureType,
    Skill,
    SkillHealth,
    SkillHealthStatus,
    SkillLoadIssue,
    SkillMatch,
    SkillOutcomeStats,
    SkillQuery,
    SkillRecommendationResult,
    SkillSearchResult,
)


class SkillRegistryError(RuntimeError):
    """Raised when a Skill directory cannot be indexed safely."""


class SkillRegistry:
    """Index YAML Skills, retain latest versions, and rank relevant experience."""

    max_skill_bytes = 1024 * 1024
    max_skill_files = 5000
    quarantine_minimum_observations = 5
    quarantine_posterior_threshold = 0.3

    def __init__(
        self,
        directory: str | Path,
        outcome_stats: dict[tuple[str, int], SkillOutcomeStats] | None = None,
    ) -> None:
        self.directory = Path(directory).expanduser().resolve()
        self.outcome_stats = outcome_stats or {}

    def search(self, query: SkillQuery) -> SkillSearchResult:
        skills, issues, ignored = self._load_latest()
        matches: list[SkillMatch] = []
        quarantined: list[SkillMatch] = []
        for skill, path in skills.values():
            match = self._score(skill, path, query)
            if match is not None:
                if (
                    match.health is not None
                    and match.health.status == SkillHealthStatus.QUARANTINED
                ):
                    quarantined.append(match)
                    if not query.include_quarantined:
                        continue
                    match = match.model_copy(
                        update={
                            "reasons": [
                                *match.reasons,
                                "quarantined Skill included by explicit override",
                            ]
                        }
                    )
                matches.append(match)
        matches.sort(key=lambda item: (-item.score, item.skill.name, -item.skill.version))
        quarantined.sort(key=lambda item: (-item.score, item.skill.name, -item.skill.version))
        if (
            not query.include_quarantined
            and query.quarantine_probe_index is not None
            and quarantined
        ):
            probe = quarantined[query.quarantine_probe_index % len(quarantined)]
            probe = probe.model_copy(
                update={
                    "quarantine_probe": True,
                    "reasons": [
                        *probe.reasons,
                        "selected as a controlled quarantine recovery probe",
                    ],
                }
            )
            matches = [*matches[: max(0, query.limit - 1)], probe]
        else:
            matches = matches[: query.limit]
        return SkillSearchResult(
            matches=matches,
            indexed_skills=len(skills),
            ignored_older_versions=ignored,
            invalid_files=issues,
            quarantined_skills=[item.health for item in quarantined if item.health is not None],
        )

    def _load_latest(
        self,
    ) -> tuple[dict[str, tuple[Skill, Path]], list[SkillLoadIssue], int]:
        if not self.directory.is_dir():
            raise SkillRegistryError(f"Skill directory is not a directory: {self.directory}")
        candidates = sorted([*self.directory.rglob("*.yaml"), *self.directory.rglob("*.yml")])
        if len(candidates) > self.max_skill_files:
            raise SkillRegistryError(
                f"Skill directory exceeds the {self.max_skill_files} file safety limit"
            )

        latest: dict[str, tuple[Skill, Path]] = {}
        issues: list[SkillLoadIssue] = []
        ignored = 0
        for path in candidates:
            try:
                skill = self._load(path)
            except (OSError, SkillRegistryError, ValidationError, yaml.YAMLError) as exc:
                issues.append(SkillLoadIssue(path=str(path), error=str(exc)))
                continue
            current = latest.get(skill.name)
            if current is None or skill.version > current[0].version:
                if current is not None:
                    ignored += 1
                latest[skill.name] = (skill, path)
            else:
                ignored += 1
        return latest, issues, ignored

    def _load(self, path: Path) -> Skill:
        if path.is_symlink():
            raise SkillRegistryError("Symbolic-link Skill files are not allowed")
        resolved = path.resolve()
        try:
            resolved.relative_to(self.directory)
        except ValueError as exc:
            raise SkillRegistryError("Skill file escapes the registry directory") from exc
        if resolved.stat().st_size > self.max_skill_bytes:
            raise SkillRegistryError("Skill file exceeds the 1 MiB safety limit")
        raw: Any = yaml.safe_load(resolved.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise SkillRegistryError("Skill YAML must contain an object")
        return Skill.model_validate(raw)

    def _score(self, skill: Skill, path: Path, query: SkillQuery) -> SkillMatch | None:
        score = 0.0
        reasons: list[str] = []
        if query.failure_type is not None:
            if skill.failure_type == query.failure_type:
                score += 5.0
                reasons.append(f"failure type matches {query.failure_type.value}")
            elif query.same_failure_only:
                return None

        query_text = " ".join(query.text.split()).casefold()
        query_tokens = self._tokens(query_text)
        for trigger in skill.triggers:
            normalized = " ".join(trigger.split()).casefold()
            if normalized and normalized in query_text:
                score += 3.0
                reasons.append(f"trigger phrase matches '{trigger}'")
                continue
            trigger_tokens = self._tokens(normalized)
            overlap = query_tokens & trigger_tokens
            if overlap:
                ratio = len(overlap) / max(1, len(trigger_tokens))
                score += 2.0 * ratio
                reasons.append(f"trigger tokens match {', '.join(sorted(overlap))}")

        skill_components = {
            str(item).casefold() for item in self._list_context_value(skill, "affected_components")
        }
        for component in query.components:
            normalized = component.casefold()
            if normalized in skill_components:
                score += 2.5
                reasons.append(f"component matches '{component}'")
            elif any(
                normalized in candidate or candidate in normalized for candidate in skill_components
            ):
                score += 1.25
                reasons.append(f"component partially matches '{component}'")

        corpus = " ".join(
            [
                skill.description,
                str(skill.context.get("root_cause", "")),
                *skill.workflow,
                *skill.evaluation,
            ]
        )
        semantic_overlap = query_tokens & self._tokens(corpus.casefold())
        if semantic_overlap:
            score += min(2.0, len(semantic_overlap) * 0.35)
            reasons.append(f"content matches {', '.join(sorted(semantic_overlap)[:5])}")

        if score <= 0:
            return None
        score += min(0.5, skill.version * 0.05)
        reasons.append(f"latest indexed version v{skill.version}")
        outcome = self.outcome_stats.get((skill.name, skill.version))
        if outcome is not None:
            score = max(0.0, score + outcome.score_adjustment)
            direction = "+" if outcome.score_adjustment >= 0 else ""
            evidence_scope = (
                outcome.failure_type.value if outcome.failure_type is not None else "all failures"
            )
            reasons.append(
                f"observed outcomes ({evidence_scope}): "
                f"{outcome.accepted} accepted, {outcome.rejected} rejected, "
                f"{outcome.unevaluated_failures} unevaluated failures; "
                f"Bayesian rate {outcome.posterior_success_rate:.3f}, "
                f"score {direction}{outcome.score_adjustment:.3f}"
            )
            if outcome.control_observations:
                ablation_direction = "+" if outcome.ablation_score_adjustment >= 0 else ""
                reasons.append(
                    "controlled ablation: "
                    f"{outcome.control_observations} controls, control Bayesian rate "
                    f"{outcome.control_posterior_success_rate:.3f}, estimated lift "
                    f"{outcome.estimated_lift:+.3f}, confidence "
                    f"{outcome.ablation_confidence:.3f}, score "
                    f"{ablation_direction}{outcome.ablation_score_adjustment:.3f}"
                )
        health = self._health(skill, outcome)
        reasons.append(f"Skill health is {health.status.value}: {health.reason}")
        return SkillMatch(
            skill=skill,
            path=str(path.resolve()),
            score=round(score, 3),
            reasons=reasons,
            outcome_stats=outcome,
            health=health,
        )

    @classmethod
    def _health(cls, skill: Skill, outcome: SkillOutcomeStats | None) -> SkillHealth:
        if outcome is None:
            return SkillHealth(
                skill_name=skill.name,
                skill_version=skill.version,
                status=SkillHealthStatus.UNOBSERVED,
                observations=0,
                posterior_success_rate=0.5,
                minimum_observations=cls.quarantine_minimum_observations,
                quarantine_threshold=cls.quarantine_posterior_threshold,
                reason="no repository-scoped outcome evidence yet",
            )
        if outcome.observations < cls.quarantine_minimum_observations:
            status = SkillHealthStatus.LEARNING
            reason = (
                f"{outcome.observations}/{cls.quarantine_minimum_observations} observations "
                "collected before health gating"
            )
        elif outcome.posterior_success_rate <= cls.quarantine_posterior_threshold:
            status = SkillHealthStatus.QUARANTINED
            reason = (
                f"Bayesian success rate {outcome.posterior_success_rate:.3f} is at or below "
                f"{cls.quarantine_posterior_threshold:.3f} after "
                f"{outcome.observations} observations"
            )
        else:
            status = SkillHealthStatus.HEALTHY
            reason = (
                f"Bayesian success rate {outcome.posterior_success_rate:.3f} remains above "
                f"{cls.quarantine_posterior_threshold:.3f} after "
                f"{outcome.observations} observations"
            )
        return SkillHealth(
            skill_name=skill.name,
            skill_version=skill.version,
            status=status,
            observations=outcome.observations,
            posterior_success_rate=outcome.posterior_success_rate,
            minimum_observations=cls.quarantine_minimum_observations,
            quarantine_threshold=cls.quarantine_posterior_threshold,
            reason=reason,
        )

    @staticmethod
    def _tokens(text: str) -> set[str]:
        stopwords = {
            "a",
            "an",
            "and",
            "are",
            "as",
            "at",
            "be",
            "by",
            "for",
            "from",
            "has",
            "in",
            "is",
            "it",
            "of",
            "on",
            "or",
            "that",
            "the",
            "to",
            "was",
            "with",
        }
        return {
            token
            for token in re.findall(r"\w+", text, flags=re.UNICODE)
            if len(token) > 1 and token not in stopwords
        }

    @staticmethod
    def _list_context_value(skill: Skill, key: str) -> list[Any]:
        value = skill.context.get(key, [])
        return value if isinstance(value, list) else []


class SkillRecommender:
    """Combine trace diagnosis, code localization, and learned Skill retrieval."""

    def __init__(self) -> None:
        self.diagnoser = FailureDiagnoser()

    def recommend(
        self,
        trace: AgentTrace,
        skill_directory: str | Path,
        *,
        repository_path: str | Path | None = None,
        limit: int = 5,
        same_failure_only: bool = True,
        allow_missing_directory: bool = False,
        diagnosis: FailureDiagnosis | None = None,
        outcome_stats: dict[tuple[str, int], SkillOutcomeStats] | None = None,
        include_quarantined: bool = False,
        quarantine_probe_index: int | None = None,
    ) -> SkillRecommendationResult:
        diagnosis = diagnosis or self.diagnoser.diagnose(trace)
        locations = []
        if repository_path is not None:
            graph = PythonCodeGraph(repository_path)
            graph.build()
            locations = graph.locate(diagnosis, limit=8)
        text = " ".join(
            [
                trace.task,
                *trace.logs,
                trace.feedback or "",
                diagnosis.summary,
                *diagnosis.likely_causes,
                *diagnosis.search_terms,
                *(evidence.excerpt for evidence in diagnosis.evidence),
            ]
        )
        components = list(
            dict.fromkeys(
                value
                for location in locations
                for value in (location.symbol, Path(location.path).stem)
            )
        )
        directory = Path(skill_directory).expanduser().resolve()
        if allow_missing_directory and not directory.exists():
            search = SkillSearchResult(matches=[], indexed_skills=0)
        else:
            search = SkillRegistry(directory, outcome_stats).search(
                SkillQuery(
                    failure_type=diagnosis.failure_type,
                    text=text,
                    components=components,
                    limit=limit,
                    same_failure_only=(
                        same_failure_only and diagnosis.failure_type != FailureType.UNKNOWN
                    ),
                    include_quarantined=include_quarantined,
                    quarantine_probe_index=quarantine_probe_index,
                )
            )
        return SkillRecommendationResult(
            diagnosis=diagnosis,
            code_locations=locations,
            skills=search,
        )
