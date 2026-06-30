#### uv run pyinstaller --onefile --windowed --collect-all gi --add-data "cli.py:." app.py

#!/usr/bin/env python3
"""
release.py — Build script for creating a Linux executable of LTerm.

Produces two binaries in dist/:
  * lterm      — GTK+VTE wrapper (entry point: app.py)
  * lterm-cli  — Textual TUI (entry point: cli.py), launched by lterm

Usage:
    python release.py          # build the executables
    python release.py --clean   # clean previous build artifacts first

Requirements:
    pyinstaller
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
APP_NAME = "lterm"
BUILD_DIR = PROJECT_ROOT / "build"
DIST_DIR = PROJECT_ROOT / "dist"

def clean() -> None:
    """Remove previous build artifacts (build/, dist/, *.spec)."""
    for path in (BUILD_DIR, DIST_DIR):
        if path.exists():
            print(f"[*] Removing {path}")
            shutil.rmtree(path)
    for spec in PROJECT_ROOT.glob("*.spec"):
        print(f"[*] Removing {spec}")
        spec.unlink()

def app_pyinstaller_cmd() -> list[str]:

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--collect-all", "gi",
        "--add-data", "cli.py:.",
        "--add-binary", f"{DIST_DIR}/{APP_NAME}-cli:.",
        "--name", APP_NAME,
        "app.py"
    ]

    return cmd

def cli_pyinstaller_cmd() -> list[str]:

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", f"{APP_NAME}-cli",
        "cli.py"
    ]

    return cmd

def build() -> None:
    """Run PyInstaller to create the two Linux executables."""

    cli_cmd = cli_pyinstaller_cmd()
    print(f"[*] Building {APP_NAME}-cli: {' '.join(cli_cmd)}")
    subprocess.check_call(cli_cmd, cwd=str(PROJECT_ROOT))

    app_cmd = app_pyinstaller_cmd()
    print(f"[*] Building {APP_NAME}: {' '.join(app_cmd)}")
    subprocess.check_call(app_cmd, cwd=str(PROJECT_ROOT))



def main() -> None:
    parser = argparse.ArgumentParser(description="Build Linux executables for LTerm.")
    parser.add_argument("--clean", action="store_true", help="Clean build artifacts before building.")
    args = parser.parse_args()

    if args.clean:
        clean()
    build()


if __name__ == "__main__":
    main()