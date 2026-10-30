"""Codebase analyst: static analysis of an existing codebase (brownfield).

Builds a module map (symbols, dependencies, HTTP routes, SQL tables), a
dependency graph, and a test map, then computes the impact set for the requirement:
modules whose identifiers/docstrings match requirement keywords, expanded to
every module that (transitively) imports them, plus the tests covering them.
Fully deterministic; an LLM, when configured, only adds a narrative summary.

Python files are parsed with `ast`. C# files are scanned lexically: declared
types, methods, minimal-API routes (app.MapGet/MapPost/...), and a dependency
graph from which files reference which other files' declared types.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import PurePosixPath

from ..llm import as_data
from .base import Agent, AgentContext, AgentResult

TABLE_RE = re.compile(r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?(\w+)", re.IGNORECASE)


def module_name(path: str) -> str:
    p = PurePosixPath(path)
    parts = list(p.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def analyze_module(path: str, source: str) -> dict:
    tree = ast.parse(source)
    mod = module_name(path)
    pkg = mod.rsplit(".", 1)[0] if "." in mod else mod
    info = {"path": path, "module": mod, "functions": [], "classes": [], "imports": set(),
            "routes": [], "tables": [], "docstring": ast.get_docstring(tree) or "", "loc": len(source.splitlines())}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            info["functions"].append(node.name)
        elif isinstance(node, ast.ClassDef):
            info["classes"].append(node.name)
        elif isinstance(node, ast.Import):
            info["imports"].update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:  # relative import
                anchor = pkg.split(".")
                anchor = anchor[: len(anchor) - (node.level - 1)] if node.level > 1 else anchor
                base = ".".join(anchor + ([base] if base else []))
            info["imports"].add(base)
            for a in node.names:
                info["imports"].add(f"{base}.{a.name}")
        elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Route" and node.args:
            if isinstance(node.args[0], ast.Constant):
                methods = []
                for kw in node.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, ast.List):
                        methods = [e.value for e in kw.value.elts if isinstance(e, ast.Constant)]
                handler = getattr(node.args[1], "id", "?") if len(node.args) > 1 else "?"
                info["routes"].append({"path": node.args[0].value, "methods": methods, "handler": handler})
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            info["tables"] += TABLE_RE.findall(node.value)
    info["imports"] = sorted(info["imports"])
    return info


CS_TYPE_RE = re.compile(r"\b(?:class|record|interface|struct|enum)\s+([A-Z]\w*)")
CS_METHOD_RE = re.compile(
    r"^\s*(?:public|private|internal|protected)\s+(?:(?:static|async|override|virtual|sealed|partial)\s+)*"
    r"[\w<>\[\],?.\s()]+?\s+([A-Z]\w*)\s*\(", re.MULTILINE)
CS_ROUTE_RE = re.compile(r"\.Map(Get|Post|Put|Delete|Patch)\(\s*\"([^\"]+)\"(?:\s*,\s*([A-Za-z_]\w*))?")
CS_METHODS_ROUTE_RE = re.compile(r"\.MapMethods\(\s*\"([^\"]+)\"\s*,\s*new\[\]\s*\{([^}]*)\}\s*,\s*([A-Za-z_]\w*)")
CS_DOC_RE = re.compile(r"^\s*///?\s?(.*)$")


def analyze_csharp(path: str, source: str) -> dict:
    mod = module_name(path)
    doc = []
    for line in source.splitlines():
        m = CS_DOC_RE.match(line)
        if m:
            doc.append(re.sub(r"</?\w+[^>]*>", "", m.group(1)).strip())
        elif doc and line.strip() and not line.lstrip().startswith(("[", "public", "internal")):
            continue
    routes = [{"path": p, "methods": [m.upper()], "handler": h or "lambda"} for m, p, h in CS_ROUTE_RE.findall(source)]
    routes += [{"path": p, "methods": re.findall(r'"([A-Z]+)"', ms), "handler": h}
               for p, ms, h in CS_METHODS_ROUTE_RE.findall(source)]
    return {"path": path, "module": mod, "functions": sorted(set(CS_METHOD_RE.findall(source))),
            "classes": sorted(set(CS_TYPE_RE.findall(source))), "imports": [], "routes": routes,
            "tables": TABLE_RE.findall(source), "docstring": " ".join(d for d in doc if d)[:400],
            "loc": len(source.splitlines()), "language": "csharp"}


def _link_csharp(modules: dict, sources: dict[str, str]) -> None:
    """A C# file depends on another if it references a type that file declares."""
    declared = {mod: info["classes"] for mod, info in modules.items() if info.get("language") == "csharp"}
    for mod, src in sources.items():
        deps = set()
        for other, types in declared.items():
            if other != mod and any(re.search(rf"\b{re.escape(t)}\b", src) for t in types):
                deps.add(other)
        modules[mod]["imports"] = sorted(deps)


def build_index(ws) -> dict:
    modules = {}
    cs_sources = {}
    for path in ws.list_files(".cs"):
        src = ws.read(path) or ""
        modules[module_name(path)] = analyze_csharp(path, src)
        cs_sources[module_name(path)] = src
    _link_csharp(modules, cs_sources)
    for path in ws.list_files(".py"):
        try:
            modules[module_name(path)] = analyze_module(path, ws.read(path))
        except SyntaxError as exc:
            modules[module_name(path)] = {"path": path, "module": module_name(path), "error": str(exc),
                                          "functions": [], "classes": [], "imports": [], "routes": [],
                                          "tables": [], "docstring": "", "loc": 0}
    # Reverse import graph restricted to in-repo modules.
    importers: dict[str, set[str]] = defaultdict(set)
    for mod, info in modules.items():
        for imp in info["imports"]:
            for target in modules:
                if imp == target or imp.startswith(target + "."):
                    if target != mod:
                        importers[target].add(mod)
    return {"modules": modules, "importers": {k: sorted(v) for k, v in importers.items()}}


def impact(index: dict, keywords: list[str]) -> dict:
    modules, importers = index["modules"], index["importers"]
    kws = [k.lower() for k in keywords if k]
    direct = {}
    for mod, info in modules.items():
        if mod.startswith("tests"):
            continue
        haystack = " ".join([mod, info["docstring"], *info["functions"], *info["classes"],
                             *[r["path"] for r in info["routes"]], *info["tables"]]).lower()
        hits = sorted({k for k in kws if k in haystack})
        if hits:
            direct[mod] = hits
    impacted = set(direct)
    frontier = list(direct)
    while frontier:
        for up in importers.get(frontier.pop(), []):
            if up not in impacted and not up.startswith("tests"):
                impacted.add(up)
                frontier.append(up)
    tests = sorted({
        modules[t]["path"] for t in modules if t.startswith("tests")
        for m in impacted if any(imp == m or imp.startswith(m + ".") for imp in modules[t]["imports"])
    })
    return {
        "direct_matches": direct,
        "impacted_modules": sorted(impacted),
        "impacted_files": sorted(modules[m]["path"] for m in impacted),
        "tests_to_run": tests,
    }


def _baseline(ctx: AgentContext) -> dict:
    # Only deterministic fields: timings would change the output fingerprint on every
    # re-run and trigger needless downstream invalidation.
    rep = ctx.run_tests()
    return {"passed": rep.passed, "total": rep.total, "passed_ids": sorted(rep.passed_ids),
            "failed_ids": sorted(rep.failed_ids)}


class CodebaseAnalystAgent(Agent):
    name = "analyst"
    role = "You are a staff engineer reviewing an unfamiliar codebase before changing it."

    def run_offline(self, ctx: AgentContext) -> AgentResult:
        req = ctx.find("requirements") or {}
        index = build_index(ctx.workspace)
        imp = impact(index, req.get("keywords", []))
        modules = index["modules"]
        routes = [dict(r, module=m) for m, i in modules.items() for r in i["routes"]]
        tables = sorted({t for i in modules.values() for t in i["tables"]})
        layers = {m: i["path"] for m, i in modules.items() if not m.startswith("tests")}
        data_flow = ("HTTP endpoint (Endpoints) -> LinkService -> ILinkRepository (SqliteLinkRepository) -> "
                     "SQLite; the redirect endpoint also records clicks via ClickAnalytics.BuildClick")
        output = {
            "module_count": len(modules),
            "source_loc": sum(i["loc"] for m, i in modules.items() if not m.startswith("tests")),
            "layers": layers,
            "routes": routes,
            "tables": tables,
            "import_graph": index["importers"],
            "impact": imp,
            "data_flow": data_flow,
            "baseline_tests": _baseline(ctx),
        }
        bt = output["baseline_tests"]
        decisions = [{
            "summary": f"Impact set: {', '.join(imp['impacted_modules']) or 'none'}",
            "rationale": f"Keyword matches {imp['direct_matches']} expanded through the reverse import graph; "
                         f"{len(imp['tests_to_run'])} test module(s) cover the impacted code.",
            "alternatives": [],
        }, {
            "summary": f"Baseline suite: {len(bt['passed_ids'])} passed, {len(bt['failed_ids'])} failed",
            "rationale": "Changes are judged against the pre-change behaviour; a red baseline would be "
                         "reported before any edits.",
            "alternatives": [],
        }]
        return AgentResult(output=output, summary=f"{len(imp['impacted_modules'])} impacted module(s), "
                           f"{len(routes)} route(s), tables={tables}", decisions=decisions)

    def run_llm(self, ctx: AgentContext) -> AgentResult:
        result = self.run_offline(ctx)
        data, tokens = self.ask_json(
            ctx, "Summarize architectural risks of changing this codebase for the requirement.",
            [as_data("analysis", str({k: result.output[k] for k in ("layers", "routes", "tables", "impact")})),
             *self.upstream_block(ctx, "requirements")],
            '{"risks": [str], "summary": str}', max_tokens=1500,
        )
        result.output["llm_risk_notes"] = data.get("risks", [])
        result.tokens = tokens
        return result
