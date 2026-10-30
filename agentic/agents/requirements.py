"""Requirements agent: interpret intent, surface ambiguity, normalize into a spec."""

from __future__ import annotations

import re

from ..approvals import Question
from ..llm import as_data
from .base import Agent, AgentContext, AgentError, AgentResult

# Words that signal an unmeasurable or under-specified requirement.
VAGUE_TERMS = {
    "secure", "safer", "safe", "safety", "fast", "faster", "scalable", "robust", "better",
    "enterprise", "enterprise-ready", "user-friendly", "simple", "easy", "appropriate",
    "reasonable", "modern", "improve", "improved", "good", "flexible", "production-ready",
    "abuse", "etc", "some", "various", "proper", "properly",
}

SPEC_SCHEMA = """{
  "problem_statement": str,
  "functional": [{"id": "FR-1", "text": str, "acceptance": [str]}],
  "non_functional": [{"id": "NFR-1", "text": str, "acceptance": [str]}],
  "out_of_scope": [str],
  "assumptions": [str],
  "ambiguities": [{"term": str, "why": str}],
  "questions": [{"id": "Q1", "text": str, "options": [str], "recommended": int, "rationale": str}],
  "keywords": [str]
}"""


def detect_vague_terms(text: str) -> list[dict]:
    found = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        words = {w.lower().strip(".,;:!?()\"'") for w in sentence.split()}
        for term in sorted(words & VAGUE_TERMS):
            found.append({"term": term, "why": f"'{term}' is not measurable as stated: \"{sentence.strip()[:120]}\""})
    return found


def validate_spec(spec: dict) -> dict:
    if not isinstance(spec.get("problem_statement"), str) or not spec["problem_statement"].strip():
        raise AgentError("spec missing problem_statement")
    if not spec.get("functional"):
        raise AgentError("spec has no functional requirements")
    ids = set()
    for group in ("functional", "non_functional"):
        for item in spec.get(group, []):
            if not item.get("id") or not item.get("text"):
                raise AgentError(f"{group} item missing id/text: {item}")
            if not item.get("acceptance"):
                raise AgentError(f"{item['id']} has no acceptance criteria")
            if item["id"] in ids:
                raise AgentError(f"duplicate requirement id {item['id']}")
            ids.add(item["id"])
    for q in spec.get("questions", []):
        if not q.get("options") or not 0 <= q.get("recommended", 0) < len(q["options"]):
            raise AgentError(f"question {q.get('id')} has invalid options/recommended")
    for key in ("non_functional", "out_of_scope", "assumptions", "ambiguities", "questions", "keywords"):
        spec.setdefault(key, [])
    return spec


class RequirementsAgent(Agent):
    name = "requirements"
    role = ("You are a staff engineer turning a raw product request into a precise, testable "
            "engineering specification. You never invent scope; you ask when intent is unclear.")

    def _finish(self, ctx: AgentContext, spec: dict) -> AgentResult:
        text = ctx.scenario["requirement"]
        spec = validate_spec(spec)
        known = {a["term"] for a in spec["ambiguities"]}
        spec["ambiguities"] += [a for a in detect_vague_terms(text) if a["term"] not in known]
        spec["raw_requirement"] = text
        if ctx.feedback:
            spec["reviewer_amendments"] = list(ctx.feedback)
        questions = [Question(**q) for q in spec["questions"]]
        n_req = len(spec["functional"]) + len(spec["non_functional"])
        decisions = [{
            "summary": f"Normalized request into {n_req} requirements"
                       + (f" with {len(questions)} open question(s)" if questions else ""),
            "rationale": "Each requirement has an id and acceptance criteria so it can be traced "
                         "to tasks and tests; vague terms are surfaced rather than guessed.",
            "alternatives": ["Proceed on agent assumptions without asking (rejected: unbounded autonomy)"]
                            if questions else [],
        }]
        return AgentResult(
            output=spec,
            summary=f"{n_req} requirements, {len(spec['ambiguities'])} ambiguity flag(s), "
                    f"{len(questions)} question(s)",
            decisions=decisions,
            questions=questions,
        )

    def run_offline(self, ctx: AgentContext) -> AgentResult:
        return self._finish(ctx, ctx.playbook.requirements(ctx.scenario["requirement"], ctx.feedback))

    def run_llm(self, ctx: AgentContext) -> AgentResult:
        data, tokens = self.ask_json(
            ctx,
            "Normalize the requirement below into a specification. Flag every ambiguous or "
            "unmeasurable phrase and, for ambiguities that change the design, ask a multiple-choice "
            "question with a recommended option. Keep scope to what was asked.",
            [as_data("requirement", ctx.scenario["requirement"]),
             as_data("system_context", ctx.scenario.get("context", ""))],
            SPEC_SCHEMA,
        )
        result = self._finish(ctx, data)
        result.tokens = tokens
        return result
