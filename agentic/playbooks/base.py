"""Playbooks: the deterministic knowledge agents use when no LLM is configured
(or when the LLM path fails validation).

A playbook supplies domain content only (requirements, design, tasks, code).
Everything procedural (ambiguity detection, codebase analysis, decomposition
validation, policy, testing, docs assembly, release readiness) is generic
agent/engine logic that runs identically in LLM and offline modes.
"""

from __future__ import annotations

from pathlib import Path

from ..workspace import Change

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_DIR = REPO_ROOT / "service"


class Playbook:
    name = "base"

    def requirements(self, text: str, feedback: list[str]) -> dict:
        raise NotImplementedError

    def design(self, requirements: dict, analysis: dict | None, feedback: list[str]) -> dict:
        raise NotImplementedError

    def threats(self, requirements: dict) -> list[dict]:
        return []

    def plan(self, requirements: dict, design: dict, analysis: dict | None) -> list[dict]:
        raise NotImplementedError

    def implement(self, task: dict, feedback: list[str]) -> list[Change]:
        raise NotImplementedError

    def integration_tests(self) -> list[Change]:
        return []

    def changelog(self, requirements: dict) -> str:
        return ""


def service_file(rel: str) -> str:
    return (SERVICE_DIR / rel).read_text(encoding="utf-8")


def get_playbook(name: str) -> Playbook:
    from .ambiguous import AmbiguousPlaybook
    from .brownfield import BrownfieldPlaybook
    from .greenfield import GreenfieldPlaybook

    registry = {p.name: p for p in (GreenfieldPlaybook(), BrownfieldPlaybook(), AmbiguousPlaybook())}
    if name not in registry:
        raise KeyError(f"unknown playbook {name!r}; known: {sorted(registry)}")
    return registry[name]
