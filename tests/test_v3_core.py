import os

from office import config


def test_reexec_only_when_flag(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "execv", lambda exe, argv: calls.append((exe, argv)))
    monkeypatch.setattr(config, "RESTART_FILE", tmp_path / "RESTART")
    config.reexec_if_requested()
    assert calls == []
    (tmp_path / "RESTART").touch()
    config.reexec_if_requested("t")
    assert len(calls) == 1 and not (tmp_path / "RESTART").exists()  # флаг снят, чтобы не зациклиться


def test_localnow_uses_owner_timezone():
    assert config.localnow().utcoffset().total_seconds() == 3 * 3600  # Europe/Minsk
