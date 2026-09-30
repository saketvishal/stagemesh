from __future__ import annotations

import sys
from pathlib import Path
import pytest

from stagemesh.cli import main
from stagemesh.persistence import Store


def test_cli_labels_setup_command(tmp_path: Path, monkeypatch, capsys):
    proj_dir = tmp_path / "project"
    proj_dir.mkdir()
    (proj_dir / ".stagemesh").mkdir()
    store = Store(proj_dir / ".stagemesh" / "stagemesh.db")
    store.migrate()

    monkeypatch.setattr(sys, "argv", ["stagemesh", "labels", "setup", "--project", str(proj_dir)])
    ret = main()
    assert ret == 0

    captured = capsys.readouterr()
    assert "Labels setup complete" in captured.out or "Provisioned" in captured.out


def test_cli_watch_foreground_command(tmp_path: Path, monkeypatch, capsys):
    proj_dir = tmp_path / "project"
    proj_dir.mkdir()
    (proj_dir / ".stagemesh").mkdir()
    store = Store(proj_dir / ".stagemesh" / "stagemesh.db")
    store.migrate()

    monkeypatch.setattr(sys, "argv", ["stagemesh", "watch", "--once", "--project", str(proj_dir)])
    ret = main()
    assert ret == 0

    captured = capsys.readouterr()
    assert "Watcher tick complete" in captured.out or "Watch cycle" in captured.out


def test_cli_daemon_startup_command(tmp_path: Path, monkeypatch, capsys):
    proj_dir = tmp_path / "project"
    proj_dir.mkdir()
    (proj_dir / ".stagemesh").mkdir()
    store = Store(proj_dir / ".stagemesh" / "stagemesh.db")
    store.migrate()

    monkeypatch.setattr(sys, "argv", ["stagemesh", "daemon", "--once", "--project", str(proj_dir)])
    ret = main()
    assert ret == 0

    captured = capsys.readouterr()
    assert "Daemon" in captured.out or "Watcher" in captured.out
