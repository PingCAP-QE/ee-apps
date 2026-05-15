from __future__ import annotations

from types import SimpleNamespace

from roster.jobs import cli


def test_parser_exposes_sync_roster_command() -> None:
    args = cli.build_parser().parse_args(["sync-roster"])

    assert args.command == "sync-roster"


def test_parser_exposes_validate_lark_command() -> None:
    args = cli.build_parser().parse_args(["validate-lark"])

    assert args.command == "validate-lark"


def test_main_runs_sync_roster(monkeypatch) -> None:
    calls: dict[str, object] = {}
    settings = SimpleNamespace(log_level="INFO", lark=SimpleNamespace(is_configured=False))
    engine = SimpleNamespace(dispose=lambda: calls.setdefault("disposed", True))
    summary = SimpleNamespace(groups_seen=2, employees_seen=3)

    monkeypatch.setattr(cli, "get_settings", lambda *args, **kwargs: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda log_level: calls.setdefault("log", log_level))
    monkeypatch.setattr(cli, "build_engine", lambda resolved: calls.setdefault("engine", engine))

    def fake_run_sync_roster(built_engine, *, source=None):
        calls["sync"] = (built_engine, source)
        return summary

    monkeypatch.setattr(cli, "run_sync_roster", fake_run_sync_roster)

    assert cli.main(["sync-roster"]) == 0
    assert calls == {
        "log": "INFO",
        "engine": engine,
        "sync": (engine, None),
        "disposed": True,
    }


def test_main_disposes_engine_when_sync_fails(monkeypatch) -> None:
    calls: dict[str, object] = {}
    settings = SimpleNamespace(log_level="INFO", lark=SimpleNamespace(is_configured=False))
    engine = SimpleNamespace(dispose=lambda: calls.setdefault("disposed", True))

    monkeypatch.setattr(cli, "get_settings", lambda *args, **kwargs: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda log_level: None)
    monkeypatch.setattr(cli, "build_engine", lambda resolved: engine)

    def fail_sync(_built_engine, *, source=None):
        calls["source"] = source
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "run_sync_roster", fail_sync)

    try:
        cli.main(["sync-roster"])
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:  # pragma: no cover
        raise AssertionError("sync failure should propagate")

    assert calls == {"source": None, "disposed": True}


def test_main_builds_lark_source_when_configured(monkeypatch) -> None:
    calls: dict[str, object] = {}
    lark_settings = SimpleNamespace(
        is_configured=True,
        app_id="cli_xxx",
        app_secret="secret",
        github_custom_attr_id="github_attr",
        root_department_id="od-root",
    )
    settings = SimpleNamespace(log_level="INFO", lark=lark_settings)
    engine = SimpleNamespace(dispose=lambda: calls.setdefault("disposed", True))
    source = object()
    client = object()
    summary = SimpleNamespace(groups_seen=2, employees_seen=3)

    monkeypatch.setattr(cli, "get_settings", lambda *args, **kwargs: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda log_level: None)
    monkeypatch.setattr(cli, "build_engine", lambda resolved: engine)
    def fake_lark_api_client(app_id, app_secret):
        calls["client"] = (app_id, app_secret)
        return client

    def fake_lark_roster_source(built_client, github_custom_attr_id, root_department_id):
        calls["source_args"] = (built_client, github_custom_attr_id, root_department_id)
        return source

    monkeypatch.setattr(cli, "LarkApiClient", fake_lark_api_client)
    monkeypatch.setattr(cli, "LarkRosterSource", fake_lark_roster_source)

    def fake_run_sync_roster(built_engine, *, source=None):
        calls["sync"] = (built_engine, source)
        return summary

    monkeypatch.setattr(cli, "run_sync_roster", fake_run_sync_roster)

    assert cli.main(["sync-roster"]) == 0
    assert calls == {
        "client": ("cli_xxx", "secret"),
        "source_args": (client, "github_attr", "od-root"),
        "sync": (engine, source),
        "disposed": True,
    }


def test_main_validate_lark_requires_lark_config(monkeypatch) -> None:
    settings = SimpleNamespace(log_level="INFO", lark=SimpleNamespace(is_configured=False))
    calls: dict[str, object] = {}

    def fake_get_settings(*, require_database=True):
        calls["require_database"] = require_database
        return settings

    monkeypatch.setattr(cli, "get_settings", fake_get_settings)
    monkeypatch.setattr(cli, "configure_logging", lambda log_level: None)

    try:
        cli.main(["validate-lark"])
    except SystemExit as exc:
        assert exc.code == "validate-lark requires ROSTER_LARK_APP_ID and ROSTER_LARK_APP_SECRET"
    else:  # pragma: no cover
        raise AssertionError("validate-lark should require Lark config")
    assert calls == {"require_database": False}


def test_main_validate_lark_prints_json(monkeypatch, capsys) -> None:
    calls: dict[str, object] = {}
    lark_settings = SimpleNamespace(
        is_configured=True,
        app_id="cli_xxx",
        app_secret="secret",
        github_custom_attr_id="github_attr",
        root_department_id="od-root",
    )
    settings = SimpleNamespace(log_level="INFO", lark=lark_settings)
    client = object()
    source = object()
    summary = SimpleNamespace(to_dict=lambda: {"groups": 2, "employees": 3})

    monkeypatch.setattr(cli, "get_settings", lambda *args, **kwargs: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda log_level: None)
    monkeypatch.setattr(cli, "LarkApiClient", lambda app_id, app_secret: client)
    monkeypatch.setattr(cli, "LarkRosterSource", lambda *args, **kwargs: source)
    def fake_validate_lark_roster(built_source):
        calls["source"] = built_source
        return summary

    monkeypatch.setattr(cli, "validate_lark_roster", fake_validate_lark_roster)

    assert cli.main(["validate-lark"]) == 0

    assert calls == {"source": source}
    assert capsys.readouterr().out == '{\n  "employees": 3,\n  "groups": 2\n}\n'


def test_main_rejects_unknown_args() -> None:
    parser = cli.build_parser()

    try:
        parser.parse_args(["sync-roster", "--unknown"])
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover
        raise AssertionError("parse_args should reject unknown arguments")
