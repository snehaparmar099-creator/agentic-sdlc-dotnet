"""Policy guardrails evaluated on every proposed change set BEFORE it is applied.

Severity
    block             change set is rejected; counts as a failed attempt
    require_approval  change set is held until a human approves it (change control)
    warn              recorded in the audit trail and the release report

Rules are data-driven through policies.json so security/compliance owners can
tune thresholds and allow-lists without touching engine code.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .graph import Node
from .workspace import Change, Workspace, WorkspaceError

BLOCK, APPROVAL, WARN = "block", "require_approval", "warn"

DEFAULT_POLICY_FILE = Path(__file__).with_name("policies.json")


@dataclass
class Finding:
    rule: str
    severity: str
    message: str
    path: str | None = None
    line: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


SECRET_PATTERNS = [
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private_key", re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("generic_secret", re.compile(
        r"(?i)\b(api[_-]?key|secret|password|token)\b\s*[:=]\s*['\"][A-Za-z0-9/+_\-]{16,}['\"]")),
]

DANGEROUS_PATTERNS = [
    ("eval", re.compile(r"(?<![\w.])eval\s*\(")),
    ("exec", re.compile(r"(?<![\w.])exec\s*\(")),
    ("os_system", re.compile(r"\bos\.system\s*\(")),
    ("shell_true", re.compile(r"shell\s*=\s*True")),
    ("pickle_loads", re.compile(r"\bpickle\.loads?\s*\(")),
    ("yaml_unsafe_load", re.compile(r"\byaml\.load\s*\((?![^)]*SafeLoader)")),
    ("tls_verify_disabled", re.compile(r"verify\s*=\s*False")),
    ("sql_string_formatting", re.compile(r"\.execute\(\s*f['\"]|\.execute\([^)]*%\s*\(|\.execute\([^)]*\.format\(")),
]

PII_LOG_PATTERN = re.compile(
    r"\b(log|logger|logging)\.(debug|info|warning|error|exception|critical)\(.*"
    r"(client\.host|remote_addr|authorization|password|x-forwarded-for)", re.IGNORECASE)

REQ_LINE = re.compile(r"^\s*([A-Za-z0-9_.\-\[\]]+)")

# C# equivalents of the Python rules above.
CSHARP_DANGEROUS_PATTERNS = [
    ("process_start", re.compile(r"\bProcess\.Start\s*\(")),
    ("binary_formatter", re.compile(r"\bBinaryFormatter\b")),
    ("tls_validation_disabled", re.compile(
        r"ServerCertificateCustomValidationCallback\s*=|DangerousAcceptAnyServerCertificateValidator")),
    ("sql_string_interpolation", re.compile(
        r"\$@?\"\s*(SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\b|CommandText\s*=\s*\$|"
        r"CommandText\s*=.*\+\s*\w|ExecuteSqlRaw\s*\(\s*\$", re.IGNORECASE)),
    ("dynamic_code", re.compile(r"\bCSharpScript\.|\bAssembly\.Load(From|File)?\s*\(")),
]

CSHARP_PII_LOG_PATTERN = re.compile(
    r"\b(_?logger|Log)\.(Log\w*|Information|Warning|Error|Debug|Critical)\s*\(.*"
    r"(RemoteIpAddress|Authorization|Password|X-Forwarded-For)", re.IGNORECASE)

CSHARP_MIGRATION = re.compile(r'new\s+Migration\(\s*(\d+)\s*,\s*@"((?:[^"]|"")*)"\s*\)', re.DOTALL)
PACKAGE_REFERENCE = re.compile(r'<PackageReference\s+Include="([^"]+)"', re.IGNORECASE)


class PolicyEngine:
    def __init__(self, config: dict | None = None) -> None:
        base = json.loads(DEFAULT_POLICY_FILE.read_text(encoding="utf-8"))
        base.update(config or {})
        self.cfg = base

    # -- entry point -------------------------------------------------------
    def evaluate(self, node: Node, changes: list[Change], ws: Workspace) -> list[Finding]:
        findings: list[Finding] = []
        for ch in changes:
            findings += self._scope(node, ch, ws)
        if any(f.severity == BLOCK for f in findings):
            return findings
        try:
            rendered = ws.render(changes)
        except WorkspaceError as exc:
            return findings + [Finding("change.inapplicable", BLOCK, str(exc))]
        lines_changed = 0
        for path, new in rendered.items():
            old = ws.read(path)
            lines_changed += _line_delta(old, new)
            if new is None:
                if self._is_test(path) and old is not None:
                    findings.append(Finding("change_control.test_deletion", APPROVAL,
                                            "deleting a test file requires approval", path))
                continue
            findings += self.scan(path, new, old)
        limit = self.cfg["max_lines_changed_without_approval"]
        if lines_changed > limit and node.stage == "implementation" and node.params.get("brownfield"):
            findings.append(Finding("change_control.change_size", APPROVAL,
                                    f"{lines_changed} lines changed exceeds {limit} in a brownfield change"))
        return findings

    def scan(self, path: str, new: str | None, old: str | None) -> list[Finding]:
        """Content rules only (no scope); used by the security review over the whole diff."""
        if new is None:
            return []
        out = self._content(path, new, old)
        if path.endswith(".py"):
            out += self._python(path, new, old)
        if path.endswith(".cs"):
            out += self._csharp(path, new, old)
        if Path(path).name == "requirements.txt":
            out += self._dependencies(path, old or "", new)
        if path.endswith(".csproj"):
            out += self._package_references(path, old or "", new)
        return out

    # -- rules -------------------------------------------------------------
    def _scope(self, node: Node, ch: Change, ws: Workspace) -> list[Finding]:
        try:
            ws.resolve(ch.path)
        except WorkspaceError as exc:
            return [Finding("scope.path_escape", BLOCK, str(exc), ch.path)]
        out = []
        for pat in self.cfg["protected_paths"]:
            if fnmatch.fnmatch(ch.path, pat):
                out.append(Finding("scope.protected_path", BLOCK, f"{ch.path} is protected ({pat})", ch.path))
        if not node.may_write(ch.path):
            out.append(Finding("scope.write_scope", BLOCK,
                               f"{node.id} may only write {node.write_scope}", ch.path))
        return out

    def _content(self, path: str, new: str, old: str | None) -> list[Finding]:
        out = []
        old_lines = set((old or "").splitlines())
        for lineno, line in enumerate(new.splitlines(), start=1):
            if line in old_lines:
                continue  # only judge lines this change introduces
            for name, pat in SECRET_PATTERNS:
                if pat.search(line):
                    out.append(Finding(f"secrets.{name}", BLOCK, "possible hard-coded secret", path, lineno))
        return out

    def _python(self, path: str, new: str, old: str | None) -> list[Finding]:
        out = []
        try:
            ast.parse(new)
        except SyntaxError as exc:
            return [Finding("quality.syntax_error", BLOCK, f"syntax error: {exc.msg}", path, exc.lineno)]
        is_test = self._is_test(path)
        old_lines = set((old or "").splitlines())
        for lineno, line in enumerate(new.splitlines(), start=1):
            if line in old_lines or line.lstrip().startswith("#"):
                continue
            if not is_test:
                for name, pat in DANGEROUS_PATTERNS:
                    if pat.search(line):
                        out.append(Finding(f"security.{name}", BLOCK, f"disallowed pattern: {name}", path, lineno))
            if PII_LOG_PATTERN.search(line):
                out.append(Finding("compliance.pii_in_logs", BLOCK,
                                   "logging of client IPs or credentials is not allowed", path, lineno))
        if Path(path).name == "migrations.py" and old is not None:
            out += self._migrations(path, old, new)
        return out

    def _migrations(self, path: str, old: str, new: str) -> list[Finding]:
        before, after = _literal(old, "MIGRATIONS"), _literal(new, "MIGRATIONS")
        if before is None or after is None:
            return [Finding("change_control.migration_unparseable", APPROVAL,
                            "could not verify MIGRATIONS is append-only", path)]
        if [tuple(m) for m in after[: len(before)]] != [tuple(m) for m in before]:
            return [Finding("change_control.migration_rewrite", BLOCK,
                            "applied migrations are immutable; add a new version instead", path)]
        if len(after) > len(before):
            versions = [m[0] for m in after[len(before):]]
            return [Finding("change_control.schema_change", APPROVAL,
                            f"new schema migration(s) {versions} change production data", path)]
        return []

    def _csharp(self, path: str, new: str, old: str | None) -> list[Finding]:
        """C# rules. Syntax and types are checked by the `compiles` gate (dotnet build)."""
        out = []
        is_test = self._is_test(path)
        old_lines = set((old or "").splitlines())
        for lineno, line in enumerate(new.splitlines(), start=1):
            if line in old_lines or line.lstrip().startswith("//"):
                continue
            if not is_test:
                for name, pat in CSHARP_DANGEROUS_PATTERNS:
                    if pat.search(line):
                        out.append(Finding(f"security.{name}", BLOCK, f"disallowed pattern: {name}", path, lineno))
            if CSHARP_PII_LOG_PATTERN.search(line):
                out.append(Finding("compliance.pii_in_logs", BLOCK,
                                   "logging of client IPs or credentials is not allowed", path, lineno))
        if Path(path).name == "Migrations.cs" and old is not None:
            out += self._csharp_migrations(path, old, new)
        return out

    def _csharp_migrations(self, path: str, old: str, new: str) -> list[Finding]:
        before = {int(v): sql for v, sql in CSHARP_MIGRATION.findall(old)}
        after = {int(v): sql for v, sql in CSHARP_MIGRATION.findall(new)}
        if not before or not after:
            return [Finding("change_control.migration_unparseable", APPROVAL,
                            "could not verify migrations are append-only", path)]
        if any(after.get(v) != sql for v, sql in before.items()):
            return [Finding("change_control.migration_rewrite", BLOCK,
                            "applied migrations are immutable; add a new version instead", path)]
        added = sorted(set(after) - set(before))
        if added:
            return [Finding("change_control.schema_change", APPROVAL,
                            f"new schema migration(s) {added} change production data", path)]
        return []

    def _package_references(self, path: str, old: str, new: str) -> list[Finding]:
        added = {p.lower() for p in PACKAGE_REFERENCE.findall(new)} - {p.lower() for p in PACKAGE_REFERENCE.findall(old)}
        return self._judge_dependencies(path, added)

    def _dependencies(self, path: str, old: str, new: str) -> list[Finding]:
        def names(text: str) -> set[str]:
            out = set()
            for line in text.splitlines():
                line = line.split("#")[0].strip()
                m = REQ_LINE.match(line)
                if m:
                    out.add(re.split(r"[\[<>=!~]", m.group(1))[0].lower())
            return out

        return self._judge_dependencies(path, names(new) - names(old))

    def _judge_dependencies(self, path: str, added: set[str]) -> list[Finding]:
        allow = {d.lower() for d in self.cfg["dependency_allowlist"]}
        deny = {d.lower() for d in self.cfg["dependency_denylist"]}
        out = []
        for dep in sorted(added):
            if dep in deny:
                out.append(Finding("supply_chain.denied_dependency", BLOCK, f"{dep} is on the deny-list", path))
            elif dep not in allow:
                out.append(Finding("supply_chain.new_dependency", APPROVAL,
                                   f"new third-party dependency '{dep}' needs review", path))
        return out

    def _is_test(self, path: str) -> bool:
        name = Path(path).name
        return (path.startswith("tests/") or name.startswith("test_") or name == "conftest.py"
                or name.endswith("Tests.cs"))


def _literal(source: str, name: str):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id == name for t in targets) and node.value is not None:
            try:
                return ast.literal_eval(node.value)
            except ValueError:
                return None
    return None


def _line_delta(old: str | None, new: str | None) -> int:
    a, b = (old or "").splitlines(), (new or "").splitlines()
    sa, sb = set(a), set(b)
    return sum(1 for l in b if l not in sa) + sum(1 for l in a if l not in sb)


def summarize(findings: list[Finding]) -> dict[str, int]:
    out = {BLOCK: 0, APPROVAL: 0, WARN: 0}
    for f in findings:
        out[f.severity] += 1
    return out
