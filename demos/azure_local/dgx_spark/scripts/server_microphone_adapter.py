#!/usr/bin/env python3
"""Keep the NexiGo hardware capture usable behind a 70% software source.

The webcam's USB mixer incorrectly maps Pulse's logical 70% to raw mixer zero,
which yields digital near-silence. This adapter holds the hardware control at
the user-provided numeric 70 and exposes a remapped source whose software
volume is also 70. It changes no speaker/output loudness.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def run(*parts: str, check: bool = True) -> str:
    result = subprocess.run(parts, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout.strip()


def write_state(path: Path, **payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"updated_at": time.time(), "pid": os.getpid(), **payload}
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def source_names() -> list[str]:
    rows = run("pactl", "list", "short", "sources").splitlines()
    return [parts[1] for row in rows if len(parts := row.split()) >= 2]


def resolve_master(requested: str) -> str:
    names = source_names()
    if requested and requested != "auto" and requested in names:
        return requested
    for name in names:
        lower = name.lower()
        if "monitor" not in lower and ("nexigo" in lower or "webcam" in lower):
            return name
    raise RuntimeError("no NexiGo/webcam Pulse source found")


def module_id_for_source(source_name: str) -> str:
    for row in run("pactl", "list", "short", "modules").splitlines():
        parts = row.split(maxsplit=2)
        if len(parts) >= 3 and parts[1] == "module-remap-source" and f"source_name={source_name}" in parts[2]:
            return parts[0]
    return ""


def ensure_adapter(args: argparse.Namespace) -> tuple[str, str]:
    master = resolve_master(args.master_source)
    run("amixer", "-c", args.alsa_card, "sset", args.alsa_control, f"{args.hardware_percent}%", "cap")
    module_id = module_id_for_source(args.source_name)
    if not module_id:
        module_id = run(
            "pactl", "load-module", "module-remap-source",
            f"master={master}", f"source_name={args.source_name}", "channels=1",
            "master_channel_map=mono", "channel_map=mono", "remix=no",
        )
    run("pactl", "set-source-volume", args.source_name, f"{args.software_percent}%")
    run("pactl", "set-source-mute", args.source_name, "0")
    return master, module_id


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master-source", default="auto")
    parser.add_argument("--source-name", default="dgx_nexigo_70")
    parser.add_argument("--alsa-card", default="1")
    parser.add_argument("--alsa-control", default="Mic")
    parser.add_argument("--hardware-percent", type=int, default=70)
    parser.add_argument("--software-percent", type=int, default=70)
    parser.add_argument("--state-json", required=True)
    parser.add_argument("--check-seconds", type=float, default=5.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state_path = Path(args.state_json)
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    master = ""
    module_id = ""
    while not stopping:
        try:
            master, module_id = ensure_adapter(args)
            write_state(
                state_path,
                status="active",
                component="server_microphone_level_adapter",
                master_source=master,
                output_source=args.source_name,
                hardware_capture_percent=args.hardware_percent,
                software_capture_percent=args.software_percent,
                physical_wave_audio_only=True,
                module_id=module_id,
                message="NexiGo raw capture is adapted to the frozen effective 70% source.",
            )
        except Exception as exc:
            write_state(
                state_path,
                status="error",
                component="server_microphone_level_adapter",
                master_source=master,
                output_source=args.source_name,
                error=f"{type(exc).__name__}: {exc}",
                message="Server microphone adapter is unavailable.",
            )
        deadline = time.time() + max(0.5, args.check_seconds)
        while not stopping and time.time() < deadline:
            time.sleep(0.2)

    write_state(
        state_path,
        status="stopped",
        component="server_microphone_level_adapter",
        master_source=master,
        output_source=args.source_name,
        hardware_capture_percent=args.hardware_percent,
        software_capture_percent=args.software_percent,
        physical_wave_audio_only=True,
        module_id=module_id,
        message="Server microphone adapter stopped.",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
