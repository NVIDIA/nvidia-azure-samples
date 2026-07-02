#!/usr/bin/env python3
"""Accept NVIDIA Cosmos gated model licenses on Hugging Face."""

import os
import sys

import requests
from huggingface_hub.utils import build_hf_headers

REPOS = [
    "nvidia/Cosmos-Predict2.5-2B",
    "nvidia/Cosmos-Transfer2.5-2B",
    "nvidia/Cosmos-Guardrail1",
]

CHECKBOX_KEY = (
    "By clicking Submit below, I accept the terms of the NVIDIA Open Model License "
    "Agreement and acknowledge that I am an adult of legal age of majority in the "
    "country in which the Cosmos Models will be used and have authority to accept "
    "this Agreement"
)


def accept_repo(token: str, repo_id: str) -> None:
    url = f"https://huggingface.co/{repo_id}/ask-access"
    headers = build_hf_headers(token=token)
    response = requests.post(url, headers=headers, json={CHECKBOX_KEY: "true"}, timeout=60)
    response.raise_for_status()
    print(f"Accepted license for {repo_id}")


def main() -> int:
    token = os.environ.get("HF_TOKEN", "")
    if not token:
        print("HF_TOKEN is required", file=sys.stderr)
        return 1

    for repo in REPOS:
        accept_repo(token, repo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
