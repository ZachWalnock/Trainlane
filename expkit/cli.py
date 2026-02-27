from __future__ import annotations

import argparse
import subprocess
import sys


def run_script(script: str) -> int:
    proc = subprocess.run([sys.executable, "-u", script], check=False)
    return proc.returncode


def main() -> None:
    parser = argparse.ArgumentParser(prog="expkit")
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Run a training script that uses expkit.trainer")
    run_parser.add_argument("script", help="Path to Python script")

    args = parser.parse_args()
    if args.command == "run":
        raise SystemExit(run_script(args.script))

    parser.print_help()
