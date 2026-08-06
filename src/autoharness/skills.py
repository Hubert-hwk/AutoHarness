"""Experience-to-skill conversion and local skill persistence."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from autoharness.models import RepairExperience, Skill


class SkillGenerator:
    def from_experience(self, experience: RepairExperience) -> Skill:
        return Skill(
            name=self._slug(experience.title),
            description=f"Repair playbook: {experience.title}",
            failure_type=experience.failure_type,
            triggers=list(dict.fromkeys(experience.trigger_terms)),
            context={
                "root_cause": experience.root_cause,
                "affected_components": experience.affected_components,
                "success_metrics": experience.success_metrics,
            },
            workflow=experience.repair_steps,
            evaluation=experience.validation_steps,
        )

    def save(self, skill: Skill, directory: str | Path) -> Path:
        output_dir = Path(directory).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{skill.name}.yaml"
        data: dict[str, Any] = skill.model_dump(mode="json")
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return path

    def save_versioned(self, skill: Skill, directory: str | Path) -> tuple[Skill, Path]:
        """Preserve skill history by allocating the next immutable version."""
        output_dir = Path(directory).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        versions: list[int] = []
        for path in output_dir.glob(f"{skill.name}.v*.yaml"):
            match = re.fullmatch(rf"{re.escape(skill.name)}\.v(\d+)\.yaml", path.name)
            if match:
                versions.append(int(match.group(1)))
        versioned = skill.model_copy(update={"version": max(versions, default=0) + 1})
        path = output_dir / f"{skill.name}.v{versioned.version}.yaml"
        data: dict[str, Any] = versioned.model_dump(mode="json")
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return versioned, path

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
        return slug or "repair_skill"
