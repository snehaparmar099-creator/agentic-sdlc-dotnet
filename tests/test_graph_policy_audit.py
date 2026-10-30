import json

import pytest

from agentic.audit import AuditLog, verify
from agentic.graph import GraphError, Node, RetryPolicy, WorkflowGraph
from agentic.policy import APPROVAL, BLOCK, PolicyEngine
from agentic.workspace import Change, Workspace, WorkspaceError


# ---------------------------------------------------------------- graph
def g(*nodes):
    return WorkflowGraph(list(nodes))


def test_topological_order_and_descendants():
    graph = g(Node("a", "s", "x"), Node("b", "s", "x", deps=["a"]), Node("c", "s", "x", deps=["a"]),
              Node("d", "s", "x", deps=["b", "c"]))
    order = graph.topo_order()
    assert order.index("a") < order.index("b") < order.index("d")
    assert graph.descendants("a") == ["b", "c", "d"]
    assert graph.ancestors("d") == {"a", "b", "c"}


def test_cycle_detected():
    with pytest.raises(GraphError, match="cycle"):
        g(Node("a", "s", "x", deps=["b"]), Node("b", "s", "x", deps=["a"])).topo_order()


def test_unknown_dependency_detected():
    with pytest.raises(GraphError, match="unknown"):
        g(Node("a", "s", "x", deps=["ghost"])).topo_order()


def test_parallel_nodes_with_overlapping_scopes_rejected():
    graph = g(Node("a", "s", "x", write_scope=["pkg/*.py"]), Node("b", "s", "x", write_scope=["pkg/api.py"]))
    with pytest.raises(GraphError, match="overlapping"):
        graph.validate()


def test_sequential_nodes_may_share_scope():
    g(Node("a", "s", "x", write_scope=["pkg/api.py"]),
      Node("b", "s", "x", deps=["a"], write_scope=["pkg/api.py"])).validate()


def test_backoff_is_bounded_exponential():
    rp = RetryPolicy(max_attempts=5, backoff_base_s=0.1, backoff_max_s=0.3)
    assert [rp.delay(i) for i in range(1, 6)] == [0.0, 0.1, 0.2, 0.3, 0.3]


# ---------------------------------------------------------------- policy
@pytest.fixture
def ws(tmp_path):
    w = Workspace(tmp_path / "ws")
    w.init(None)
    return w


def node(scope=("*",), **params):
    return Node("impl.t", "implementation", "implementer", write_scope=list(scope), params=params)


def rules(findings, severity=None):
    return {f.rule for f in findings if severity is None or f.severity == severity}


@pytest.mark.parametrize("code,rule", [
    ("x = eval(user)\n", "security.eval"),
    ("import os\nos.system(cmd)\n", "security.os_system"),
    ("subprocess.run(cmd, shell=True)\n", "security.shell_true"),
    ("requests.get(u, verify=False)\n", "security.tls_verify_disabled"),
    ("conn.execute(f\"SELECT * FROM t WHERE id={x}\")\n", "security.sql_string_formatting"),
    ("API_KEY = 'abcd1234abcd1234abcd'\n", "secrets.generic_secret"),
    ("k = 'AKIAABCDEFGHIJKLMNOP'\n", "secrets.aws_access_key"),
    ("log.info('client %s', request.client.host)\n", "compliance.pii_in_logs"),
    ("def broken(:\n", "quality.syntax_error"),
])
def test_blocking_rules(ws, code, rule):
    assert rule in rules(PolicyEngine().evaluate(node(), [Change("app/m.py", code)], ws), BLOCK)


@pytest.mark.parametrize("code,rule", [
    ('cmd.CommandText = $"SELECT * FROM links WHERE code = \'{code}\'";', "security.sql_string_interpolation"),
    ('var sql = $"DELETE FROM links WHERE code = {code}";', "security.sql_string_interpolation"),
    ('Process.Start("cmd.exe", args);', "security.process_start"),
    ('var f = new BinaryFormatter();', "security.binary_formatter"),
    ('handler.ServerCertificateCustomValidationCallback = (_, _, _, _) => true;', "security.tls_validation_disabled"),
    ('_logger.LogInformation("client {Ip}", context.Connection.RemoteIpAddress);', "compliance.pii_in_logs"),
])
def test_csharp_blocking_rules(ws, code, rule):
    assert rule in rules(PolicyEngine().evaluate(node(), [Change("src/App/Thing.cs", code + "\n")], ws), BLOCK)


def test_csharp_parameterized_sql_is_allowed(ws):
    code = 'using var c = Command("SELECT url FROM links WHERE code = $code", ("$code", code));\n'
    assert not rules(PolicyEngine().evaluate(node(), [Change("src/App/Repo.cs", code)], ws), BLOCK)


CS_MIG = 'new Migration(1, @"CREATE TABLE a (x INT)"),\n'


def test_csharp_migrations_are_append_only(ws):
    ws.apply([Change("src/App/Migrations.cs", CS_MIG)])
    pe = PolicyEngine()
    added = CS_MIG + 'new Migration(2, @"ALTER TABLE a ADD COLUMN y INT"),\n'
    assert "change_control.schema_change" in rules(
        pe.evaluate(node(), [Change("src/App/Migrations.cs", added)], ws), APPROVAL)
    rewritten = 'new Migration(1, @"CREATE TABLE a (x TEXT)"),\n'
    assert "change_control.migration_rewrite" in rules(
        pe.evaluate(node(), [Change("src/App/Migrations.cs", rewritten)], ws), BLOCK)


def test_new_nuget_package_needs_approval(ws):
    proj = '<Project><ItemGroup>\n<PackageReference Include="Microsoft.Data.Sqlite" Version="10.0.0" />\n</ItemGroup></Project>\n'
    ws.apply([Change("src/App/App.csproj", proj)])
    pe = PolicyEngine()
    redis = proj.replace("</ItemGroup>", '<PackageReference Include="StackExchange.Redis" Version="2.8.16" />\n</ItemGroup>')
    assert "supply_chain.new_dependency" in rules(pe.evaluate(node(), [Change("src/App/App.csproj", redis)], ws), APPROVAL)
    denied = proj.replace("</ItemGroup>", '<PackageReference Include="System.Runtime.Serialization.Formatters" Version="1.0.0" />\n</ItemGroup>')
    assert "supply_chain.denied_dependency" in rules(pe.evaluate(node(), [Change("src/App/App.csproj", denied)], ws), BLOCK)


def test_dangerous_patterns_allowed_in_tests_but_secrets_are_not(ws):
    f = PolicyEngine().evaluate(node(), [Change("tests/test_x.py", "assert eval('1') == 1\n")], ws)
    assert not rules(f, BLOCK)
    f = PolicyEngine().evaluate(node(), [Change("tests/test_x.py", "TOKEN = 'abcd1234abcd1234abcd'\n")], ws)
    assert "secrets.generic_secret" in rules(f, BLOCK)


def test_scope_rules(ws):
    pe = PolicyEngine()
    assert "scope.write_scope" in rules(pe.evaluate(node(["app/*"]), [Change("other.py", "x=1\n")], ws), BLOCK)
    assert "scope.path_escape" in rules(pe.evaluate(node(), [Change("../evil.py", "x=1\n")], ws), BLOCK)
    assert "scope.protected_path" in rules(pe.evaluate(node(), [Change(".github/w.yml", "x\n")], ws), BLOCK)


def test_dependency_rules(ws):
    ws.apply([Change("requirements.txt", "pytest>=8\n")])
    pe = PolicyEngine()
    f = pe.evaluate(node(), [Change("requirements.txt", "pytest>=8\nredis>=5\n")], ws)
    assert "supply_chain.new_dependency" in rules(f, APPROVAL)
    f = pe.evaluate(node(), [Change("requirements.txt", "pytest>=8\npickle5\n")], ws)
    assert "supply_chain.denied_dependency" in rules(f, BLOCK)
    f = pe.evaluate(node(), [Change("requirements.txt", "pytest>=8\nstarlette\n")], ws)
    assert not f


MIG = 'MIGRATIONS = [(1, "CREATE TABLE a (x INT)")]\n'


def test_migrations_are_append_only(ws):
    ws.apply([Change("pkg/migrations.py", MIG)])
    pe = PolicyEngine()
    added = MIG.replace(")]", '), (2, "ALTER TABLE a ADD COLUMN y INT")]')
    assert "change_control.schema_change" in rules(pe.evaluate(node(), [Change("pkg/migrations.py", added)], ws), APPROVAL)
    rewritten = 'MIGRATIONS = [(1, "CREATE TABLE a (x TEXT)")]\n'
    assert "change_control.migration_rewrite" in rules(
        pe.evaluate(node(), [Change("pkg/migrations.py", rewritten)], ws), BLOCK)


def test_only_introduced_lines_are_judged(ws):
    ws.apply([Change("app/legacy.py", "x = eval('1')  # pre-existing\n")])
    f = PolicyEngine().evaluate(node(), [Change("app/legacy.py", find="# pre-existing", replace="# legacy")], ws)
    assert "security.eval" in rules(f)  # the edited line is new content
    f = PolicyEngine().evaluate(node(), [Change("app/legacy.py", "x = eval('1')  # pre-existing\ny = 2\n")], ws)
    assert not rules(f, BLOCK)


def test_inapplicable_edit_blocks(ws):
    f = PolicyEngine().evaluate(node(), [Change("missing.py", find="a", replace="b")], ws)
    assert "change.inapplicable" in rules(f, BLOCK)


# ---------------------------------------------------------------- workspace
def test_edits_compose_and_rollback_restores_exactly(ws):
    ws.apply([Change("m.py", "a = 1\nb = 2\n")])
    snap = ws.apply([Change("m.py", find="a = 1", replace="a = 10"), Change("m.py", find="b = 2", replace="b = 20"),
                     Change("new.py", "n = 1\n")])
    assert ws.read("m.py") == "a = 10\nb = 20\n"
    ws.restore(snap)
    assert ws.read("m.py") == "a = 1\nb = 2\n" and not ws.exists("new.py")


def test_ambiguous_anchor_rejected(ws):
    ws.apply([Change("m.py", "x\nx\n")])
    with pytest.raises(WorkspaceError, match="exactly once"):
        ws.render([Change("m.py", find="x", replace="y")])


def test_git_checkpoints_and_reset(ws):
    ws.apply([Change("a.txt", "1\n")])
    ws.commit("add a", ["a.txt"])
    assert ws.changed_files("baseline") == ["a.txt"]
    ws.reset_hard("baseline")
    assert not ws.exists("a.txt")


# ---------------------------------------------------------------- audit
def test_audit_chain_detects_tampering(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, "run")
    for i in range(5):
        log.emit("event", i=i)
    assert verify(path)[0]

    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[2])
    rec["data"]["i"] = 99
    path.write_text("\n".join(lines[:2] + [json.dumps(rec)] + lines[3:]) + "\n", encoding="utf-8")
    ok, msg = verify(path)
    assert not ok and "modified" in msg

    path.write_text("\n".join(lines[:2] + lines[3:]) + "\n", encoding="utf-8")
    assert not verify(path)[0]


def test_audit_log_resumes_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    AuditLog(path, "run").emit("a")
    AuditLog(path, "run").emit("b")
    assert verify(path) == (True, "audit chain intact")
