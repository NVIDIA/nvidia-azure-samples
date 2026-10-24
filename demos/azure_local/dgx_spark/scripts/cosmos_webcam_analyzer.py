#!/usr/bin/env python3
"""Continuously analyze the live webcam stream with NVIDIA Cosmos-Reason."""

from __future__ import annotations

import argparse
import base64
from io import BytesIO
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request
from urllib.request import urlopen

from llm_token_usage import record_response_usage, record_token_usage


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEGACY_HF_HOME = Path("/home/anslutsky/Dev/Cosmos-transfer/.cache/huggingface")


DEFAULT_PROMPT = (
    "Return exactly three Markdown bullets, each under 14 words. "
    "Cover: visible people/objects, current activity, and unusual or safety-relevant details. "
    "No intro or extra commentary."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-url", default="http://127.0.0.1:8090/snapshot.jpg")
    parser.add_argument("--output-json", default=str(PROJECT_ROOT / "webcam-analysis.json"))
    parser.add_argument("--trigger-json", default=None, help="Optional JSON file used to request analyses")
    parser.add_argument(
        "--trigger-only",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Analyze only when trigger-json changes",
    )
    parser.add_argument("--clips-dir", default=str(PROJECT_ROOT / "webcam-analysis-clips"))
    parser.add_argument("--model", default="nvidia/Cosmos-Reason1-7B")
    parser.add_argument("--backend", choices=("cosmos", "ollama_omni", "vllm_omni"), default="cosmos")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model-api-runtime", choices=("ollama", "vllm"), default="ollama")
    parser.add_argument("--ollama-openai-path", default="/v1/chat/completions")
    parser.add_argument("--ollama-timeout", type=float, default=90.0)
    parser.add_argument("--ollama-keep-alive", default="60m")
    parser.add_argument("--ollama-image-max-width", type=int, default=960)
    parser.add_argument("--ollama-image-jpeg-quality", type=int, default=72)
    parser.add_argument("--clip-seconds", type=float, default=5.0)
    parser.add_argument("--sample-fps", type=float, default=1.0)
    parser.add_argument("--loop-delay", type=float, default=1.0)
    parser.add_argument("--model-video-fps", type=float, default=1.0)
    parser.add_argument("--max-pixels", type=int, default=640 * 360)
    parser.add_argument("--max-new-tokens", type=int, default=180)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--local-files-only", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--device-map", default="auto")
    parser.add_argument("--once", action="store_true", help="Analyze one clip and exit")
    return parser.parse_args()


def publish(path: str | Path, payload: dict) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": time.time(), **payload}
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(output_path)


def read_json(path: str | Path | None) -> dict:
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def fetch_snapshot(url: str, output_path: Path, timeout: float = 5.0) -> None:
    try:
        with urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError(f"snapshot endpoint returned HTTP {response.status}")
            data = response.read()
    except URLError as exc:
        raise RuntimeError(f"could not fetch snapshot: {exc}") from exc

    if not data.startswith(b"\xff\xd8"):
        raise RuntimeError("snapshot endpoint did not return a JPEG frame")
    output_path.write_bytes(data)


def collect_frames(args: argparse.Namespace, frame_dir: Path, snapshot_url: str) -> list[Path]:
    frame_count = max(1, round(args.clip_seconds * args.sample_fps))
    interval = 1.0 / max(args.sample_fps, 0.01)
    frame_paths = []
    for index in range(frame_count):
        start = time.monotonic()
        frame_path = frame_dir / f"frame_{index:04d}.jpg"
        fetch_snapshot(snapshot_url, frame_path)
        frame_paths.append(frame_path)
        elapsed = time.monotonic() - start
        if index < frame_count - 1:
            time.sleep(max(0.0, interval - elapsed))
    return frame_paths


def encode_clip(args: argparse.Namespace, frame_dir: Path, clip_path: Path) -> None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-framerate",
        str(args.sample_fps),
        "-i",
        str(frame_dir / "frame_%04d.jpg"),
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(clip_path),
    ]
    subprocess.run(cmd, check=True)


def collect_clip(args: argparse.Namespace, clip_path: Path, snapshot_url: str) -> int:
    with tempfile.TemporaryDirectory(prefix="cosmos-webcam-frames-") as tmp:
        frame_dir = Path(tmp)
        frame_paths = collect_frames(args, frame_dir, snapshot_url)
        encode_clip(args, frame_dir, clip_path)
    return len(frame_paths)


def load_model(args: argparse.Namespace):
    import torch
    from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

    model_path = resolve_model_path(args.model, args.local_files_only)
    device_map = normalize_device_map(args.device_map)
    processor = AutoProcessor.from_pretrained(
        model_path,
        local_files_only=args.local_files_only,
        use_fast=True,
    )
    model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
        model_path,
        torch_dtype=torch.bfloat16 if torch.cuda.is_available() else torch.float32,
        device_map=device_map,
        local_files_only=args.local_files_only,
        low_cpu_mem_usage=True,
    )
    model.eval()
    return processor, model


def normalize_device_map(device_map: str):
    if device_map.lower() in {"none", "false", "off"}:
        return None
    if device_map in {"cuda", "cuda:0", "cpu"}:
        return {"": device_map}
    return device_map


def default_hf_home() -> Path:
    worktree_hf_home = PROJECT_ROOT / ".cache" / "huggingface"
    if worktree_hf_home.exists():
        return worktree_hf_home
    if LEGACY_HF_HOME.exists():
        return LEGACY_HF_HOME
    return worktree_hf_home


def resolve_model_path(model: str, local_files_only: bool) -> str:
    model_path = Path(model).expanduser()
    if model_path.exists():
        return str(model_path)
    if not local_files_only or "/" not in model:
        return model

    hf_home = Path(os.environ.get("HF_HOME", str(default_hf_home())))
    cache_dir = hf_home / "hub" / f"models--{model.replace('/', '--')}"
    ref_path = cache_dir / "refs" / "main"
    snapshots_dir = cache_dir / "snapshots"
    required = ("config.json", "preprocessor_config.json", "tokenizer.json")

    def is_complete_snapshot(path: Path) -> bool:
        return all((path / name).exists() for name in required) and any(path.glob("*.safetensors"))

    if ref_path.exists():
        snapshot = snapshots_dir / ref_path.read_text(encoding="utf-8").strip()
        if snapshot.exists() and is_complete_snapshot(snapshot):
            return str(snapshot)

    snapshots = sorted((path for path in snapshots_dir.glob("*") if path.is_dir()), key=lambda path: path.stat().st_mtime)
    complete_snapshots = [path for path in snapshots if is_complete_snapshot(path)]
    if complete_snapshots:
        return str(complete_snapshots[-1])
    if snapshots:
        return str(snapshots[-1])
    return model


def prompt_for_trigger(base_prompt: str, trigger_reason: str) -> str:
    reason = str(trigger_reason or "").strip()
    query = reason.split(":", 1)[1].strip() if reason.startswith("voice_query:") else ""
    if not query:
        return base_prompt
    return (
        f"User visual question: {query}\n"
        "Answer that question first using the video frames. "
        "If the requested detail is not visible, say so. "
        "Then provide up to three short Markdown bullets for situational context. "
        "No intro or extra commentary."
    )


def compact_image_bytes(data: bytes, max_width: int, jpeg_quality: int) -> tuple[bytes, dict]:
    info = {"original_bytes": len(data), "bytes": len(data), "resized": False}
    try:
        from PIL import Image

        image = Image.open(BytesIO(data))
        info["original_size"] = list(image.size)
        max_width = max(128, int(max_width or 960))
        if image.width > max_width:
            ratio = max_width / float(image.width)
            target = (max_width, max(1, int(image.height * ratio)))
            image = image.resize(target, Image.Resampling.LANCZOS)
            info["resized"] = True
        if image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        output = BytesIO()
        image.save(output, format="JPEG", quality=max(35, min(95, int(jpeg_quality or 72))), optimize=True)
        compact = output.getvalue()
        info["bytes"] = len(compact)
        info["size"] = list(image.size)
        return compact, info
    except Exception as exc:
        info["compact_error"] = str(exc)
        return data, info


def image_data_url(path: Path, args: argparse.Namespace | None = None) -> tuple[str, dict]:
    data = path.read_bytes()
    if args is not None:
        data, info = compact_image_bytes(data, args.ollama_image_max_width, args.ollama_image_jpeg_quality)
    else:
        info = {"original_bytes": len(data), "bytes": len(data), "resized": False}
    return "data:image/jpeg;base64," + base64_encode(data), info


def base64_encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def extract_text_response(data: object) -> str:
    if isinstance(data, dict):
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else {}
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
                reasoning = message.get("reasoning")
                if isinstance(reasoning, str) and reasoning.strip():
                    return reasoning.strip()
        for key in ("response", "text", "content", "reasoning"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in data.values():
            nested = extract_text_response(value)
            if nested:
                return nested
    if isinstance(data, list):
        for item in data:
            nested = extract_text_response(item)
            if nested:
                return nested
    return ""


def analyze_frames_ollama_omni(args: argparse.Namespace, frame_paths: list[Path], prompt: str | None = None) -> str:
    prompt_text = prompt or args.prompt
    content = [
        {
            "type": "text",
            "text": (
                "/no_think\n"
                "Directly analyze the attached ordered webcam frames as live video/snapshot evidence. "
                "Return only the requested answer; do not include hidden reasoning or preface text.\n\n"
                f"{prompt_text}"
            ),
        }
    ]
    selected_frames = frame_paths[:1] if args.model_api_runtime == "vllm" else frame_paths
    for path in selected_frames:
        data_url, _image_info = image_data_url(path, args)
        content.append({"type": "image_url", "image_url": {"url": data_url}})
    payload = {
        "model": args.model,
        "stream": False,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max(64, int(args.max_new_tokens)),
        "temperature": 0.2 if args.model_api_runtime == "vllm" else 0,
    }
    if args.model_api_runtime == "vllm":
        payload.update({"top_k": 1, "chat_template_kwargs": {"enable_thinking": False}})
    else:
        payload.update(
            {
                "think": False,
                "keep_alive": str(args.ollama_keep_alive or "60m"),
            }
        )
    request = Request(
        args.ollama_url.rstrip("/") + args.ollama_openai_path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=args.ollama_timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:800]
        raise RuntimeError(f"Ollama Omni HTTP {exc.code}: {detail}") from exc
    record_response_usage(
        data,
        component="cosmos",
        model=str(args.model),
        provider=args.model_api_runtime,
        source="visual_analysis",
        metadata={"endpoint": args.ollama_openai_path, "frame_count": len(selected_frames)},
    )
    text = extract_text_response(data)
    if not text:
        raise RuntimeError("Ollama Omni returned no text")
    return text.strip()


def analyze_clip(args: argparse.Namespace, processor, model, clip_path: Path, prompt: str | None = None) -> str:
    import torch
    from qwen_vl_utils import process_vision_info

    prompt_text = prompt or args.prompt
    messages = [
        {
            "role": "system",
            "content": "Always respond in English. You are analyzing a live webcam video for concise situational awareness.",
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "video",
                    "video": str(clip_path),
                    "fps": args.model_video_fps,
                    "max_pixels": args.max_pixels,
                },
                {"type": "text", "text": prompt_text},
            ],
        },
    ]

    text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    image_inputs, video_inputs = process_vision_info(messages)
    inputs = processor(
        text=[text],
        images=image_inputs,
        videos=video_inputs,
        padding=True,
        return_tensors="pt",
    )

    if hasattr(model, "device"):
        inputs = inputs.to(model.device)
    elif torch.cuda.is_available():
        inputs = inputs.to("cuda")

    with torch.inference_mode():
        generated_ids = model.generate(
            **inputs,
            max_new_tokens=args.max_new_tokens,
            do_sample=False,
            repetition_penalty=1.05,
        )

    trimmed_ids = [
        output_ids[len(input_ids) :]
        for input_ids, output_ids in zip(inputs.input_ids, generated_ids, strict=False)
    ]
    input_tokens = int(inputs.attention_mask.sum().item()) if hasattr(inputs, "attention_mask") else int(inputs.input_ids.numel())
    output_tokens = sum(int(output_ids.numel()) for output_ids in trimmed_ids)
    record_token_usage(
        component="cosmos",
        model=str(args.model),
        provider="transformers",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        source="visual_analysis",
        usage_source="generated_tensor_lengths",
        metadata={"backend": "cosmos"},
    )
    return processor.batch_decode(trimmed_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0].strip()


def main() -> int:
    args = parse_args()
    clips_dir = Path(args.clips_dir)
    clips_dir.mkdir(parents=True, exist_ok=True)

    if "HF_HOME" not in os.environ:
        os.environ["HF_HOME"] = str(default_hf_home())

    previous_output = read_json(args.output_json)
    last_result = previous_output if previous_output.get("answer") else {}

    backend_label = "Nemotron 3 Nano Omni" if args.backend in {"ollama_omni", "vllm_omni"} else "NVIDIA Cosmos-Reason"

    publish(
        args.output_json,
        {
            "status": "loading",
            "model": args.model,
            "backend": args.backend,
            "message": f"Loading {backend_label} visual analyzer.",
        },
    )

    processor = None
    model = None
    if args.backend == "cosmos":
        try:
            processor, model = load_model(args)
        except Exception as exc:
            publish(
                args.output_json,
                {"status": "error", "model": args.model, "backend": args.backend, "error": f"model load failed: {exc}"},
            )
            raise

    publish(
        args.output_json,
        {
            "status": "running",
            "model": args.model,
            "backend": args.backend,
            "message": f"{backend_label} ready. Waiting for the first webcam observation.",
            "trigger_mode": "request" if args.trigger_json and args.trigger_only else "continuous",
        },
    )

    clip_index = 0
    last_trigger_id = ""
    while True:
        trigger = read_json(args.trigger_json)
        trigger_id = str(trigger.get("request_id") or "")
        source_id = str(trigger.get("source_id") or "")
        source_label = str(trigger.get("source_label") or "")
        snapshot_url = str(trigger.get("snapshot_url") or args.snapshot_url)
        if args.trigger_json and args.trigger_only and (not trigger_id or trigger_id == last_trigger_id):
            waiting_payload = {
                "status": "ready" if last_result.get("answer") else "waiting",
                "model": args.model,
                "backend": args.backend,
                "message": (
                    f"Latest {backend_label} analysis ready. Waiting for the next environment-agent trigger."
                    if last_result.get("answer")
                    else f"{backend_label} ready. Waiting for an environment-agent trigger."
                ),
                "trigger_mode": "request",
                "last_trigger_id": last_trigger_id,
            }
            if last_result.get("answer"):
                waiting_payload = {
                    **last_result,
                    **waiting_payload,
                    "previous_result_updated_at": last_result.get("updated_at"),
                }
            publish(args.output_json, waiting_payload)
            time.sleep(max(args.loop_delay, 0.5))
            continue

        clip_index += 1
        clip_path = clips_dir / f"webcam_clip_{clip_index:06d}.mp4"
        started = time.time()

        try:
            publish(
                args.output_json,
                {
                    "status": "capturing",
                    "model": args.model,
                    "backend": args.backend,
                    "message": f"Capturing triggered webcam frames for {backend_label} analysis.",
                    "trigger_id": trigger_id,
                    "source_id": source_id,
                    "source_label": source_label,
                    "snapshot_url": snapshot_url,
                    "trigger_requested_at": trigger.get("requested_at"),
                    "trigger_reason": trigger.get("reason", ""),
                },
            )
            with tempfile.TemporaryDirectory(prefix="cosmos-webcam-frames-") as tmp:
                frame_dir = Path(tmp)
                frame_paths = collect_frames(args, frame_dir, snapshot_url)
                sampled_frames = len(frame_paths)
                encode_clip(args, frame_dir, clip_path)
                prompt_text = prompt_for_trigger(args.prompt, str(trigger.get("reason") or ""))
                publish(
                    args.output_json,
                    {
                        "status": "analyzing",
                        "model": args.model,
                        "backend": args.backend,
                        "message": f"Running {backend_label} on triggered webcam frames.",
                        "trigger_id": trigger_id,
                        "source_id": source_id,
                        "source_label": source_label,
                        "snapshot_url": snapshot_url,
                        "clip_path": str(clip_path),
                        "sampled_frames": sampled_frames,
                        "analysis_prompt": prompt_text,
                        "ollama_image_max_width": args.ollama_image_max_width,
                        "ollama_image_jpeg_quality": args.ollama_image_jpeg_quality,
                    },
                )
                if args.backend in {"ollama_omni", "vllm_omni"}:
                    answer = analyze_frames_ollama_omni(args, frame_paths, prompt_text)
                else:
                    answer = analyze_clip(args, processor, model, clip_path, prompt_text)
            latency = time.time() - started
            latest_clip = clips_dir / "latest.mp4"
            shutil.copyfile(clip_path, latest_clip)
            last_result = {
                "status": "running",
                "model": args.model,
                "backend": args.backend,
                "answer": answer,
                "clip_path": str(latest_clip),
                "latency_seconds": round(latency, 2),
                "sampled_frames": sampled_frames,
                "clip_seconds": args.clip_seconds,
                "trigger_id": trigger_id,
                "source_id": source_id,
                "source_label": source_label,
                "snapshot_url": snapshot_url,
                "trigger_requested_at": trigger.get("requested_at"),
                "trigger_reason": trigger.get("reason", ""),
                "analysis_prompt": prompt_text,
                "ollama_image_max_width": args.ollama_image_max_width,
                "ollama_image_jpeg_quality": args.ollama_image_jpeg_quality,
            }
            publish(args.output_json, last_result)
            if trigger_id:
                last_trigger_id = trigger_id
        except Exception as exc:
            publish(
                args.output_json,
                {
                    "status": "error",
                    "model": args.model,
                    "backend": args.backend,
                    "error": str(exc),
                    "clip_path": str(clip_path),
                    "trigger_id": trigger_id,
                    "source_id": source_id,
                    "source_label": source_label,
                    "snapshot_url": snapshot_url,
                },
            )
            if args.once:
                return 1
            time.sleep(max(args.loop_delay, 3.0))
            continue

        # Keep storage bounded while preserving the latest few clips for inspection.
        old_clips = sorted(clips_dir.glob("webcam_clip_*.mp4"))[:-5]
        for old_clip in old_clips:
            old_clip.unlink(missing_ok=True)

        if args.once:
            return 0

        time.sleep(args.loop_delay)


if __name__ == "__main__":
    raise SystemExit(main())
