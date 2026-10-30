"""Command line interface.

  python -m agentic run greenfield [--approvals cli|auto|scripted] [--llm auto|offline|anthropic]
  python -m agentic demo                      # all three scenarios, scripted approvals, offline
  python -m agentic resume <run_id>
  python -m agentic status <run_id>
  python -m agentic stop <run_id>             # safe stop: finishes in-flight work, then halts
  python -m agentic rollback <run_id> --to baseline|<node_id>
  python -m agentic verify-audit <run_id>
  python -m agentic metrics [run_id ...]      # aggregate reliability metrics
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .audit import AuditLog, verify
from .engine import rollback_run
from .metrics import aggregate
from .pipeline import RUNS_DIR, create_run, load_run, load_scenario
from .report import write_reports
from .state import RunState
from .workspace import Workspace


def _finish(engine) -> int:
    status = engine.run()
    report, summary = write_reports(engine.state.run_dir)
    m = engine.metrics.summary()
    print("\n" + "=" * 78)
    print(f"run {engine.state.run_id}: {status.upper()}"
          + (f" ({engine.state.stop_reason})" if engine.state.stop_reason else ""))
    print(f"  nodes {m['nodes_succeeded']}/{m['nodes_total']} succeeded | retries {m['retries']} | "
          f"fallbacks {m['fallbacks']} | rollbacks {m['rollbacks']} | invalidations {m['invalidations']} | "
          f"approvals {m['approvals_requested']} ({m['approvals_rejected']} not approved)")
    print(f"  e2e {m['e2e_latency_s']}s (agent time {m['agent_time_s']}s) | MTTR {str(m['mttr_s']) + 's' if m['mttr_s'] is not None else 'n/a (no failures)'}")
    rel = lambda p: p.relative_to(Path.cwd()) if p.is_relative_to(Path.cwd()) else p  # noqa: E731
    print(f"  workspace : {rel(engine.state.run_dir / 'workspace')}")
    print(f"  report    : {rel(report)}")
    print(f"  summary   : {rel(summary)}")
    return 0 if status == "succeeded" else 1


def cmd_run(args) -> int:
    scenario = load_scenario(args.scenario)
    approvals = "auto" if args.auto_approve else args.approvals
    engine = create_run(scenario, approvals=approvals, llm_mode=args.llm, max_parallel=args.max_parallel,
                        from_run=args.from_run, rollback_on_stop=args.rollback_on_stop,
                        faults=not args.no_faults, run_id=args.run_id, verbose=not args.quiet)
    print(f"run {engine.state.run_id} | scenario {scenario['id']} | llm={engine.llm.name} | approvals={approvals}")
    return _finish(engine)


def cmd_demo(args) -> int:
    codes = []
    for name in ("greenfield", "brownfield", "ambiguous"):
        print(f"\n{'#' * 78}\n# scenario: {name}\n{'#' * 78}")
        engine = create_run(load_scenario(name), approvals="scripted", llm_mode=args.llm,
                            max_parallel=args.max_parallel, verbose=not args.quiet)
        codes.append(_finish(engine))
    ids = sorted(RUNS_DIR.glob("*/metrics.json"))[-3:]
    print("\naggregate:", json.dumps(aggregate(ids), indent=2))
    return max(codes)


def cmd_resume(args) -> int:
    engine = load_run(args.run_id, approvals=args.approvals)
    return _finish(engine)


def cmd_status(args) -> int:
    st = RunState.load(RUNS_DIR / args.run_id)
    print(f"{st.run_id}: {st.status}" + (f" ({st.stop_reason})" if st.stop_reason else ""))
    for nid in st.graph.topo_order():
        ns = st.nodes[nid]
        print(f"  {nid:28} {ns.status:18} attempts={ns.attempts} revisions={ns.revisions}"
              + (f" commit={ns.commit[:8]}" if ns.commit else ""))
    return 0


def cmd_stop(args) -> int:
    (RUNS_DIR / args.run_id / "STOP").write_text("stop requested\n", encoding="utf-8")
    print(f"safe stop requested for {args.run_id}; in-flight nodes will finish first")
    return 0


def cmd_rollback(args) -> int:
    run_dir = RUNS_DIR / args.run_id
    st = RunState.load(run_dir)
    audit = AuditLog(run_dir / "audit.jsonl", st.run_id)
    rolled = rollback_run((st, Workspace(run_dir / "workspace"), audit, None), args.to,
                          reason=f"manual rollback via CLI to {args.to}")
    print(f"workspace reset to {args.to}; nodes marked rolled_back: {', '.join(rolled) or 'none'}")
    return 0


def cmd_verify(args) -> int:
    ok, msg = verify(RUNS_DIR / args.run_id / "audit.jsonl")
    print(msg)
    return 0 if ok else 2


def cmd_metrics(args) -> int:
    files = [RUNS_DIR / r / "metrics.json" for r in args.run_ids] if args.run_ids \
        else sorted(RUNS_DIR.glob("*/metrics.json"))
    print(json.dumps(aggregate([f for f in files if f.exists()]), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agentic", description="Agentic SDLC orchestrator")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="start a scenario run")
    r.add_argument("scenario", help="scenario name (greenfield|brownfield|ambiguous) or path to JSON")
    r.add_argument("--approvals", choices=["cli", "auto", "scripted"], default="cli")
    r.add_argument("--auto-approve", action="store_true", help="shorthand for --approvals auto")
    r.add_argument("--llm", default="auto", choices=["auto", "offline", "anthropic"])
    r.add_argument("--max-parallel", type=int, default=4)
    r.add_argument("--from-run", help="seed the workspace from a previous run's workspace")
    r.add_argument("--rollback-on-stop", action="store_true")
    r.add_argument("--no-faults", action="store_true", help="disable the scenario's fault injection")
    r.add_argument("--run-id")
    r.add_argument("--quiet", action="store_true")
    r.set_defaults(fn=cmd_run)

    d = sub.add_parser("demo", help="run all three scenarios with scripted approvals")
    d.add_argument("--llm", default="offline", choices=["auto", "offline", "anthropic"])
    d.add_argument("--max-parallel", type=int, default=4)
    d.add_argument("--quiet", action="store_true")
    d.set_defaults(fn=cmd_demo)

    s = sub.add_parser("resume")
    s.add_argument("run_id")
    s.add_argument("--approvals", choices=["cli", "auto", "scripted"])
    s.set_defaults(fn=cmd_resume)

    for name, fn in (("status", cmd_status), ("stop", cmd_stop), ("verify-audit", cmd_verify)):
        x = sub.add_parser(name)
        x.add_argument("run_id")
        x.set_defaults(fn=fn)

    rb = sub.add_parser("rollback")
    rb.add_argument("run_id")
    rb.add_argument("--to", default="baseline", help="'baseline' or a node id")
    rb.set_defaults(fn=cmd_rollback)

    m = sub.add_parser("metrics")
    m.add_argument("run_ids", nargs="*")
    m.set_defaults(fn=cmd_metrics)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
