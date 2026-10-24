# Kokoro JBL speech test harness

`scripts/kokoro_speech_test.py` generates conversational questions, renders each
one locally with a random Kokoro-82M voice and speaking speed, and plays the WAV
directly on the paired JBL Flip 5 PulseAudio sink.

The script is a lightweight client of the already-running
`deepstream_nemotron_worker.py` service. The resident service owns the warmed
Kokoro model, so the harness does not create another model process or install
its own TTS dependencies.

Run one question:

```bash
./scripts/kokoro_speech_test.py
```

Run five randomized voice/speed trials and retain the WAV files and JSONL
metrics:

```bash
./scripts/kokoro_speech_test.py --count 5 --output-dir speech-test-results
```

Preview a deterministic sequence without loading Kokoro or playing audio:

```bash
./scripts/kokoro_speech_test.py --count 5 --seed 42 --dry-run
```

Useful controls:

- `--voice af_heart` fixes the voice; the default is `random`.
- `--voice-pool af_heart,am_michael,bf_emma` limits random selection.
- `--min-speed 0.9 --max-speed 1.12` controls speed randomization.
- `--question "Can you hear this clearly?"` supplies fixed test text.
- `--no-playback --output-dir DIR` generates files without using the speaker.
- `--service-timeout 120` controls how long the client waits for the resident
  worker.
- `--list-voices` lists the included American and British English voices.

Playback is intentionally strict: unless `--no-playback` is used, the harness
checks/connects Bluetooth device `E8:D0:3C:4C:A3:7E`, resolves a PulseAudio sink
containing that MAC address, and passes it explicitly to `paplay`. It exits with
an error instead of sending a test to an unrelated default output.
