"""Git-backed workspace the engine applies agent changes to.

- Every applied change set is snapshotted first, so a node's changes can be
  reverted precisely (rollback) even while other nodes run in parallel.
- Every successful node is committed, giving a reviewable history and
  checkpoints the whole run can be reset to.
- Paths are confined to the workspace root.
- Tool runs (build, tests) go through one place, per toolchain:
    pytest  Python projects (used by the engine's own tests)
    dotnet  .NET solutions: `dotnet build` and `dotnet test` with TRX results
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".pytest_cache", "*.db", "*.db-wal", "*.db-shm", ".git", "runs",
    "bin", "obj", "TestResults", ".vs", ".idea",
)
IGNORED_DIRS = {".git", "__pycache__", "bin", "obj", "TestResults", ".vs", ".idea", ".pytest_cache"}
GITIGNORE = "__pycache__/\n*.pyc\n*.db\n*.db-wal\n*.db-shm\n.pytest_cache/\nbin/\nobj/\nTestResults/\n.vs/\n"
TRX_NS = {"t": "http://microsoft.com/schemas/VisualStudio/TeamTest/2010"}
DOTNET_ENV = {"DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1", "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1"}


class WorkspaceError(Exception):
    pass


@dataclass
class Change:
    path: str
    content: str | None = None      # full new content; None with delete=True removes the file
    delete: bool = False
    find: str | None = None         # targeted edit: replace exactly one occurrence of find
    replace: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Change":
        return cls(**{k: d.get(k) for k in ("path", "content", "delete", "find", "replace") if k in d})

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, False)}


@dataclass
class TestReport:
    passed: bool
    total: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    duration_s: float = 0.0
    passed_ids: list[str] = field(default_factory=list)
    failed_ids: list[str] = field(default_factory=list)
    output_tail: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class BuildReport:
    passed: bool
    errors: list[str] = field(default_factory=list)
    output_tail: str = ""
    duration_s: float = 0.0


class Workspace:
    def __init__(self, root: Path, toolchain: str = "pytest") -> None:
        self.root = Path(root).resolve()
        self.toolchain = toolchain
        self._git_lock = threading.Lock()
        self._fs_lock = threading.Lock()
        # One build at a time: parallel `dotnet build`s in one tree clash on obj/ and bin/.
        self._tool_lock = threading.Lock()

    # -- setup -------------------------------------------------------------
    def init(self, seed: Path | None) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        if seed is not None:
            shutil.copytree(seed, self.root, dirs_exist_ok=True, ignore=COPY_IGNORE)
        (self.root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
        self._git("init", "-q")
        self._git("config", "user.email", "orchestrator@agentic.local")
        self._git("config", "user.name", "agentic-orchestrator")
        # Keep LF line endings on Windows: edits match exact text anchors.
        self._git("config", "core.autocrlf", "false")
        self._git("add", "-A")
        self._git("commit", "-q", "--allow-empty", "-m", "baseline")
        self._git("tag", "-f", "baseline")
        return self.head()

    # -- path safety -------------------------------------------------------
    def resolve(self, rel: str) -> Path:
        if not rel or os.path.isabs(rel):
            raise WorkspaceError(f"path must be relative: {rel!r}")
        target = (self.root / rel).resolve()
        if target != self.root and self.root not in target.parents:
            raise WorkspaceError(f"path escapes workspace: {rel!r}")
        if ".git" in Path(rel).parts:
            raise WorkspaceError(f"path targets git metadata: {rel!r}")
        return target

    # -- reads (agents get these) -----------------------------------------
    def read(self, rel: str) -> str | None:
        p = self.resolve(rel)
        return p.read_text(encoding="utf-8") if p.is_file() else None

    def exists(self, rel: str) -> bool:
        return self.resolve(rel).exists()

    def list_files(self, suffix: str | None = None) -> list[str]:
        out = []
        for p in sorted(self.root.rglob("*")):
            rel = p.relative_to(self.root).as_posix()
            if p.is_file() and not IGNORED_DIRS.intersection(p.relative_to(self.root).parts):
                if suffix is None or rel.endswith(suffix):
                    out.append(rel)
        return out

    # -- writes (engine only) ---------------------------------------------
    def render(self, changes: list[Change]) -> dict[str, str | None]:
        """Final content per path after applying changes in order, without writing.

        Several targeted edits to the same file compose sequentially. Raises
        WorkspaceError if any change is inapplicable (e.g. an edit anchor that
        does not match exactly once).
        """
        out: dict[str, str | None] = {}
        for ch in changes:
            current = out[ch.path] if ch.path in out else self.read(ch.path)
            if ch.delete:
                out[ch.path] = None
            elif ch.find is not None:
                if current is None:
                    raise WorkspaceError(f"edit target does not exist: {ch.path}")
                count = current.count(ch.find)
                if count != 1:
                    raise WorkspaceError(
                        f"edit anchor must match exactly once in {ch.path} (matched {count}): "
                        f"{ch.find[:80]!r}")
                out[ch.path] = current.replace(ch.find, ch.replace or "", 1)
            elif ch.content is None:
                raise WorkspaceError(f"change for {ch.path} has no content")
            else:
                out[ch.path] = ch.content
        return out

    def apply(self, changes: list[Change]) -> dict[str, str | None]:
        """Apply atomically; returns the pre-change snapshot used for rollback."""
        with self._fs_lock:
            new_contents = self.render(changes)
            snapshot = {path: self.read(path) for path in new_contents}
            try:
                for path, content in new_contents.items():
                    self._write(path, content)
            except Exception:
                self._restore(snapshot)
                raise
            return snapshot

    def restore(self, snapshot: dict[str, str | None]) -> None:
        with self._fs_lock:
            self._restore(snapshot)

    def _restore(self, snapshot: dict[str, str | None]) -> None:
        for path, content in snapshot.items():
            self._write(path, content)

    def _write(self, rel: str, content: str | None) -> None:
        p = self.resolve(rel)
        if content is None:
            if p.exists():
                p.unlink()
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="\n")  # LF on every OS

    # -- git ---------------------------------------------------------------
    def _git(self, *args: str) -> str:
        res = subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60
        )
        if res.returncode != 0:
            raise WorkspaceError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
        return res.stdout.strip()

    def head(self) -> str:
        with self._git_lock:
            return self._git("rev-parse", "HEAD")

    def commit(self, message: str, paths: list[str]) -> str | None:
        with self._git_lock:
            if not paths:
                return None
            self._git("add", "-A", "--", *paths)
            if not self._git("diff", "--cached", "--name-only"):
                return None
            self._git("commit", "-q", "-m", message)
            return self._git("rev-parse", "HEAD")

    def tag(self, name: str) -> None:
        with self._git_lock:
            self._git("tag", "-f", name)

    def reset_hard(self, ref: str) -> None:
        with self._git_lock:
            self._git("reset", "-q", "--hard", ref)
            self._git("clean", "-q", "-fd")

    def diff(self, base: str = "baseline", head: str = "HEAD", stat: bool = False) -> str:
        with self._git_lock:
            args = ["diff", "--stat" if stat else "--unified=3", base, head]
            return self._git(*args)

    def changed_files(self, base: str = "baseline") -> list[str]:
        with self._git_lock:
            out = self._git("diff", "--name-only", base, "HEAD")
        return [l for l in out.splitlines() if l]

    def rev_parse(self, ref: str) -> str:
        with self._git_lock:
            return self._git("rev-parse", ref)

    def is_ancestor(self, commit: str, of: str) -> bool:
        with self._git_lock:
            res = subprocess.run(["git", "merge-base", "--is-ancestor", commit, of], cwd=self.root,
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        return res.returncode == 0

    def read_at(self, ref: str, rel: str) -> str | None:
        self.resolve(rel)
        with self._git_lock:
            res = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=self.root,
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        return res.stdout if res.returncode == 0 else None

    def log(self) -> list[str]:
        with self._git_lock:
            return self._git("log", "--oneline", "--reverse").splitlines()

    # -- tools -------------------------------------------------------------
    def build(self, timeout_s: int = 600) -> BuildReport:
        """Compile the project. pytest projects have no build step (syntax is checked per file)."""
        if self.toolchain != "dotnet":
            return BuildReport(passed=True)
        with self._tool_lock:
            start = time.monotonic()
            res = self._run(["dotnet", "build", "-nologo", "-v", "q"], timeout_s)
            if res is None:
                return BuildReport(passed=False, output_tail=f"dotnet build timed out after {timeout_s}s")
            output = res.stdout + res.stderr
            errors = sorted({line.strip() for line in output.splitlines() if re.search(r": error [A-Z]+\d+", line)})
            return BuildReport(passed=res.returncode == 0, errors=errors[:30], output_tail=output[-3000:],
                               duration_s=round(time.monotonic() - start, 3))

    def run_tests(self, targets: list[str] | None = None, timeout_s: int = 600) -> TestReport:
        """Run tests in a subprocess. `targets` are test file paths; None runs the whole suite."""
        if self.toolchain == "dotnet":
            return self._run_dotnet_tests(targets, timeout_s)
        return self._run_pytest(targets, timeout_s)

    def _run(self, cmd: list[str], timeout_s: int, env: dict | None = None):
        if shutil.which(cmd[0]) is None:
            raise WorkspaceError(f"'{cmd[0]}' was not found on PATH (is the SDK installed?)")
        try:
            return subprocess.run(cmd, cwd=self.root, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout_s, env={**os.environ, **(env or {})})
        except subprocess.TimeoutExpired:
            return None

    def _report_dir(self) -> Path:
        report_dir = self.root.parent / "test-reports"
        report_dir.mkdir(exist_ok=True)
        return report_dir

    # pytest ---------------------------------------------------------------
    def _run_pytest(self, targets: list[str] | None, timeout_s: int) -> TestReport:
        xml_path = self._report_dir() / f"junit-{time.time_ns()}.xml"
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
               f"--junitxml={xml_path}", *(targets or [])]
        start = time.monotonic()
        res = self._run(cmd, timeout_s, {"PYTHONDONTWRITEBYTECODE": "1"})
        if res is None:
            return TestReport(passed=False, output_tail=f"pytest timed out after {timeout_s}s")
        report = TestReport(passed=res.returncode == 0, duration_s=round(time.monotonic() - start, 3),
                            output_tail=(res.stdout + res.stderr)[-3000:])
        if xml_path.exists():
            try:
                self._parse_junit(xml_path, report)
            except ET.ParseError:
                pass
        if res.returncode == 5:  # no tests collected
            report.passed = False
        return report

    @staticmethod
    def _parse_junit(xml_path: Path, report: TestReport) -> None:
        root = ET.parse(xml_path).getroot()
        for case in root.iter("testcase"):
            cls = case.get("classname", "")
            test_id = f"{cls.replace('.', '/')}.py::{case.get('name')}"
            report.total += 1
            if case.find("failure") is not None:
                report.failures += 1
                report.failed_ids.append(test_id)
            elif case.find("error") is not None:
                report.errors += 1
                report.failed_ids.append(test_id)
            elif case.find("skipped") is not None:
                report.skipped += 1
            else:
                report.passed_ids.append(test_id)

    # dotnet ---------------------------------------------------------------
    def _run_dotnet_tests(self, targets: list[str] | None, timeout_s: int) -> TestReport:
        name = f"results-{time.time_ns()}"
        results_dir = self._report_dir()
        cmd = ["dotnet", "test", "-nologo", "--results-directory", str(results_dir),
               "--logger", f"trx;LogFileName={name}.trx"]
        if targets:
            classes = [Path(t).stem for t in targets]
            cmd += ["--filter", "|".join(f"FullyQualifiedName~.{c}." for c in classes)]
        with self._tool_lock:
            start = time.monotonic()
            res = self._run(cmd, timeout_s, DOTNET_ENV)
            if res is None:
                return TestReport(passed=False, output_tail=f"dotnet test timed out after {timeout_s}s")
            output = res.stdout + res.stderr
            report = TestReport(passed=res.returncode == 0, duration_s=round(time.monotonic() - start, 3),
                                output_tail=output[-3000:])
        trx = results_dir / f"{name}.trx"
        if trx.exists():
            try:
                self._parse_trx(trx, report, self._test_files_by_class())
            except ET.ParseError:
                pass
        if report.total == 0:  # nothing ran (build failure or the filter matched no tests)
            report.passed = False
        return report

    def _test_files_by_class(self) -> dict[str, str]:
        return {Path(p).stem: p for p in self.list_files(".cs") if p.startswith("tests/")}

    @staticmethod
    def test_id(test_name: str, files_by_class: dict[str, str]) -> str:
        """'UrlShortener.Tests.ModelsTests.Accepts_x(url: "...")' -> 'tests/.../ModelsTests.cs::Accepts_x'."""
        parts = test_name.split("(", 1)[0].split(".")
        cls, method = (parts[-2], parts[-1]) if len(parts) >= 2 else ("", parts[-1])
        return f"{files_by_class.get(cls, cls + '.cs')}::{method}"

    @classmethod
    def _parse_trx(cls, trx: Path, report: TestReport, files_by_class: dict[str, str]) -> None:
        root = ET.parse(trx).getroot()
        for result in root.iter(f"{{{TRX_NS['t']}}}UnitTestResult"):
            test_id = cls.test_id(result.get("testName", ""), files_by_class)
            outcome = result.get("outcome", "")
            report.total += 1
            if outcome == "Passed":
                report.passed_ids.append(test_id)
            elif outcome in ("NotExecuted", "Inconclusive"):
                report.skipped += 1
            else:
                report.failures += 1
                report.failed_ids.append(test_id)
