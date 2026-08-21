"""The module entry point delegates to the same CLI and preserves its exit status."""

from __future__ import annotations

import runpy

import pytest

from dreame_valetudo import cli


def test_python_m_entrypoint_returns_the_cli_exit_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "main", lambda: 17)

    with pytest.raises(SystemExit) as exc:
        runpy.run_module("dreame_valetudo.__main__", run_name="__main__")

    assert exc.value.code == 17
