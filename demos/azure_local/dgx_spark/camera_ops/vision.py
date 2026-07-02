# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Snapshot helpers used to infer whether a PTZ pulse visibly moved the camera."""

from __future__ import annotations

from io import BytesIO
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from PIL import Image


def snapshot_url(server_url: str, source: str, explicit_url: str | None) -> str:
    if explicit_url:
        return explicit_url
    return f"{server_url.rstrip('/')}/{source}-snapshot.jpg"


def fetch_snapshot(url: str, timeout: float = 3.0) -> bytes:
    request = Request(
        _cache_busted_url(url),
        headers={
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        method="GET",
    )
    with urlopen(request, timeout=max(0.5, float(timeout))) as response:
        return response.read(8_000_000)


def mean_absolute_luma_delta(before: bytes, after: bytes, size: tuple[int, int] = (96, 54)) -> float:
    before_image = _load_luma(before, size)
    after_image = _load_luma(after, size)
    before_pixels = before_image.tobytes()
    after_pixels = after_image.tobytes()
    if len(before_pixels) != len(after_pixels) or not before_pixels:
        return 0.0
    total = sum(abs(left - right) for left, right in zip(before_pixels, after_pixels))
    return total / len(before_pixels)


def _load_luma(payload: bytes, size: tuple[int, int]) -> Image.Image:
    with Image.open(BytesIO(payload)) as image:
        return image.convert("L").resize(size, Image.Resampling.BILINEAR)


def _cache_busted_url(url: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["_"] = str(int(time.time() * 1000))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
