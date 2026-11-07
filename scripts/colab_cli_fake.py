#!/usr/bin/env python3
"""Subprocess stand-in for the official colab CLI. It never contacts Google.

The scenario is read only from COLAB_CLI_FAKE_SCENARIO. User script files are
not executed.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _config_path(argv: list[str]) -> Path:
    if "--config" not in argv:
        return Path(os.environ.get("HOME", ".")) / "sessions.json"
    return Path(argv[argv.index("--config") + 1])


def _load(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _flag(argv: list[str], name: str) -> str:
    if name not in argv:
        return ""
    index = argv.index(name)
    return argv[index + 1] if index + 1 < len(argv) else ""


def main(argv: list[str]) -> int:
    scenario = os.environ.get("COLAB_CLI_FAKE_SCENARIO", "ok")
    # Drop the executable and global flags so the subcommand is visible.
    args = list(argv[1:])
    while args and args[0].startswith("-"):
        flag = args.pop(0)
        if flag in {"--auth", "--config", "-c", "--client-oauth-config"} and args:
            args.pop(0)
    command = args[0] if args else ""
    config = _config_path(argv)
    if scenario == "auth":
        sys.stderr.write(
            "No valid default credentials found. To authenticate, run:\n\n"
            "  gcloud auth application-default login \\\n"
            "      --scopes=openid,"
            "https://www.googleapis.com/auth/cloud-platform,"
            "https://www.googleapis.com/auth/userinfo.email,"
            "https://www.googleapis.com/auth/colaboratory\n"
        )
        return 1
    if command == "usage":
        if scenario == "usage-down":
            sys.stderr.write("compute-unit info unavailable\n")
            return 1
        sys.stdout.write(
            "Current balance: 12.50 compute units\n"
            "Usage rate: 1.25/hr\n"
            "Active assignments: 0\n"
        )
        return 0
    if command == "new":
        name = _flag(args, "-s") or _flag(args, "--session")
        sessions = _load(config)
        sessions[name] = {
            "name": name,
            "token": "ya29.FAKE-TOKEN-VALUE",
            "url": "https://example.invalid/runtime",
            "endpoint": f"ep-{name}",
            "accelerator": "T4" if "--gpu" in args else "NONE",
        }
        _save(config, sessions)
        sys.stderr.write(f"[colab] Session READY ({name}).\n")
        return 0
    if command == "exec":
        name = _flag(args, "-s")
        script = _flag(args, "-f")
        text = Path(script).read_text(encoding="utf-8") if script else ""
        sys.stdout.write(f"[fake] executed-remote name={name} bytes={len(text.encode())}\n")
        if scenario == "exec-fail":
            sys.stderr.write("kernel error\n")
            return 1
        return 0
    if command == "stop":
        name = _flag(args, "-s")
        sessions = _load(config)
        count_path = config.with_name("stop-count.json")
        try:
            count = json.loads(count_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            count = {}
        count[name] = int(count.get(name, 0)) + 1
        count_path.write_text(json.dumps(count), encoding="utf-8")
        if scenario == "release-unproven" and count[name] < 2:
            sys.stderr.write("[colab] Session terminated.\n")
            return 0
        if name not in sessions:
            sys.stderr.write(f"[colab] Session '{name}' not found.\n")
            return 0
        del sessions[name]
        _save(config, sessions)
        sys.stderr.write("[colab] Session terminated.\n")
        return 0
    if command == "sessions":
        sessions = _load(config)
        if not sessions:
            sys.stdout.write("[colab] No active sessions found on server.\n")
            return 0
        for name, row in sessions.items():
            endpoint = row.get("endpoint", "")
            hardware = "CPU" if row.get("accelerator") == "NONE" else row.get("accelerator")
            sys.stdout.write(
                f"[{name}] {endpoint} | Hardware: {hardware} | Shape: Standard | Variant: DEFAULT\n"
            )
        return 0
    sys.stderr.write(f"unsupported fake command: {command}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
