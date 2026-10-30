"""Shared fixtures.

`stub_dotnet` replaces the .NET toolchain with a stand-in so the full scenario flows
(planning, parallel tasks, policy, approvals, re-planning, rollback, release) can be tested
on machines without the .NET SDK. The stand-in "build" checks bracket balance of every C#
file; "test" discovers xUnit [Fact]/[Theory] methods and reports them as passed. Real
compilation and test execution are covered by the tests marked `requires_dotnet`.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from agentic import pipeline
from agentic.workspace import BuildReport, TestReport, Workspace

XUNIT_TEST = re.compile(r"\[(?:Fact|Theory)\][\s\S]*?public\s+(?:async\s+Task|void)\s+(\w+)\s*\(")
STRING_OR_COMMENT = re.compile(r'@"(?:[^"]|"")*"|\$?"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])\'|//[^\n]*|/\*[\s\S]*?\*/')

HAVE_DOTNET = shutil.which("dotnet") is not None
requires_dotnet = pytest.mark.skipif(not HAVE_DOTNET, reason=".NET SDK not installed")


def _balanced(source: str) -> bool:
    code = STRING_OR_COMMENT.sub("", source)
    pairs, stack = {")": "(", "]": "[", "}": "{"}, []
    for ch in code:
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack or stack.pop() != pairs[ch]:
                return False
    return not stack


def _stub_build(self, timeout_s=600):
    bad = [f for f in self.list_files(".cs") if not _balanced(self.read(f) or "")]
    return BuildReport(passed=not bad, errors=[f"{f}: unbalanced brackets" for f in bad])


def _stub_tests(self, targets=None, timeout_s=600):
    build = _stub_build(self)
    if not build.passed:
        return TestReport(passed=False, output_tail="\n".join(build.errors))
    classes = {Path(t).stem for t in targets} if targets else None
    report = TestReport(passed=True)
    for f in self.list_files(".cs"):
        if f.startswith("tests/") and (classes is None or Path(f).stem in classes):
            for method in XUNIT_TEST.findall(self.read(f) or ""):
                report.total += 1
                report.passed_ids.append(f"{f}::{method}")
    report.passed = report.total > 0
    return report


@pytest.fixture
def stub_dotnet(monkeypatch):
    monkeypatch.setattr(Workspace, "build", _stub_build)
    monkeypatch.setattr(Workspace, "run_tests", _stub_tests)
    monkeypatch.setattr(pipeline.shutil, "which", lambda name: f"/usr/bin/{name}")
