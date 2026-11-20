"""The autouse isolation fixture of tests/conftest.py is active (issue #10)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def test_path_is_empty_so_no_ambient_program_can_start() -> None:
    assert os.environ["PATH"] == ""
    assert shutil.which("git") is None


def test_home_and_config_home_are_private_temp_dirs() -> None:
    home = Path(os.environ["HOME"])
    assert home.is_dir() and not any(home.iterdir())
    assert Path(os.environ["XDG_CONFIG_HOME"]).parent == home
    assert Path.home() == home
