#!/usr/bin/env python3
"""Download Cosmos Transfer checkpoints into HF_HOME for reuse across inference jobs."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default="edge/distilled",
        help="Model key to prefetch (default: edge/distilled)",
    )
    parser.add_argument(
        "--marker",
        default=None,
        help="Write this file when prefetch completes (default: $HF_HOME/.prefetch-<model>.ok)",
    )
    return parser.parse_args()


def _marker_path(model: str, marker: str | None) -> Path:
    if marker is not None:
        return Path(marker)
    safe_name = model.replace("/", "-")
    hf_home = os.environ.get("HF_HOME", "/cache/huggingface")
    return Path(hf_home) / f".prefetch-{safe_name}.ok"


# Checkpoints loaded during edge/distilled inference (from successful AKS job logs).
EDGE_DISTILLED_CHECKPOINTS = [
    "41f07f13-f2e4-4e34-ba4c-86f595acbc20",  # distilled edge main weights
    "38c6c645-7d41-4560-8eeb-6f4ddc0e6574",  # action-cond base (loaded at startup)
    "7219c6c7-f878-4137-bbdb-76842ea85e70",  # Qwen2.5-VL tokenizer
    "cb3e3ffa-7b08-4c34-822d-61c7aa31a14f",  # Cosmos-Reason1.1 text encoder
    "685afcaa-4de2-42fe-b7b9-69f7a2dee4d8",  # Wan2.1 VAE
]

PREFETCH_BY_MODEL: dict[str, list[str]] = {
    "edge/distilled": EDGE_DISTILLED_CHECKPOINTS,
}


def main() -> int:
    args = _parse_args()
    os.environ.setdefault("COSMOS_EXPERIMENTAL_CHECKPOINTS", "1")

    marker_path = _marker_path(args.model, args.marker)
    if marker_path.exists():
        print(f"Marker already exists at {marker_path}; skipping prefetch")
        return 0

    checkpoint_ids = PREFETCH_BY_MODEL.get(args.model)
    if checkpoint_ids is None:
        print(f"No prefetch list for model {args.model!r}", file=sys.stderr)
        print(f"Known models: {', '.join(sorted(PREFETCH_BY_MODEL))}", file=sys.stderr)
        return 1

    from cosmos_transfer2._src.imaginaire.utils.checkpoint_db import download_checkpoint
    from cosmos_transfer2._src.predict2.text_encoders.text_encoder import TextEncoderConfig

    # Import registers all checkpoint UUIDs / S3 mappings.
    import cosmos_transfer2.config  # noqa: F401

    from scripts.accept_hf_licenses import main as accept_hf_licenses

    if accept_hf_licenses() != 0:
        return 1

    for checkpoint_id in checkpoint_ids:
        print(f"Prefetching {checkpoint_id}...")
        path = download_checkpoint(checkpoint_id)
        print(f"  -> {path}")

    # Text encoder checkpoint referenced by the interactive config.
    text_encoder_s3 = TextEncoderConfig().ckpt_path
    print(f"Prefetching text encoder from {text_encoder_s3}...")
    path = download_checkpoint(text_encoder_s3)
    print(f"  -> {path}")

    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(f"model={args.model}\n", encoding="utf-8")
    print(f"Wrote marker {marker_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
