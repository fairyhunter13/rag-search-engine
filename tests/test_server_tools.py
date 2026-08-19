"""The two-tool contract, the unit text, and the CLI's refusals."""

from __future__ import annotations

import queue
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


def test_the_server_instructions_name_both_actions():
    """Phase 4's hermes serves this string verbatim, so it is the only place
    the doctrine lives -- an empty one silently blanks the host prompt."""
    assert "index" in tools.INSTRUCTIONS and "search" in tools.INSTRUCTIONS
    assert tools.mcp.instructions == tools.INSTRUCTIONS


def test_index_returns_before_the_work_does(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "start", lambda: None)
    monkeypatch.setattr(index, "start_worker", lambda: None)
    project = tmp_path / "p"
    project.mkdir()

    started = time.perf_counter()
    out = tools.index_project(str(project))

    assert time.perf_counter() - started < 1.0, "a return that blocks is a failure"
    assert out["state"] == "indexing" and out["queue_depth"] >= 1


def test_index_on_a_path_that_is_not_a_directory_says_so(tmp_path):
    file = tmp_path / "not-a-dir"
    file.write_text("x")
    assert "error" in tools.index_project(str(file))


def test_unflagging_releases_the_row_and_never_the_index_directory(tmp_path, monkeypatch):
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

    out = tools.index_project(str(project), enabled=False)

    assert out["enabled"] is False
    assert db.exists(), "unflagging must never delete a store"


def test_a_search_error_is_returned_as_data_not_raised(tmp_path):
    """An agent can act on an error that names what to call next; a transport
    failure is just a dead turn."""
    out = tools.search_code("anything", str(tmp_path))
    assert "error" in out and out["results"] == []


def test_an_unknown_mode_reaches_the_caller_as_an_error(tmp_path):
    assert "error" in tools.search_code("q", str(tmp_path), mode="fuzzy")


# ------------------------------------------------------------------ the server


def test_the_app_serves_two_routes_and_no_dashboard():
    paths = {getattr(r, "path", None) for r in server.build_app().routes}
    assert "/mcp" in paths and "/healthz" in paths
    assert not {p for p in paths if p and p.startswith("/api")}


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
    assert "Restart=on-failure" in text
    assert "Type=notify" in text and "WatchdogSec=" in text


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


def test_the_full_flag_is_the_only_thing_that_empties_a_store():
    idx = next(
        a
        for a in cli.build_parser()._subparsers._group_actions[0].choices["index"]._actions
        if a.dest == "full"
    )
    assert idx.help and "rebuild" in idx.help
