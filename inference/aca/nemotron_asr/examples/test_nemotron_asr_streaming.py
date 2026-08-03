# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Stream a WAV file to a Riva ASR endpoint and print transcripts."""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import riva.client


def positive_integer(value: str) -> int:
    parsed_value = int(value)
    if parsed_value <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed_value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream a WAV file to a NVIDIA Riva ASR gRPC endpoint.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--server",
        required=True,
        help="Riva gRPC endpoint, for example '<container-app-fqdn>:443'.",
    )
    parser.add_argument(
        "--use-ssl",
        action="store_true",
        help="Use TLS for the gRPC connection. ACA external ingress requires TLS.",
    )
    parser.add_argument(
        "--input-file",
        required=True,
        type=Path,
        help="Path to a mono, 16-bit PCM WAV file.",
    )
    parser.add_argument(
        "--language-code",
        default="auto",
        help="Language code to send in the Riva recognition config.",
    )
    parser.add_argument(
        "--model",
        default="",
        help="Optional Riva ASR model name.",
    )
    parser.add_argument(
        "--chunk-duration-ms",
        default=1000,
        type=positive_integer,
        help="Approximate amount of audio to send in each streaming chunk.",
    )
    parser.add_argument(
        "--show-interim",
        action="store_true",
        help="Print interim transcription hypotheses in addition to final text.",
    )
    return parser.parse_args()


def read_wav_parameters(path: Path) -> tuple[int, int, int]:
    try:
        with wave.open(str(path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate = wav_file.getframerate()
            compression_type = wav_file.getcomptype()
    except wave.Error as exc:
        raise SystemExit(f"{path} is not a valid WAV file: {exc}") from exc

    if channels != 1:
        raise SystemExit(f"{path} must be mono; found {channels} channels.")
    if sample_width != 2:
        raise SystemExit(
            f"{path} must be 16-bit PCM; found sample width {sample_width} bytes."
        )
    if compression_type != "NONE":
        raise SystemExit(
            f"{path} must use uncompressed PCM; found {compression_type} compression."
        )

    return channels, sample_width, sample_rate


def print_response(response: object, show_interim: bool) -> None:
    for result in response.results:
        if not result.alternatives:
            continue
        if result.is_final:
            print(result.alternatives[0].transcript)
        elif show_interim:
            print(f"[interim] {result.alternatives[0].transcript}", flush=True)


def main() -> int:
    args = parse_args()
    input_file = args.input_file.expanduser()
    if not input_file.is_file():
        raise SystemExit(f"Input file not found: {input_file}")

    _, _, sample_rate = read_wav_parameters(input_file)
    chunk_frames = max(1, sample_rate * args.chunk_duration_ms // 1000)

    auth = riva.client.Auth(uri=args.server, use_ssl=args.use_ssl)
    asr_service = riva.client.ASRService(auth)

    recognition_config = riva.client.RecognitionConfig(
        encoding=riva.client.AudioEncoding.LINEAR_PCM,
        language_code=args.language_code,
        max_alternatives=1,
        enable_automatic_punctuation=True,
        sample_rate_hertz=sample_rate,
        audio_channel_count=1,
    )
    if args.model:
        recognition_config.model = args.model

    streaming_config = riva.client.StreamingRecognitionConfig(
        config=recognition_config,
        interim_results=args.show_interim,
    )

    with riva.client.AudioChunkFileIterator(
        str(input_file), chunk_frames
    ) as audio_chunks:
        responses = asr_service.streaming_response_generator(
            audio_chunks=audio_chunks,
            streaming_config=streaming_config,
        )

        for response in responses:
            print_response(response, args.show_interim)

    return 0


if __name__ == "__main__":
    sys.exit(main())
