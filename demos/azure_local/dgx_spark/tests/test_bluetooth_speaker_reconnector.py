import importlib.util
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bluetooth_speaker_reconnector.py"
SPEC = importlib.util.spec_from_file_location("bluetooth_speaker_reconnector", SCRIPT)
assert SPEC and SPEC.loader
reconnector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reconnector
SPEC.loader.exec_module(reconnector)


def test_parse_devices_and_audio_profile():
    devices = reconnector.parse_device_lines(
        "Device E8:D0:3C:4C:A3:7E JBL Flip 5\nDevice E3:60:55:BE:90:F8 Surface Mouse\n"
    )
    speaker = reconnector.parse_device_info(
        "E8:D0:3C:4C:A3:7E",
        devices["E8:D0:3C:4C:A3:7E"],
        """Name: JBL Flip 5
Paired: yes
Connected: no
Blocked: no
UUID: Audio Sink (0000110b-0000-1000-8000-00805f9b34fb)
""",
    )

    assert speaker.name == "JBL Flip 5"
    assert speaker.paired is True
    assert speaker.audio_sink is True
    assert devices["E3:60:55:BE:90:F8"] == "Surface Mouse"


def test_candidate_order_prefers_jbl_and_excludes_non_audio_devices():
    speaker = reconnector.BluetoothDevice(
        "E8:D0:3C:4C:A3:7E", "JBL Flip 5", paired=True, audio_sink=True
    )
    fallback = reconnector.BluetoothDevice(
        "00:11:22:33:44:55", "Other Speaker", paired=True, connected=True, audio_sink=True
    )
    mouse = reconnector.BluetoothDevice(
        "E3:60:55:BE:90:F8", "Surface Mouse", paired=True, connected=True, audio_sink=False
    )

    candidates = reconnector.ordered_audio_candidates([fallback, mouse, speaker], speaker.mac)

    assert candidates == [speaker, fallback]


def test_sink_resolution_uses_device_mac():
    sinks = [
        "alsa_output.platform.hdmi-stereo",
        "bluez_output.E8_D0_3C_4C_A3_7E.1",
    ]

    assert reconnector.sink_for_mac(sinks, "E8:D0:3C:4C:A3:7E") == sinks[1]
    assert reconnector.sink_for_mac(sinks, "00:11:22:33:44:55") == ""


def test_connected_preferred_speaker_is_routed_without_connect(monkeypatch):
    monitor = reconnector.SpeakerReconnector(
        preferred_mac=reconnector.JBL_FLIP_5_MAC,
        interval=5,
        preferred_retry=30,
        scan_interval=30,
        scan_seconds=0,
        sink_timeout=0,
        move_streams=True,
        trust_connected=True,
    )
    speaker = reconnector.BluetoothDevice(
        reconnector.JBL_FLIP_5_MAC,
        "JBL Flip 5",
        paired=True,
        connected=True,
        audio_sink=True,
    )
    monkeypatch.setattr(monitor, "discover_devices", lambda: [speaker])
    routed = []
    monkeypatch.setattr(monitor, "route_sink", lambda device: routed.append(device) or True)
    monkeypatch.setattr(monitor, "connect", lambda _device: (_ for _ in ()).throw(AssertionError("connect called")))

    assert monitor.tick() is True
    assert routed == [speaker]


def test_fallback_speaker_is_used_when_jbl_is_unavailable(monkeypatch):
    monitor = reconnector.SpeakerReconnector(
        preferred_mac=reconnector.JBL_FLIP_5_MAC,
        interval=5,
        preferred_retry=30,
        scan_interval=30,
        scan_seconds=0,
        sink_timeout=0,
        move_streams=True,
        trust_connected=True,
    )
    jbl = reconnector.BluetoothDevice(
        reconnector.JBL_FLIP_5_MAC, "JBL Flip 5", paired=True, audio_sink=True
    )
    fallback = reconnector.BluetoothDevice(
        "00:11:22:33:44:55", "Other Speaker", paired=True, audio_sink=True
    )
    monkeypatch.setattr(monitor, "discover_devices", lambda: [fallback, jbl])
    attempted = []
    monkeypatch.setattr(monitor, "connect", lambda device: attempted.append(device.mac) or device == fallback)

    assert monitor.tick() is True
    assert attempted == [jbl.mac, fallback.mac]


def test_connected_speaker_repairs_missing_media_sink(monkeypatch):
    monitor = reconnector.SpeakerReconnector(
        preferred_mac=reconnector.JBL_FLIP_5_MAC,
        interval=5,
        preferred_retry=30,
        scan_interval=30,
        scan_seconds=0,
        sink_timeout=0,
        move_streams=True,
        trust_connected=True,
    )
    speaker = reconnector.BluetoothDevice(
        reconnector.JBL_FLIP_5_MAC,
        "JBL Flip 5",
        paired=True,
        connected=True,
        audio_sink=True,
    )
    monkeypatch.setattr(monitor, "discover_devices", lambda: [speaker])
    monkeypatch.setattr(monitor, "route_sink", lambda _device: False)
    repaired = []
    monkeypatch.setattr(monitor, "repair_media_route", lambda device: repaired.append(device) or True)

    assert monitor.tick() is True
    assert repaired == [speaker]
