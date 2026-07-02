#!/usr/bin/env python3
"""Keep a preferred Bluetooth speaker connected and routed as audio output."""

from __future__ import annotations

import argparse
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable


JBL_FLIP_5_MAC = "E8:D0:3C:4C:A3:7E"
AUDIO_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"
MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")


@dataclass(frozen=True)
class BluetoothDevice:
    mac: str
    name: str
    paired: bool = False
    connected: bool = False
    blocked: bool = False
    audio_sink: bool = False


def run_command(command: list[str], timeout: float = 20) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError(f"required command is missing: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"command timed out: {' '.join(command)}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "unknown error").strip()
        raise RuntimeError(f"command failed ({' '.join(command)}): {detail}") from exc


def parse_device_lines(output: str) -> dict[str, str]:
    devices: dict[str, str] = {}
    for line in output.splitlines():
        match = MAC_RE.search(line)
        if not match:
            continue
        mac = match.group(0).upper()
        name = line[match.end() :].strip()
        devices[mac] = name or mac
    return devices


def parse_device_info(mac: str, fallback_name: str, output: str) -> BluetoothDevice:
    values: dict[str, str] = {}
    uuids: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("UUID:"):
            uuids.append(line.lower())
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip().lower()] = value.strip()
    return BluetoothDevice(
        mac=mac.upper(),
        name=values.get("name") or values.get("alias") or fallback_name or mac,
        paired=values.get("paired", "no").lower() == "yes",
        connected=values.get("connected", "no").lower() == "yes",
        blocked=values.get("blocked", "no").lower() == "yes",
        audio_sink=(
            any(AUDIO_SINK_UUID in item or "audio sink" in item for item in uuids)
            or values.get("icon", "").lower() == "audio-card"
        ),
    )


def ordered_audio_candidates(devices: list[BluetoothDevice], preferred_mac: str) -> list[BluetoothDevice]:
    preferred = preferred_mac.upper()
    eligible = [device for device in devices if device.paired and device.audio_sink and not device.blocked]
    return sorted(
        eligible,
        key=lambda device: (
            0 if device.mac == preferred else 1,
            0 if device.connected else 1,
            device.name.casefold(),
            device.mac,
        ),
    )


def sink_for_mac(sinks: list[str], mac: str) -> str:
    token = mac.replace(":", "_").lower()
    return next((sink for sink in sinks if token in sink.lower()), "")


class SpeakerReconnector:
    def __init__(
        self,
        preferred_mac: str,
        interval: float,
        preferred_retry: float,
        scan_interval: float,
        scan_seconds: float,
        sink_timeout: float,
        move_streams: bool,
        trust_connected: bool,
        repair_media_session: bool = True,
        repair_cooldown: float = 30.0,
        command_runner: Callable[[list[str], float], subprocess.CompletedProcess[str]] = run_command,
    ) -> None:
        self.preferred_mac = preferred_mac.upper()
        self.interval = interval
        self.preferred_retry = preferred_retry
        self.scan_interval = scan_interval
        self.scan_seconds = scan_seconds
        self.sink_timeout = sink_timeout
        self.move_streams = move_streams
        self.trust_connected = trust_connected
        self.repair_media_session = repair_media_session
        self.repair_cooldown = repair_cooldown
        self.run_command = command_runner
        self.stop_event = threading.Event()
        self.last_scan = 0.0
        self.last_preferred_attempt = 0.0
        self.last_media_repair = 0.0
        self.failures: dict[str, int] = {}
        self.retry_after: dict[str, float] = {}
        self.active_route = ""

    def command(self, command: list[str], timeout: float = 20) -> subprocess.CompletedProcess[str]:
        return self.run_command(command, timeout)

    def discover_devices(self) -> list[BluetoothDevice]:
        paired_output = self.command(["bluetoothctl", "devices", "Paired"]).stdout
        names = parse_device_lines(paired_output)
        devices = []
        for mac, name in names.items():
            try:
                info = self.command(["bluetoothctl", "info", mac]).stdout
            except RuntimeError as exc:
                print(f"Bluetooth info failed for {name} ({mac}): {exc}", flush=True)
                continue
            devices.append(parse_device_info(mac, name, info))
        return devices

    def scan(self) -> None:
        now = time.monotonic()
        if self.scan_seconds <= 0 or now - self.last_scan < self.scan_interval:
            return
        self.last_scan = now
        print(f"Scanning for paired Bluetooth audio devices for {self.scan_seconds:g}s.", flush=True)
        try:
            self.command(
                ["bluetoothctl", "--timeout", str(max(1, int(self.scan_seconds))), "scan", "on"],
                timeout=self.scan_seconds + 5,
            )
        except RuntimeError as exc:
            print(f"Bluetooth scan warning: {exc}", flush=True)
        finally:
            try:
                self.command(["bluetoothctl", "scan", "off"], timeout=5)
            except RuntimeError:
                pass

    def pulse_sinks(self) -> list[str]:
        output = self.command(["pactl", "list", "short", "sinks"]).stdout
        return [fields[1] for line in output.splitlines() if len(fields := line.split()) >= 2]

    def wait_for_sink(self, mac: str) -> str:
        deadline = time.monotonic() + self.sink_timeout
        while not self.stop_event.is_set() and time.monotonic() < deadline:
            sink = sink_for_mac(self.pulse_sinks(), mac)
            if sink:
                return sink
            self.stop_event.wait(0.25)
        return ""

    def route_sink(self, device: BluetoothDevice) -> bool:
        sink = sink_for_mac(self.pulse_sinks(), device.mac) or self.wait_for_sink(device.mac)
        if not sink:
            if self.active_route != f"missing:{device.mac}":
                print(f"{device.name} is connected, but its Bluetooth audio sink is unavailable.", flush=True)
                self.active_route = f"missing:{device.mac}"
            return False
        self.command(["pactl", "set-default-sink", sink])
        if self.move_streams:
            inputs = self.command(["pactl", "list", "short", "sink-inputs"]).stdout
            for line in inputs.splitlines():
                fields = line.split()
                if not fields or not fields[0].isdigit():
                    continue
                try:
                    self.command(["pactl", "move-sink-input", fields[0], sink])
                except RuntimeError as exc:
                    print(f"Could not move audio stream {fields[0]}: {exc}", flush=True)
        route = f"{device.mac}:{sink}"
        if self.active_route != route:
            print(f"Bluetooth audio ready: {device.name} ({device.mac}) -> {sink}", flush=True)
            self.active_route = route
        return True

    def repair_media_route(self, device: BluetoothDevice) -> bool:
        """Refresh WirePlumber and reconnect when BlueZ has no usable A2DP sink."""
        now = time.monotonic()
        if not self.repair_media_session or now - self.last_media_repair < self.repair_cooldown:
            return False
        self.last_media_repair = now
        print(
            f"Refreshing the Bluetooth media session for {device.name}; BlueZ is connected without an audio sink.",
            flush=True,
        )
        try:
            self.command(["bluetoothctl", "disconnect", device.mac], timeout=15)
            self.command(["systemctl", "--user", "restart", "wireplumber.service"], timeout=20)
            self.stop_event.wait(0.75)
            self.command(["bluetoothctl", "connect", device.mac], timeout=30)
            if self.trust_connected:
                self.command(["bluetoothctl", "trust", device.mac], timeout=10)
            return self.route_sink(BluetoothDevice(**{**device.__dict__, "connected": True}))
        except RuntimeError as exc:
            self.record_failure(device, f"media-session repair failed: {exc}")
            return False

    def can_attempt(self, mac: str) -> bool:
        return time.monotonic() >= self.retry_after.get(mac, 0.0)

    def record_failure(self, device: BluetoothDevice, error: str) -> None:
        failures = self.failures.get(device.mac, 0) + 1
        self.failures[device.mac] = failures
        delay = min(60.0, self.interval * (2 ** min(failures - 1, 4)))
        self.retry_after[device.mac] = time.monotonic() + delay
        print(f"Connect failed for {device.name} ({device.mac}); retry in {delay:g}s: {error}", flush=True)

    def connect(self, device: BluetoothDevice) -> bool:
        if not self.can_attempt(device.mac):
            return False
        if device.mac == self.preferred_mac:
            self.last_preferred_attempt = time.monotonic()
        print(f"Connecting Bluetooth audio device {device.name} ({device.mac}).", flush=True)
        try:
            self.command(["bluetoothctl", "connect", device.mac], timeout=30)
            if self.trust_connected:
                self.command(["bluetoothctl", "trust", device.mac], timeout=10)
            connected = BluetoothDevice(**{**device.__dict__, "connected": True})
            if not self.route_sink(connected):
                if not self.repair_media_route(connected):
                    raise RuntimeError("connected without an A2DP/PulseAudio sink")
        except RuntimeError as exc:
            self.record_failure(device, str(exc))
            return False
        self.failures.pop(device.mac, None)
        self.retry_after.pop(device.mac, None)
        return True

    def tick(self) -> bool:
        devices = ordered_audio_candidates(self.discover_devices(), self.preferred_mac)
        preferred = next((device for device in devices if device.mac == self.preferred_mac), None)
        connected = [device for device in devices if device.connected]

        if preferred and preferred.connected:
            return self.route_sink(preferred) or self.repair_media_route(preferred)

        fallback = next((device for device in connected if device.mac != self.preferred_mac), None)
        if fallback:
            routed = self.route_sink(fallback) or self.repair_media_route(fallback)
            if (
                preferred
                and time.monotonic() - self.last_preferred_attempt >= self.preferred_retry
                and self.can_attempt(preferred.mac)
            ):
                return self.connect(preferred) or routed
            return routed

        self.scan()
        if not devices:
            if self.active_route != "no-candidates":
                print("No paired Bluetooth audio devices are configured.", flush=True)
                self.active_route = "no-candidates"
            return False

        for device in devices:
            if self.connect(device):
                return True
        return False

    def run(self, once: bool = False) -> int:
        while not self.stop_event.is_set():
            try:
                self.tick()
            except RuntimeError as exc:
                print(f"Bluetooth speaker monitor warning: {exc}", flush=True)
            if once:
                return 0
            self.stop_event.wait(self.interval)
        return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preferred-mac", default=JBL_FLIP_5_MAC)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--preferred-retry", type=float, default=30.0)
    parser.add_argument("--scan-interval", type=float, default=30.0)
    parser.add_argument("--scan-seconds", type=float, default=4.0)
    parser.add_argument("--sink-timeout", type=float, default=8.0)
    parser.add_argument("--move-streams", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--trust-connected", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--repair-media-session", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--repair-cooldown", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not MAC_RE.fullmatch(args.preferred_mac):
        parser.error("--preferred-mac must be a Bluetooth MAC address")
    for name in ("interval", "preferred_retry", "scan_interval", "scan_seconds", "sink_timeout", "repair_cooldown"):
        if getattr(args, name) < 0:
            parser.error(f"--{name.replace('_', '-')} cannot be negative")
    return args


def main() -> int:
    args = parse_args()
    monitor = SpeakerReconnector(
        preferred_mac=args.preferred_mac,
        interval=args.interval,
        preferred_retry=args.preferred_retry,
        scan_interval=args.scan_interval,
        scan_seconds=args.scan_seconds,
        sink_timeout=args.sink_timeout,
        move_streams=args.move_streams,
        trust_connected=args.trust_connected,
        repair_media_session=args.repair_media_session,
        repair_cooldown=args.repair_cooldown,
    )
    signal.signal(signal.SIGTERM, lambda _signum, _frame: monitor.stop_event.set())
    signal.signal(signal.SIGINT, lambda _signum, _frame: monitor.stop_event.set())
    return monitor.run(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
