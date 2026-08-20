"""The two-tool contract, the unit text, and the CLI's refusals."""

from __future__ import annotations

import os
import queue
import threading
import time

import pytest

from coderag import cli, config, index, registry, server, store, systemd, tools, watch


@pytest.fixture(autouse=True)
def fresh_queue(monkeypatch):
    monkeypatch.setattr(index, "_queue", queue.Queue())
    monkeypatch.setattr(index, "_state", index.State())


# ------------------------------------------------------------ the two actions


async def test_the_surface_is_exactly_two_tools():
    """Not "two tools with many modes". Every third tool the old engine grew
    was an operator concern that leaked into the protocol."""
    listed = await tools.mcp.list_tools()
    assert sorted(t.name for t in listed) == ["index", "search"]


async def test_both_tools_carry_a_schema_a_caller_can_read():
    for tool in await tools.mcp.list_tools():
        assert tool.description
        assert "root" in tool.input_schema["properties"]


async def test_the_workspace_pin_is_not_a_parameter_the_model_can_write():
    """The whole reason the boundary comes from `roots` rather than an argument.
    A pin in the schema is a pin the model supplies, and a pin the model supplies
    is the same string it already gets wrong -- and worse, it would read as one."""
    for tool in await tools.mcp.list_tools():
        props = tool.input_schema["properties"]
        # `root` is the discriminator: it is the parameter that must stay, so a
        # schema that lost everything cannot satisfy this by being empty.
        assert "root" in props and "pinned" not in props, props


def test_the_server_instructions_name_both_actions():
    """Phase 4's hermes serves this string verbatim, so it is the only place
    the doctrine lives -- an empty one silently blanks the host prompt."""
    assert "index" in tools.INSTRUCTIONS and "search" in tools.INSTRUCTIONS
    assert tools.mcp.instructions == tools.INSTRUCTIONS


def test_index_returns_before_the_work_does(tmp_path, monkeypatch, pin):
    monkeypatch.setattr(watch, "start", lambda: None)
    monkeypatch.setattr(index, "start_worker", lambda: None)
    project = tmp_path / "p"
    project.mkdir()

    started = time.perf_counter()
    out = tools.index_project(pin(project), str(project))

    assert time.perf_counter() - started < 1.0, "a return that blocks is a failure"
    assert out["state"] == "indexing" and out["queue_depth"] >= 1


def test_index_on_a_path_that_is_not_a_directory_says_so(tmp_path, pin):
    file = tmp_path / "not-a-dir"
    file.write_text("x")
    assert "error" in tools.index_project(pin(file), str(file))


def test_a_config_that_cannot_parse_leaves_no_row_behind(tmp_path, monkeypatch, pin):
    """The row was written before the config that rejects it, so a project that
    can never index still landed one -- and reconcile retries it at every start,
    logging a traceback, forever."""
    monkeypatch.setattr(watch, "start", lambda: None)
    monkeypatch.setattr(index, "start_worker", lambda: None)
    project = tmp_path / "typo"
    project.mkdir()
    (project / config.PROJECT_CONFIG_NAME).write_text('index:\n  excludes: ["x/*"]\n')

    out = tools.index_project(pin(project), str(project))

    assert "excludes" in out["error"], out
    assert registry.get(project) is None, "a project that cannot index kept a row"


def test_unflagging_releases_the_row_and_never_the_index_directory(tmp_path, monkeypatch, pin):
    """Both fleet-wide index wipes in this engine's history came from something
    that deleted store directories on a computed set."""
    monkeypatch.setattr(watch, "start", lambda: None)
    monkeypatch.setattr(index, "start_worker", lambda: None)
    project = tmp_path / "p"
    project.mkdir()
    registry.claim(project, direct=True)
    store.connect(project)
    db = config.index_path(project)
    assert db.exists()

    out = tools.index_project(pin(project), str(project), enabled=False)

    assert out["enabled"] is False
    assert db.exists(), "unflagging must never delete a store"


def test_a_project_whose_directory_is_gone_can_still_be_turned_off(tmp_path, monkeypatch, pin):
    """The row an operator most needs to unflag is the one that stopped
    existing. While `is_dir` gated the whole tool, two such rows sat enabled
    across every restart: reconcile retried them, `doctor` named them, and
    nothing on the surface could act on them."""
    monkeypatch.setattr(watch, "start", lambda: None)
    monkeypatch.setattr(index, "start_worker", lambda: None)
    project = tmp_path / "gone"
    project.mkdir()
    registry.claim(project, direct=True)
    project.rmdir()

    out = tools.index_project(pin(project), str(project), enabled=False)

    assert out["enabled"] is False, out
    assert project not in [e.path for e in registry.enabled_projects()]


def test_unflagging_a_root_re_walks_the_members_it_released(tmp_path, monkeypatch, pin):
    """The asymmetry that made a root's excludes outlive the root.

    Joining a root narrows a member and submits it; leaving one widens it and
    submitted nothing, so the member went on answering under excludes nobody
    was holding any more -- until the next unrelated write to it, or forever.

    The member is claimed directly on purpose: that is the row that survives the
    release and goes on being searched, and `unregister` used to report only the
    rows it deleted -- so the one project that needed re-walking was the one
    project it never named.
    """
    monkeypatch.setattr(watch, "start", lambda: None)
    monkeypatch.setattr(index, "start_worker", lambda: None)
    member, root = tmp_path / "m", tmp_path / "r"
    (member / "src").mkdir(parents=True)
    root.mkdir()
    (root / "linked").symlink_to(member, target_is_directory=True)
    tools.index_project(pin(member), str(member))
    tools.index_project(pin(root), str(root))
    while not index._queue.empty():
        index._queue.get_nowait()

    out = tools.index_project(pin(root), str(root), enabled=False)

    assert str(member) in out["members_released"], out
    submitted = []
    while not index._queue.empty():
        submitted.append(index._queue.get_nowait().project)
    assert submitted == [member], submitted


def test_a_search_error_is_returned_as_data_not_raised(tmp_path, pin):
    """An agent can act on an error that names what to call next; a transport
    failure is just a dead turn."""
    out = tools.search_code("anything", pin(tmp_path), str(tmp_path))
    assert "error" in out and out["results"] == []


def test_an_unknown_mode_reaches_the_caller_as_an_error(tmp_path, pin):
    assert "error" in tools.search_code("q", pin(tmp_path), str(tmp_path), mode="fuzzy")


# ------------------------------------------------------------------ the server


def test_the_app_serves_two_routes_and_no_dashboard():
    paths = {getattr(r, "path", None) for r in server.build_app().routes}
    assert "/mcp" in paths and "/healthz" in paths
    assert not {p for p in paths if p and p.startswith("/api")}


def test_mcp_answers_a_request_and_not_only_the_route_table(monkeypatch):
    """`/mcp` existing in `routes` is not `/mcp` working.

    Ours replaced the SDK's lifespan instead of nesting inside it, so the
    session manager's task group was never entered and every call answered 500
    while `/healthz` -- a plain route -- stayed green. The route-table test
    above passed throughout.
    """
    from starlette.testclient import TestClient

    monkeypatch.setattr(config, "RECONCILE_ON_START", False)
    monkeypatch.setattr(index, "start_worker", lambda: None)
    monkeypatch.setattr(watch, "start", lambda: None)

    with TestClient(server.build_app()) as client:
        got = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )

    assert got.status_code != 500, got.text
    assert "Task group is not initialized" not in got.text


async def test_healthz_reports_what_doctor_would_ask():
    body = await server.healthz(None)
    assert body.status_code == 200


@pytest.mark.parametrize("on,expected", [(True, 1), (False, 0)])
async def test_the_startup_sweep_is_the_difference_between_serving_and_indexing(
    monkeypatch, on, expected
):
    """Starting the daemon and starting a fleet-wide index used to be one act.
    They are two intentions, and the second is an overnight run on one card --
    so a live suite or a bake-off could not have the daemon up without it."""
    called = []
    monkeypatch.setattr(config, "RECONCILE_ON_START", on)
    monkeypatch.setattr(index, "reconcile_all", lambda: called.append(1) or 1)
    monkeypatch.setattr(index, "start_worker", lambda: None)
    monkeypatch.setattr(watch, "start", lambda: None)
    monkeypatch.setattr(server, "_notify", lambda _msg: None)

    async with server.lifespan(None):
        pass
    assert len(called) == expected


def test_the_notifier_is_silent_without_a_socket(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    server._notify("READY=1")  # must not raise outside systemd


# ------------------------------------------------------------------- the unit


def test_the_unit_carries_the_two_numbers_that_were_bought_with_outages():
    text = systemd.unit_text("/usr/bin/python3")
    assert "LimitNOFILE=65536" in text, "150 repos x 3 fds under WAL against a 1024 default"
    assert "CODERAG_INDEX_TEMP_C=84" in text, (
        "the governor defaults to off, and the unit is the unattended overnight run"
    )
    assert "Restart=on-failure" in text
    assert "Type=notify" in text and "WatchdogSec=" in text


def test_on_failure_sits_in_unit_where_systemd_reads_it():
    """systemd logs "Unknown key name" for a `[Service]` OnFailure and carries
    on, so the misplaced one ran for weeks as an alert that never fired and
    never said it had not."""
    text = systemd.unit_text("/usr/bin/python3")
    unit, service = text.split("\n[Service]\n", 1)
    assert "OnFailure=" in unit and "OnFailure=" not in service


def test_the_alert_waits_before_it_pages():
    """A five-second restart is not an incident, and an alert that fires on
    every one of them gets muted -- after which the real outage is silent too."""
    text = systemd.alert_text()
    assert "sleep 8" in text and "is-active" in text


def test_install_writes_both_units_without_touching_systemd(tmp_path, monkeypatch):
    monkeypatch.setattr(systemd, "UNIT_DIR", tmp_path / "units")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    unit = systemd.install(enable=False)

    assert unit.exists() and (tmp_path / "units" / systemd.ALERT_NAME).exists()


# --------------------------------------------------------------------- the cli


def test_every_operator_action_stayed_off_the_mcp_surface():
    """The contraction is the decision; this is what holds it."""
    parser = build_choices()
    assert {"doctor", "list", "serve", "install-systemd", "bridge-stdio"} <= parser
    assert {t.name for t in _tool_names()} == {"index", "search"}


def build_choices() -> set[str]:
    action = next(a for a in cli.build_parser()._actions if a.choices and a.dest == "command")
    return set(action.choices)


def _tool_names():
    import asyncio

    return asyncio.run(tools.mcp.list_tools())


def test_search_from_the_cli_reports_the_error_rather_than_a_traceback(tmp_path, capsys):
    assert cli.main(["search", "anything", str(tmp_path)]) == 2
    assert "error" in capsys.readouterr().err


def test_doctor_names_a_store_no_row_claims(tmp_path, monkeypatch, capsys):
    """The row-driven half starts from a row, so a store whose row is gone was
    the one class it could never reach: 143 dirs, 0.46 GiB, under a `doctor`
    that had been reporting 0 problems."""
    monkeypatch.setattr(cli.gpu, "providers", lambda: ["CUDAExecutionProvider"])
    monkeypatch.setattr(cli.gpu, "free_vram_bytes", lambda: 0)
    kept = tmp_path / "kept"
    kept.mkdir()
    registry.claim(kept, direct=True)
    registry.set_enabled(kept, False)
    config.index_path(kept).parent.mkdir(parents=True)
    orphan = config.INDEX_DIR / "gone-0123456789abcdef"
    orphan.mkdir(parents=True)
    (orphan / "index.db").write_bytes(b"x" * 2048)

    code = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert code == 1, out
    assert "UNCLAIMED gone-0123456789abcdef" in out
    # A disabled row keeps its store by policy, so reporting it would make the
    # walk fire on all 88 of them.
    assert "kept" not in out

    # Idle, so prune is allowed to take it: a store still being written to is a
    # row-less job finishing rather than garbage.
    os.utime(orphan / "index.db", (0, 0))
    os.utime(orphan, (0, 0))
    assert cli.main(["doctor", "--prune"]) == 0
    assert "pruned gone-0123456789abcdef" in capsys.readouterr().out
    assert not orphan.exists()
    assert config.index_path(kept).parent.exists()


def test_prune_keeps_an_unclaimed_store_something_is_still_writing_to(
    tmp_path, monkeypatch, capsys
):
    """The one gap the registry lock cannot close: a job queued before its row
    was dropped still creates its directory and indexes into it."""
    monkeypatch.setattr(cli.gpu, "providers", lambda: ["CUDAExecutionProvider"])
    monkeypatch.setattr(cli.gpu, "free_vram_bytes", lambda: 0)
    busy = config.INDEX_DIR / "rowless-0123456789abcdef"
    busy.mkdir(parents=True)
    (busy / "index.db").write_bytes(b"x" * 2048)

    assert cli.main(["doctor", "--prune"]) == 1
    out = capsys.readouterr().out
    assert "BUSY rowless-0123456789abcdef" in out
    assert (busy / "index.db").exists()


def _raise_missing(*_args, **_kwargs):
    raise FileNotFoundError


def test_prune_spares_a_store_claimed_while_it_was_looking(tmp_path, monkeypatch, capsys):
    """The rows and the glob have to come from one view of the registry.

    Reading rows first and globbing after enumerates a project claimed in
    between as unclaimed, and the rmtree then lands on a store the daemon has
    open: on Linux its writes keep succeeding into the unlinked inode, commit()
    returns clean, and the next connect creates an empty database.
    """
    monkeypatch.setattr(cli.gpu, "providers", lambda: ["CUDAExecutionProvider"])
    monkeypatch.setattr(cli.gpu, "free_vram_bytes", lambda: 0)
    # The row-driven half also resolves store paths; this test is about the walk.
    monkeypatch.setattr(cli.store, "connect", _raise_missing)
    incumbent = tmp_path / "incumbent"
    incumbent.mkdir()
    registry.claim(incumbent, direct=True)
    config.index_path(incumbent).parent.mkdir(parents=True)

    latecomer = tmp_path / "latecomer"
    latecomer.mkdir()
    store_dir = config.index_path(latecomer).parent

    looking, claimed = threading.Event(), threading.Event()

    def claim_late():
        looking.wait(10)
        registry.claim(latecomer, direct=True)
        store_dir.mkdir(parents=True, exist_ok=True)
        (store_dir / "index.db").write_bytes(b"a store with a daemon writing to it")
        claimed.set()

    real_index_path = config.index_path

    def index_path_once(path):
        """Widen the window between the rows and the glob to the whole claim."""
        monkeypatch.setattr(config, "index_path", real_index_path)
        looking.set()
        claimed.wait(2)
        return real_index_path(path)

    monkeypatch.setattr(config, "index_path", index_path_once)
    claimer = threading.Thread(target=claim_late)
    claimer.start()
    try:
        cli.main(["doctor", "--prune"])
    finally:
        claimer.join(20)

    out = capsys.readouterr().out
    assert (store_dir / "index.db").read_bytes().endswith(b"writing to it"), out
    assert f"pruned {store_dir.name}" not in out, out


def test_the_full_flag_is_the_only_thing_that_empties_a_store():
    idx = next(
        a
        for a in cli.build_parser()._subparsers._group_actions[0].choices["index"]._actions
        if a.dest == "full"
    )
    assert idx.help and "rebuild" in idx.help
