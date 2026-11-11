# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Capture a stitched PTZ snapshot grid from a centered local IP camera."""

from __future__ import annotations

import argparse
import html
import json
from io import BytesIO
from pathlib import Path
import sys
import time
from typing import Any

from PIL import Image, ImageDraw


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from camera_ops.client import CameraPtzClient, PtzError, clamp_speed
from camera_ops.pan import ptz_url
from camera_ops.vision import fetch_snapshot, snapshot_url


GRID_PLACEHOLDER = (24, 26, 30)
GRID_LINE = (220, 220, 220)


class GridScanError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan a PTZ snapshot grid and stitch it into one image.")
    parser.add_argument("--steps_width", "--steps-width", dest="steps_width", type=int, default=11)
    parser.add_argument("--steps_height", "--steps-height", dest="steps_height", type=int, default=5)
    parser.add_argument("--step-ms", "--step_ms", dest="step_ms", type=int, default=1000)
    parser.add_argument("--settle-ms", "--settle_ms", dest="settle_ms", type=int, default=500)
    parser.add_argument("--snapshot-delay-ms", "--snapshot_delay_ms", dest="snapshot_delay_ms", type=int, default=500)
    parser.add_argument("--speed", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--pulse-mode", "--pulse_mode", dest="pulse_mode", choices=("server", "timed"), default="server")
    parser.add_argument("--source", choices=("wifi", "bulb"), default="wifi")
    parser.add_argument("--server-url", "--server_url", dest="server_url", default="http://127.0.0.1:8090")
    parser.add_argument("--url", default=None, help="Full PTZ endpoint URL. Overrides --server-url and --source.")
    parser.add_argument("--snapshot-url", "--snapshot_url", dest="snapshot_url", default=None)
    parser.add_argument("--output", default=str(Path(__file__).with_name("grid_scan.jpg")))
    parser.add_argument("--metadata-output", "--metadata_output", dest="metadata_output", default="")
    parser.add_argument("--cell-width", "--cell_width", dest="cell_width", type=int, default=320)
    parser.add_argument("--cell-height", "--cell_height", dest="cell_height", type=int, default=180)
    parser.add_argument("--preview-output", "--preview_output", dest="preview_output", default="")
    parser.add_argument("--no-preview-image", "--no_preview_image", dest="no_preview_image", action="store_true")
    parser.add_argument("--preview-html", "--preview_html", dest="preview_html", default="")
    parser.add_argument("--no-preview-html", "--no_preview_html", dest="no_preview_html", action="store_true")
    parser.add_argument("--return-to-center", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--live-grid", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def debug_log(message: str) -> None:
    print(f"[camera_ops.grid_scan] {message}", file=sys.stderr, flush=True)


def clamp_positive_int(value: int, default: int, upper: int) -> int:
    try:
        return max(1, min(upper, int(value)))
    except (TypeError, ValueError):
        return default


def normalize_output_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def default_preview_output_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}_preview{output_path.suffix or '.jpg'}")


def grid_start_offset(width: int, height: int) -> tuple[int, int]:
    return -(width // 2), -(height // 2)


def move_one(
    *,
    client: CameraPtzClient,
    direction: str,
    speed: int,
    step_ms: int,
    pulse_mode: str,
) -> dict[str, Any]:
    try:
        if pulse_mode == "server":
            response = client.pulse(command=direction, speed=speed, duration_ms=step_ms)
        else:
            response = client.timed_pulse(command=direction, speed=speed, duration_ms=step_ms)
    except PtzError as exc:
        raise GridScanError(str(exc)) from exc
    return response.raw


def move_steps(
    *,
    client: CameraPtzClient,
    direction: str,
    steps: int,
    speed: int,
    step_ms: int,
    pulse_mode: str,
    settle_ms: int,
) -> list[dict[str, Any]]:
    records = []
    for index in range(max(0, steps)):
        debug_log(f"move direction={direction} step={index + 1}/{steps} step_ms={step_ms} pulse_mode={pulse_mode}")
        raw = move_one(client=client, direction=direction, speed=speed, step_ms=step_ms, pulse_mode=pulse_mode)
        records.append({"direction": direction, "step": index + 1, "steps": steps, "response": raw})
        if settle_ms > 0 and index < steps - 1:
            time.sleep(settle_ms / 1000.0)
    return records


def capture_cell(
    *,
    snap_url: str,
    timeout: float,
    snapshot_delay_ms: int,
    cell_size: tuple[int, int],
) -> Image.Image:
    if snapshot_delay_ms > 0:
        time.sleep(snapshot_delay_ms / 1000.0)
    payload = fetch_snapshot(snap_url, timeout=timeout)
    with Image.open(BytesIO(payload)) as image:
        return image.convert("RGB").resize(cell_size, Image.Resampling.BILINEAR)


def blank_grid(width: int, height: int, cell_size: tuple[int, int]) -> Image.Image:
    image = Image.new("RGB", (width * cell_size[0], height * cell_size[1]), GRID_PLACEHOLDER)
    draw_grid_lines(image, width, height, cell_size)
    return image


def draw_grid_lines(image: Image.Image, width: int, height: int, cell_size: tuple[int, int]) -> None:
    draw = ImageDraw.Draw(image)
    total_width, total_height = image.size
    for col in range(width + 1):
        x = min(total_width - 1, col * cell_size[0])
        draw.line((x, 0, x, total_height), fill=GRID_LINE)
    for row in range(height + 1):
        y = min(total_height - 1, row * cell_size[1])
        draw.line((0, y, total_width, y), fill=GRID_LINE)


def save_grid(
    *,
    cells: list[list[Image.Image | None]],
    output_path: Path,
    cell_size: tuple[int, int],
) -> None:
    height = len(cells)
    width = len(cells[0]) if cells else 0
    grid = blank_grid(width, height, cell_size)
    for row_index, row in enumerate(cells):
        for col_index, cell in enumerate(row):
            if cell is not None:
                grid.paste(cell, (col_index * cell_size[0], row_index * cell_size[1]))
    draw_grid_lines(grid, width, height, cell_size)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f".{output_path.name}.tmp")
    image_format = "JPEG" if output_path.suffix.lower() in {".jpg", ".jpeg"} else "PNG"
    save_kwargs = {"quality": 92} if image_format == "JPEG" else {}
    grid.save(temp_path, format=image_format, **save_kwargs)
    temp_path.replace(output_path)


def save_preview_html(
    *,
    image_path: Path,
    preview_html_path: Path,
    width: int,
    height: int,
    cell_size: tuple[int, int],
) -> None:
    preview_html_path.parent.mkdir(parents=True, exist_ok=True)
    if image_path.parent == preview_html_path.parent:
        image_src = image_path.name
    else:
        image_src = image_path.as_uri()
    title = f"Camera grid scan preview ({width}x{height})"
    preview_html_path.write_text(
        "\n".join(
            [
                "<!doctype html>",
                "<meta charset=\"utf-8\">",
                f"<title>{html.escape(title)}</title>",
                "<style>",
                "body { margin: 0; background: #111; color: #eee; font-family: system-ui, sans-serif; }",
                ".bar { padding: 10px 12px; font-size: 14px; background: #1d1f24; }",
                "img { display: block; width: 100vw; height: auto; }",
                "</style>",
                f"<div class=\"bar\">{html.escape(title)} &middot; {html.escape(str(image_path))}</div>",
                "<img id=\"grid\" alt=\"Camera grid scan preview\">",
                "<script>",
                f"const imagePath = {json.dumps(image_src)};",
                "const img = document.getElementById('grid');",
                "function refresh() { img.src = imagePath + '?t=' + Date.now(); }",
                "refresh();",
                "setInterval(refresh, 1000);",
                "</script>",
                "",
            ]
        ),
        encoding="utf-8",
    )


def scan_grid(
    *,
    client: CameraPtzClient,
    snap_url: str,
    output_path: Path,
    width: int,
    height: int,
    cell_size: tuple[int, int],
    speed: int,
    step_ms: int,
    settle_ms: int,
    snapshot_delay_ms: int,
    timeout: float,
    pulse_mode: str,
    return_to_center: bool,
    live_grid: bool,
    preview_image_path: Path | None,
    preview_html_path: Path | None,
) -> dict[str, Any]:
    x, y = 0, 0
    start_x, start_y = grid_start_offset(width, height)
    records: list[dict[str, Any]] = []
    move_records: list[dict[str, Any]] = []
    cells: list[list[Image.Image | None]] = [[None for _ in range(width)] for _ in range(height)]

    if preview_image_path is not None:
        save_grid(cells=cells, output_path=preview_image_path, cell_size=cell_size)
        if preview_html_path is not None:
            save_preview_html(
                image_path=preview_image_path,
                preview_html_path=preview_html_path,
                width=width,
                height=height,
                cell_size=cell_size,
            )
            debug_log(f"preview_html={preview_html_path} preview_image={preview_image_path}")

    if start_x < 0:
        move_records.extend(move_steps(client=client, direction="left", steps=abs(start_x), speed=speed, step_ms=step_ms, pulse_mode=pulse_mode, settle_ms=settle_ms))
    elif start_x > 0:
        move_records.extend(move_steps(client=client, direction="right", steps=start_x, speed=speed, step_ms=step_ms, pulse_mode=pulse_mode, settle_ms=settle_ms))
    x = start_x
    if start_y < 0:
        move_records.extend(move_steps(client=client, direction="up", steps=abs(start_y), speed=speed, step_ms=step_ms, pulse_mode=pulse_mode, settle_ms=settle_ms))
    elif start_y > 0:
        move_records.extend(move_steps(client=client, direction="down", steps=start_y, speed=speed, step_ms=step_ms, pulse_mode=pulse_mode, settle_ms=settle_ms))
    y = start_y

    for row in range(height):
        left_to_right = row % 2 == 0
        cols = range(width) if left_to_right else range(width - 1, -1, -1)
        row_direction = "right" if left_to_right else "left"
        for col_index, col in enumerate(cols):
            cell = capture_cell(snap_url=snap_url, timeout=timeout, snapshot_delay_ms=snapshot_delay_ms, cell_size=cell_size)
            cells[row][col] = cell
            record = {
                "row": row,
                "col": col,
                "x_steps": x,
                "y_steps": y,
                "captured_cells": len(records) + 1,
                "total_cells": width * height,
            }
            records.append(record)
            if live_grid and preview_image_path is not None:
                save_grid(cells=cells, output_path=preview_image_path, cell_size=cell_size)
            debug_log(
                f"captured row={row + 1}/{height} col={col + 1}/{width} "
                f"x_steps={x} y_steps={y} captured={len(records)}/{width * height} "
                f"preview={preview_image_path or ''}"
            )
            if col_index < width - 1:
                move_records.extend(move_steps(client=client, direction=row_direction, steps=1, speed=speed, step_ms=step_ms, pulse_mode=pulse_mode, settle_ms=settle_ms))
                x += 1 if row_direction == "right" else -1
        if row < height - 1:
            move_records.extend(move_steps(client=client, direction="down", steps=1, speed=speed, step_ms=step_ms, pulse_mode=pulse_mode, settle_ms=settle_ms))
            y += 1

    save_grid(cells=cells, output_path=output_path, cell_size=cell_size)
    if preview_image_path is not None and not live_grid:
        save_grid(cells=cells, output_path=preview_image_path, cell_size=cell_size)

    return_records: list[dict[str, Any]] = []
    if return_to_center:
        if x < 0:
            return_records.extend(move_steps(client=client, direction="right", steps=abs(x), speed=speed, step_ms=step_ms, pulse_mode=pulse_mode, settle_ms=settle_ms))
        elif x > 0:
            return_records.extend(move_steps(client=client, direction="left", steps=x, speed=speed, step_ms=step_ms, pulse_mode=pulse_mode, settle_ms=settle_ms))
        if y < 0:
            return_records.extend(move_steps(client=client, direction="down", steps=abs(y), speed=speed, step_ms=step_ms, pulse_mode=pulse_mode, settle_ms=settle_ms))
        elif y > 0:
            return_records.extend(move_steps(client=client, direction="up", steps=y, speed=speed, step_ms=step_ms, pulse_mode=pulse_mode, settle_ms=settle_ms))

    return {
        "status": "ok",
        "output": str(output_path),
        "preview_output": str(preview_image_path) if preview_image_path is not None else "",
        "preview_html": str(preview_html_path) if preview_html_path is not None else "",
        "steps_width": width,
        "steps_height": height,
        "cell_width": cell_size[0],
        "cell_height": cell_size[1],
        "captured_cells": len(records),
        "total_cells": width * height,
        "start_offset": {"x_steps": start_x, "y_steps": start_y},
        "final_scan_offset": {"x_steps": x, "y_steps": y},
        "returned_to_center": return_to_center,
        "capture_records": records,
        "move_records": move_records,
        "return_records": return_records,
    }


def dry_run_plan(
    args: argparse.Namespace,
    control_url: str,
    snap_url: str,
    output_path: Path,
    preview_image_path: Path | None,
    preview_html_path: Path | None,
) -> dict[str, Any]:
    width = clamp_positive_int(args.steps_width, 11, 100)
    height = clamp_positive_int(args.steps_height, 5, 100)
    start_x, start_y = grid_start_offset(width, height)
    rows = []
    for row in range(height):
        left_to_right = row % 2 == 0
        rows.append(
            {
                "row": row,
                "direction": "right" if left_to_right else "left",
                "columns": list(range(width) if left_to_right else range(width - 1, -1, -1)),
            }
        )
    return {
        "status": "dry_run",
        "url": control_url,
        "snapshot_url": snap_url,
        "output": str(output_path),
        "preview_output": str(preview_image_path) if preview_image_path is not None else "",
        "preview_html": str(preview_html_path) if preview_html_path is not None else "",
        "steps_width": width,
        "steps_height": height,
        "start_offset": {"x_steps": start_x, "y_steps": start_y},
        "row_plan": rows,
        "pulse_mode": args.pulse_mode,
        "step_ms": max(1, min(1000, int(args.step_ms))),
        "live_grid": bool(args.live_grid),
        "return_to_center": bool(args.return_to_center),
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    width = clamp_positive_int(args.steps_width, 11, 100)
    height = clamp_positive_int(args.steps_height, 5, 100)
    step_ms = max(1, min(1000, int(args.step_ms)))
    settle_ms = max(0, min(10000, int(args.settle_ms)))
    snapshot_delay_ms = max(0, min(10000, int(args.snapshot_delay_ms)))
    speed = clamp_speed(args.speed)
    cell_size = (
        clamp_positive_int(args.cell_width, 320, 4096),
        clamp_positive_int(args.cell_height, 180, 4096),
    )
    control_url = ptz_url(args.server_url, args.source, args.url)
    snap_url = snapshot_url(args.server_url, args.source, args.snapshot_url)
    output_path = normalize_output_path(args.output)
    preview_image_path = None
    if not args.no_preview_image:
        preview_image_path = normalize_output_path(args.preview_output) if args.preview_output else default_preview_output_path(output_path)
    preview_html_path = None
    if not args.no_preview_html:
        preview_base = preview_image_path or output_path
        preview_html_path = normalize_output_path(args.preview_html) if args.preview_html else preview_base.with_suffix(".html")

    if args.dry_run:
        print(
            json.dumps(
                dry_run_plan(args, control_url, snap_url, output_path, preview_image_path, preview_html_path),
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    client = CameraPtzClient(url=control_url, timeout=args.timeout)
    try:
        result = scan_grid(
            client=client,
            snap_url=snap_url,
            output_path=output_path,
            width=width,
            height=height,
            cell_size=cell_size,
            speed=speed,
            step_ms=step_ms,
            settle_ms=settle_ms,
            snapshot_delay_ms=snapshot_delay_ms,
            timeout=args.timeout,
            pulse_mode=args.pulse_mode,
            return_to_center=bool(args.return_to_center),
            live_grid=bool(args.live_grid),
            preview_image_path=preview_image_path,
            preview_html_path=preview_html_path,
        )
    except Exception as exc:
        payload = {"status": "error", "error": str(exc), "output": str(output_path)}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1

    if args.metadata_output:
        metadata_path = normalize_output_path(args.metadata_output)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result["metadata_output"] = str(metadata_path)

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Saved {width}x{height} grid to {output_path}")
        if preview_image_path is not None:
            print(f"Preview image: {preview_image_path}")
        if preview_html_path is not None:
            print(f"Preview HTML: {preview_html_path}")
        print(f"Captured {result['captured_cells']} cell(s).")
        if args.return_to_center:
            print("Returned camera to the starting center.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
