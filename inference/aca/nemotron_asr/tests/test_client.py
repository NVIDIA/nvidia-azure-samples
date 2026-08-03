# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock


SAMPLE_ROOT = Path(__file__).parents[1]
CLIENT_PATH = SAMPLE_ROOT / "examples" / "test_nemotron_asr_streaming.py"
SPEC = importlib.util.spec_from_file_location("nemotron_asr_client", CLIENT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to load client module from {CLIENT_PATH}")
CLIENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLIENT)


class ClientTests(unittest.TestCase):
    def test_language_defaults_to_automatic_detection(self) -> None:
        with mock.patch.object(
            sys,
            "argv",
            ["client", "--server", "example:443", "--input-file", "audio.wav"],
        ):
            args = CLIENT.parse_args()

        self.assertEqual(args.language_code, "auto")

    def test_chunk_duration_must_be_positive(self) -> None:
        with self.assertRaises(argparse.ArgumentTypeError):
            CLIENT.positive_integer("0")

    def test_reads_supported_wav_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "audio.wav"
            with wave.open(str(wav_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(b"\x00\x00" * 160)

            self.assertEqual(CLIENT.read_wav_parameters(wav_path), (1, 2, 16000))

    def test_rejects_stereo_wav(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "stereo.wav"
            with wave.open(str(wav_path), "wb") as wav_file:
                wav_file.setnchannels(2)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(b"\x00\x00" * 320)

            with self.assertRaisesRegex(SystemExit, "must be mono"):
                CLIENT.read_wav_parameters(wav_path)


if __name__ == "__main__":
    unittest.main()
