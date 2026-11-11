# Bluetooth speaker reconnector

`scripts/bluetooth_speaker_reconnector.py` continuously maintains a Bluetooth
media output for this DGX Spark. It prefers the paired JBL Flip 5 at
`E8:D0:3C:4C:A3:7E`. If the JBL cannot be reached, it tries other **paired**
devices that advertise the Bluetooth Audio Sink profile. Keyboards, mice, and
unknown or unpaired devices are never selected as fallback speakers.

When a speaker connects, the monitor waits for its PipeWire/PulseAudio sink,
sets that sink as the default, and moves active playback streams to it. Failed
connections use exponential backoff. While a fallback speaker is active, the
monitor periodically retries the preferred JBL.

If BlueZ says a speaker is connected but PipeWire does not expose its media
sink, the monitor performs a bounded repair: it disconnects that speaker,
restarts the current user's WirePlumber session, and reconnects. Repairs have a
cooldown so a broken audio stack cannot cause a restart loop.

The user service is installed from
`systemd/user/dgx-spark-bluetooth-speaker.service`:

```bash
systemctl --user status dgx-spark-bluetooth-speaker.service
journalctl --user -u dgx-spark-bluetooth-speaker.service -f
```

For a foreground, one-pass diagnostic:

```bash
./scripts/bluetooth_speaker_reconnector.py --once
```

Useful options include `--interval`, `--preferred-retry`, `--scan-interval`,
`--sink-timeout`, `--no-move-streams`, and `--no-trust-connected`.
