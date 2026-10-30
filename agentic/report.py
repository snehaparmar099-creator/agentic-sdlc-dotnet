"""Run reports: report.md (audit-grade run record) and ENGINEERING_SUMMARY.md."""

from __future__ import annotations

import json
from pathlib import Path

from .audit import read_events, verify
from .state import RunState
from .workspace import Workspace


def _table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in r) + " |")
    return "\n".join(out)


def write_reports(run_dir: Path) -> tuple[Path, Path]:
    st = RunState.load(run_dir)
    ws = Workspace(run_dir / "workspace")
    metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8")) if (run_dir / "metrics.json").exists() else {}
    events = read_events(run_dir / "audit.jsonl")
    chain_ok, chain_msg = verify(run_dir / "audit.jsonl")
    out = lambda nid: (st.nodes.get(nid).output or {}) if nid in st.nodes else {}  # noqa: E731

    t0 = events[0]["ts"] if events else ""
    timeline = [[e["seq"], e["ts"][11:23], e["event"], e["data"].get("node", ""),
                 (e["data"].get("reason") or e["data"].get("summary") or e["data"].get("message")
                  or e["data"].get("decision") or "")[:110]]
                for e in events if e["event"] in {
                    "node_succeeded", "attempt_failed", "retry_scheduled", "fallback", "rollback",
                    "approval_decided", "graph_expand", "graph_replan", "node_invalidated", "node_revalidated",
                    "rerun_requested", "safe_stop", "run_finished", "revision_requested",
                    "clarifications_carried_over", "attempt_discarded"}]

    nodes = [[nid, ns.status, ns.agent or st.graph.nodes[nid].agent, ns.attempts, ns.revisions,
              "yes" if ns.used_fallback else "", (ns.commit or "")[:8], len(ns.changed_files)]
             for nid, ns in ((n, st.nodes[n]) for n in st.graph.topo_order())]
    approvals = [[a["request"]["id"], a["request"]["node_id"], a["request"]["kind"],
                  a["decision"]["decision"], a["decision"]["approver"],
                  ("(unattended) " if a["decision"].get("unattended") else "") + (a["decision"]["comment"] or "")[:90]]
                 for a in st.approvals]
    decisions = [[d.id, d.node, d.made_by, d.summary[:110], ",".join(d.parents[-4:]) or "-",
                  d.superseded_by or ""] for d in st.decisions]
    findings = [[f["node"], f["attempt"], f["severity"], f["rule"], f.get("path") or "", f["message"][:80]]
                for f in st.findings]
    try:
        gitlog = "\n".join(ws.log())
        diffstat = ws.diff("baseline", "HEAD", stat=True)
    except Exception as exc:  # workspace may have been removed
        gitlog = diffstat = f"(unavailable: {exc})"
    suite = out("test").get("gates", {}).get("full_suite", {})

    report = f"""# Run report `{st.run_id}`

| | |
|---|---|
| Scenario | {st.scenario.get('title')} |
| Status | **{st.status}**{f' ({st.stop_reason})' if st.stop_reason else ''} |
| LLM | {st.config.get('llm')} |
| Approvals | {st.config.get('approvals')} |
| Audit chain | {'intact' if chain_ok else 'BROKEN'}: {chain_msg} ({len(events)} events) |
| Tests | {suite.get('message', 'n/a')} |

## Workflow graph (final)
```mermaid
{st.graph.to_mermaid(st.statuses())}
```

## Nodes
{_table(['node', 'status', 'agent', 'attempts', 'revisions', 'fallback', 'commit', 'files'], nodes)}

## Reliability metrics
```json
{json.dumps({k: v for k, v in metrics.items() if k != 'node_latency_s'}, indent=2)}
```

## Human checkpoints
{_table(['id', 'node', 'kind', 'decision', 'approver', 'comment'], approvals) if approvals else '_none_'}

## Decision lineage
Each decision lists the upstream decisions it builds on; superseded decisions were replaced by a re-run.

{_table(['id', 'node', 'made by', 'decision', 'parents', 'superseded by'], decisions)}

## Policy findings
{_table(['node', 'attempt', 'severity', 'rule', 'path', 'message'], findings) if findings else '_none_'}

## Timeline (key events; full log in audit.jsonl, started {t0})
{_table(['seq', 'time', 'event', 'node', 'detail'], timeline)}

## Workspace history
```
{gitlog}
```
```
{diffstat}
```
"""
    report_path = run_dir / "report.md"
    report_path.write_text(report, encoding="utf-8")

    req, design = out("requirements"), out("design")
    plan = design
    threats, review, release, analysis = out("threat_model"), out("security_review"), out("release"), out("analysis")
    verification = {v["id"]: v["status"] for v in review.get("risk_verification", [])}
    risk_rows = [[r["id"], r.get("impact", ""), r["risk"], r["mitigation"], verification.get(r["id"], "not reviewed")]
                 for r in threats.get("risks", [])]
    clar = req.get("clarifications") or {}
    summary = f"""# Engineering summary: {st.scenario.get('title')}

Run `{st.run_id}` finished **{st.status}**. Release readiness: **{'GO' if release.get('ready') else 'NO-GO / not reached'}**.

## 1. Problem and plan
**Request.** {st.scenario.get('requirement')}

**Normalized problem.** {req.get('problem_statement', 'n/a')}

{_table(['id', 'requirement', 'acceptance'], [[r['id'], r['text'], '; '.join(r['acceptance'])] for r in req.get('functional', []) + req.get('non_functional', [])])}

{('**Clarifications (human answers).**' + chr(10) + chr(10) + _table(['question', 'answer', 'answered by'], [[c['question'], c['answer'], c['answered_by']] for c in clar.values()])) if clar else ''}
{('**Reviewer amendments.** ' + '; '.join(req.get('reviewer_amendments', []))) if req.get('reviewer_amendments') else ''}

**Design.** {design.get('summary', 'n/a')}

{_table(['ADR', 'decision', 'rationale', 'alternatives'], [[a['id'] + ' ' + a['title'], a['decision'], a['rationale'], '; '.join(a.get('alternatives', []))] for a in design.get('adrs', [])])}

**Task plan** (parallel waves: {' | '.join(', '.join(w) for w in plan.get('waves', []))}).

{_table(['task', 'deps', 'files', 'requirements'], [[t['id'], ', '.join(t['deps']) or '-', ', '.join(t['files']), ', '.join(t['requirements'])] for t in plan.get('tasks', [])])}

{('**Codebase analysis.** Impacted modules: ' + ', '.join(analysis.get('impact', {}).get('impacted_modules', [])) + '. Tests to re-run: ' + ', '.join(analysis.get('impact', {}).get('tests_to_run', [])) + '. Data flow: ' + analysis.get('data_flow', '')) if analysis else ''}

## 2. Artifacts
- Code and tests: `workspace/` (git history: one commit per successful node; `baseline` tag = starting point)
- Release notes with traceability and rollback plan: `artifacts/RELEASE_NOTES.md`
- Run record: `report.md`, `audit.jsonl` (hash-chained), `state.json`, `metrics.json`

## 3. Risks, trade-offs and validation
{_table(['risk', 'impact', 'description', 'mitigation', 'verification'], risk_rows) if risk_rows else '_no threat model_'}

Validation: per-task test gates, full-suite gate ({out('test').get('gates', {}).get('full_suite', {}).get('message', 'n/a')}),
independent security re-scan of {len(review.get('files_reviewed', []))} changed files ({review.get('blocking', 'n/a')} blocking findings),
docs/OpenAPI drift check, release checklist.

## 4. Assumptions
{chr(10).join('- ' + a for a in req.get('assumptions', [])) or '- none recorded'}

## 5. Limitations
- Out of scope: {', '.join(req.get('out_of_scope', [])) or 'n/a'}
- Accepted risks: {', '.join(r['id'] + ' ' + r['risk'] for r in threats.get('risks', []) if not r.get('verified_by')) or 'none'}
- Offline agents replay curated playbooks; with an API key configured, agents call the LLM and fall back to playbooks on invalid output.
"""
    summary_path = run_dir / "ENGINEERING_SUMMARY.md"
    summary_path.write_text(summary, encoding="utf-8")
    return report_path, summary_path
