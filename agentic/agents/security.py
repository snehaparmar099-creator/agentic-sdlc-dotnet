"""Security agent, used at two points in the workflow (node param `mode`):

threat_model  before design: a STRIDE-style risk register. Every high-impact
              risk must name the tests that will prove its mitigation.
review        after testing: an independent policy re-scan of every changed
              file, plus verification that each risk's named tests exist and
              passed. Review mode is deterministic by design: the model that
              generated the code is not the one certifying it.
"""

from __future__ import annotations

from ..policy import BLOCK, PolicyEngine
from .base import Agent, AgentContext, AgentError, AgentResult

THREAT_SCHEMA = """{"risks": [{"id": "R1", "category": str, "risk": str, "likelihood": "low|medium|high",
  "impact": "low|medium|high", "mitigation": str, "verified_by": ["tests/UrlShortener.Tests/ApiTests.cs::Test_name"]}]}"""


def _test_passed(test_id: str, passed: list[str]) -> bool:
    return any(p == test_id or p.split("[")[0] == test_id for p in passed)


class SecurityAgent(Agent):
    name = "security"
    role = "You are an application security engineer performing a lightweight STRIDE threat model."

    def _mode(self, ctx: AgentContext) -> str:
        return ctx.node.params.get("mode", "threat_model")

    def supports_llm(self, ctx: AgentContext) -> bool:
        return self._mode(ctx) == "threat_model"

    def run_offline(self, ctx: AgentContext) -> AgentResult:
        if self._mode(ctx) == "review":
            return self._review(ctx)
        return self._threats(ctx.playbook.threats(ctx.find("requirements") or {}))

    def run_llm(self, ctx: AgentContext) -> AgentResult:
        data, tokens = self.ask_json(ctx, "Threat-model this change. For each high-impact risk, name the "
                                     "test ids (tests/<Project>/<Class>.cs::<Method>) that should verify the mitigation.",
                                     self.upstream_block(ctx, "requirements"), THREAT_SCHEMA)
        result = self._threats(data.get("risks", []))
        result.tokens = tokens
        return result

    # -- threat model --------------------------------------------------------
    def _threats(self, risks: list[dict]) -> AgentResult:
        for r in risks:
            if not all(r.get(k) for k in ("id", "risk", "mitigation")):
                raise AgentError(f"incomplete risk entry: {r}")
            r.setdefault("verified_by", [])
        high = [r["id"] for r in risks if r.get("impact") == "high"]
        decisions = [{
            "summary": f"Risk register: {len(risks)} risks, {len(high)} high impact",
            "rationale": "High-impact risks must name the tests that verify their mitigation; the "
                         "security review fails if those tests are missing or red.",
            "alternatives": [],
        }]
        return AgentResult(output={"risks": risks, "high_impact": high},
                           summary=f"{len(risks)} risks ({len(high)} high)", decisions=decisions)

    # -- review ----------------------------------------------------------------
    def _review(self, ctx: AgentContext) -> AgentResult:
        policy = PolicyEngine(ctx.scenario.get("policy_overrides"))
        ws = ctx.workspace
        changed = ws.changed_files("baseline")
        findings = []
        for path in changed:
            findings += [f.to_dict() for f in policy.scan(path, ws.read(path), ws.read_at("baseline", path))]
        blocking = [f for f in findings if f["severity"] == BLOCK]

        passed = (ctx.find("test") or {}).get("gates", {}).get("full_suite", {}).get("passed_ids", [])
        verification = []
        for r in (ctx.find("threat_model") or {}).get("risks", []):
            ids = r.get("verified_by", [])
            missing = [t for t in ids if not _test_passed(t, passed)]
            status = "verified" if ids and not missing else ("unverified" if not ids else "failing_or_missing")
            verification.append({"id": r["id"], "impact": r.get("impact"), "status": status,
                                 "missing_tests": missing})
        unverified_high = [v["id"] for v in verification if v["impact"] == "high" and v["status"] != "verified"]
        ok = not blocking and not unverified_high
        return AgentResult(
            output={"status": "pass" if ok else "fail", "files_reviewed": changed, "findings": findings,
                    "blocking": len(blocking), "risk_verification": verification,
                    "unverified_high_risks": unverified_high},
            summary=f"{len(changed)} files reviewed, {len(blocking)} blocking finding(s), "
                    f"{sum(v['status'] == 'verified' for v in verification)}/{len(verification)} risks verified",
            decisions=[{"summary": f"Security review {'passed' if ok else 'failed'}",
                        "rationale": "Re-scanned every changed file against policy independent of per-node "
                                     "checks, and matched each high-impact risk to passing tests.",
                        "alternatives": []}],
        )
