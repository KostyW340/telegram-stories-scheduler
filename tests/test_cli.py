from __future__ import annotations

import pytest

from app import cli


def test_build_parser_defaults_to_client_mode() -> None:
    parser = cli.build_parser()
    args = parser.parse_args([])
    assert args.command is None
    assert args.handler is cli._run_client


def test_main_dispatches_auth_command(monkeypatch: pytest.MonkeyPatch, isolated_settings) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: isolated_settings)
    monkeypatch.setattr(cli, "configure_logging", lambda *_args, **_kwargs: None)

    import app.auth.cli as auth_cli

    monkeypatch.setattr(auth_cli, "main", lambda: 7)

    assert cli.main(["auth"]) == 7


def test_main_dispatches_default_client_launcher(monkeypatch: pytest.MonkeyPatch, isolated_settings) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: isolated_settings)
    monkeypatch.setattr(cli, "configure_logging", lambda *_args, **_kwargs: None)

    import app.launcher as launcher

    monkeypatch.setattr(launcher, "main", lambda: 11)

    assert cli.main([]) == 11

