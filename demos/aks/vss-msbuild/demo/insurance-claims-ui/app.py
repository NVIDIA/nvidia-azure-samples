# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Insurance Claims Processing — Customer Rep Web UI.

Streamlit app that:
  1. Lists customer policies from policies.json (sidebar table + selector)
  2. Lets a customer rep upload a damage video for the selected policy
  3. PUTs the video to the VSS Agent (AKS) via VST presigned URL
  4. Calls the deployed Microsoft Foundry agent (insurance-claims-triage)
     with the video_id + policy number
  5. Parses the agent's structured response (regex)
  6. Renders the draft claim PDF locally using the parsed fields
  7. Embeds the PDF preview + offers a download button

Run:
    cd demo/insurance-claims-ui
    uv venv .venv && source .venv/bin/activate
    uv pip install -r requirements.txt
    az login                     # for DefaultAzureCredential
    streamlit run app.py
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import streamlit as st
from azure.identity import DefaultAzureCredential

# ---------------------------------------------------------------------------
# Config — env vars seed the defaults; the in-app ⚙️ Settings expander lets a
# user point this UI at a different VSS / hosted-agent deployment for the
# current browser session.
# ---------------------------------------------------------------------------

HERE = Path(__file__).parent
POLICIES_PATH = HERE / "policies.json"
OUTPUT_DIR = HERE / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
FIXTURES_DIR = HERE / "fixtures"

# Offline mode default — env-controlled so a parallel ACA deployment can boot
# straight into offline (when the live VSS AKS cluster is shut down to save
# GPU costs). Falsy by default; "1", "true", "yes" turn it on.
DEFAULT_OFFLINE_MODE = os.environ.get("OFFLINE_MODE", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

DEFAULT_VSS_BASE_URL = os.environ.get(
    "VSS_BASE_URL", ""
).rstrip("/")

DEFAULT_FOUNDRY_ENDPOINT = os.environ.get(
    "FOUNDRY_AGENT_ENDPOINT",
    "",
)

FOUNDRY_TOKEN_SCOPE = "https://ai.azure.com/.default"

HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0)


def _vss_url() -> str:
    """Current VSS base URL — overridable via the in-app ⚙️ Settings expander."""
    return st.session_state.get("vss_base_url", DEFAULT_VSS_BASE_URL).rstrip("/")


def _foundry_endpoint() -> str:
    """Current Foundry hosted-agent /responses endpoint — overridable via Settings."""
    return st.session_state.get("foundry_endpoint", DEFAULT_FOUNDRY_ENDPOINT)


def _validate_live_endpoints() -> None:
    missing = []
    if not _vss_url():
        missing.append("VSS Base URL")
    if not _foundry_endpoint():
        missing.append("Foundry Hosted-Agent Endpoint")
    if missing:
        raise RuntimeError(
            "Set the following service endpoint(s) in Settings or environment "
            f"variables before running live mode: {', '.join(missing)}."
        )

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@st.cache_resource
def get_token_credential() -> DefaultAzureCredential:
    return DefaultAzureCredential()


@st.cache_data
def load_policies() -> list[dict]:
    return json.loads(POLICIES_PATH.read_text())


def upload_video_to_vss(filename: str, video_bytes: bytes) -> str:
    """Get a VST presigned upload URL, PUT the bytes, return the video_id."""
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        r = client.post(f"{_vss_url()}/api/v1/videos", json={"filename": filename})
        r.raise_for_status()
        upload_url = r.json()["url"]
        # VST URL is .../v1/storage/file/<video_id>/<timestamp>; the second-to-last
        # path segment is the canonical video_id the agent will reference.
        video_id = upload_url.rstrip("/").split("/")[-2]
        put = client.put(
            upload_url, content=video_bytes, headers={"Content-Type": "video/mp4"}
        )
        put.raise_for_status()
    return video_id


def _vss_video_exists(video_id: str, timeout: float = 10.0) -> bool:
    """Check if VST already has a video with this canonical `video_id`.

    VSS exposes `GET /vst/api/v1/storage/file/list` which returns a dict keyed
    by sensor_id, with arrays of clips; each clip's `metadata.streamName` is
    the canonical video_id (filename stem). Fail-open: return False on any
    network/parse error so the caller falls through to a real upload.
    """
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
            r = client.get(
                f"{_vss_url()}/vst/api/v1/storage/file/list",
                params={"offset": 0, "limit": 500},
            )
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError):
        return False

    for clips in data.values():
        if not isinstance(clips, list):
            continue
        for clip in clips:
            if (clip.get("metadata") or {}).get("streamName") == video_id:
                return True
    return False


def load_fixture(policy_number: str) -> dict | None:
    """Load the canned agent response for a policy, or None if absent.

    Fixtures are produced by scripts/capture_fixtures.py against the real
    deployed Foundry agent and committed to the repo so offline mode (the
    AKS-GPU-cluster-is-down demo path) can replay them.
    """
    path = FIXTURES_DIR / f"{policy_number}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def upload_video_to_vss_cached(filename: str, video_bytes: bytes) -> tuple[str, str]:
    """Upload to VSS if needed; otherwise reuse an existing video_id.

    Three-tier cache (each step is cheaper / more authoritative than the next):

      1. Session cache  — keyed by sha256(bytes). Same bytes uploaded already
         this session → use the cached video_id. No network.

      2. VSS list check — keyed by filename stem (== VSS canonical video_id,
         which the upstream upload-URL handler derives deterministically from
         the filename). Catches the case where a previous session / container
         already uploaded this file. One GET to /vst/.../file/list.

      3. Fresh upload   — POST /api/v1/videos for the URL, PUT bytes.

    Returns (video_id, source) where source is one of "session-cache",
    "vss-name-match", "fresh-upload".
    """
    cache_key = hashlib.sha256(video_bytes).hexdigest()
    cache: dict = st.session_state.setdefault("vss_uploads", {})

    # 1. Same bytes seen this session?
    if cache_key in cache:
        return cache[cache_key]["video_id"], "session-cache"

    # 2. Server-side existence by filename. VSS's video_id ==
    #    filename.rsplit(".", 1)[0] per the upstream handler.
    candidate_id = filename.rsplit(".", 1)[0] or filename
    if _vss_video_exists(candidate_id):
        cache[cache_key] = {
            "video_id": candidate_id,
            "filename": filename,
            "size_bytes": len(video_bytes),
            "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "vss-name-match",
        }
        return candidate_id, "vss-name-match"

    # 3. Fresh upload.
    video_id = upload_video_to_vss(filename, video_bytes)
    cache[cache_key] = {
        "video_id": video_id,
        "filename": filename,
        "size_bytes": len(video_bytes),
        "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "fresh-upload",
    }
    return video_id, "fresh-upload"


def call_foundry_agent(prompt: str) -> dict:
    """POST to the deployed Foundry agent's /responses endpoint.

    Returns the full Responses-API JSON. The agent's final text is at
    `output[-1].content[0].text` for completed responses.
    """
    token = get_token_credential().get_token(FOUNDRY_TOKEN_SCOPE).token
    payload = {"input": prompt, "stream": False}
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        r = client.post(
            _foundry_endpoint(),
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        r.raise_for_status()
    return r.json()


def extract_agent_text(resp_json: dict) -> str:
    """Pull the assistant's final text message from a Responses API result.

    The response shape is `output: [...]` where each item has `type` and
    `content`. The last item with `type == "message"` carries the user-facing
    text.
    """
    output = resp_json.get("output") or []
    for item in reversed(output):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    return c.get("text", "")
    # Fallback: stringify the whole thing so the user can see what happened.
    return json.dumps(resp_json, indent=2)


# Regexes for the agent's structured reply. The system prompt
# (instructions.md) demands this exact format, so the regexes are stable.
_CLAIM_RE = re.compile(r"Claim drafted:\s*(\S+)", re.IGNORECASE)
_VEH_RE = re.compile(r"Vehicle:\s*(.+?)\s*\(VIN\s*(\S+?)\)", re.IGNORECASE)
_DAMAGE_RE = re.compile(r"Damage:\s*(.+?)(?:\r?\n)", re.IGNORECASE)
_REPAIR_RE = re.compile(r"Estimated repair:\s*\$?([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_DEDUCT_RE = re.compile(r"Deductible:\s*\$?([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_PAYABLE_RE = re.compile(r"Payable:\s*\$?([\d,]+(?:\.\d+)?)", re.IGNORECASE)


# Phrases that strongly suggest the customer uploaded the wrong kind of video —
# something other than a damaged vehicle. We treat any match as a hard stop:
# the UI surfaces a clear "wrong video" message and does NOT generate a PDF.
_REJECT_PHRASES = (
    "no damage",
    "no damage visible",
    "no damage observed",
    "no vehicle",
    "no car",
    "not a car",
    "not a vehicle",
    "not a damage",
    "doesn't show a car",
    "does not show a car",
    "doesn't show a vehicle",
    "does not show a vehicle",
    "doesn't show vehicle damage",
    "does not show vehicle damage",
    "cannot find any damage",
    "cannot identify any damage",
    "unable to identify damage",
    "unable to detect damage",
    "please resubmit",
    "please advise the customer to resubmit",
    "wrong video",
    "invalid video",
)


def validate_claim_for_pdf(claim: dict, agent_text: str) -> tuple[bool, str]:
    """Sanity-check that the agent actually found vehicle damage to claim against.

    Returns (is_valid, reason). When `is_valid` is False, the UI surfaces the
    reason as a "we couldn't process this video" message and skips the PDF.
    """
    text_lower = (agent_text or "").lower()
    for phrase in _REJECT_PHRASES:
        if phrase in text_lower:
            return False, (
                f"VSS / agent indicated the video may not show vehicle damage "
                f"(found phrase: “{phrase}” in the reply)."
            )

    summary = (claim.get("damage_summary") or "").strip()
    repair_total = claim.get("repair_total")

    # Missing both the damage prose AND the cost is a strong signal the agent
    # didn't have enough to work with. (We accept either as evidence; e.g. a
    # genuine claim might have summary + 0-cost if everything is "minor".)
    if not summary and (repair_total is None or repair_total <= 0):
        return False, (
            "The agent returned no damage summary and no repair estimate. "
            "Either the video doesn't show vehicle damage or the agent "
            "couldn't parse the scene."
        )

    return True, ""


def parse_agent_summary(text: str, policy: dict) -> dict:
    """Extract claim fields from the agent's final-format summary."""

    def _money(s: str | None) -> float | None:
        if not s:
            return None
        try:
            return float(s.replace(",", ""))
        except ValueError:
            return None

    return {
        "claim_id": (m.group(1) if (m := _CLAIM_RE.search(text)) else ""),
        "vehicle": (m.group(1) if (m := _VEH_RE.search(text)) else policy["vehicle"]),
        "vin": (m.group(2) if (m := _VEH_RE.search(text)) else policy["vin"]),
        "damage_summary": (m.group(1) if (m := _DAMAGE_RE.search(text)) else ""),
        "repair_total": _money(m.group(1) if (m := _REPAIR_RE.search(text)) else None),
        "deductible": _money(m.group(1) if (m := _DEDUCT_RE.search(text)) else None)
        or float(policy.get("deductible_usd", 0)),
        "payable": _money(m.group(1) if (m := _PAYABLE_RE.search(text)) else None),
    }


def render_claim_pdf(claim: dict, policy: dict, full_agent_text: str) -> Path:
    """Render a one-page PDF from the parsed claim. Returns the file path."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    claim_id = claim.get("claim_id") or "CLM-DRAFT"
    path = OUTPUT_DIR / f"{claim_id}.pdf"

    # Coerce numeric fields to floats with safe defaults — the regex parser
    # returns None when the agent's reply doesn't carry that line, and we
    # can't `:,.2f`-format a None.
    repair_total = float(claim.get("repair_total") or 0)
    deductible = float(claim.get("deductible") or policy.get("deductible_usd", 0) or 0)
    payable = claim.get("payable")
    if payable is None:
        payable = max(repair_total - deductible, 0)
    else:
        payable = float(payable)

    lines = [
        ("Heading", f"INSURANCE CLAIM — DRAFT"),
        ("", ""),
        ("", f"Claim ID:        {claim_id}"),
        ("", f"Date filed:      {datetime.now(timezone.utc).isoformat(timespec='seconds')}"),
        ("", f"Customer:        {policy['customer_name']}"),
        ("", f"Email:           {policy.get('customer_email', '')}"),
        ("", f"Policy:          {policy['policy_number']} ({policy['coverage_tier']})"),
        ("", f"Vehicle:         {claim['vehicle']}"),
        ("", f"VIN:             {claim['vin']}"),
        ("", ""),
        ("Heading", "Damage Summary"),
        ("", claim.get("damage_summary", "(none)")),
        ("", ""),
        ("Heading", "Cost"),
        ("", f"  Estimated repair:  ${repair_total:,.2f}"),
        ("", f"  Deductible:        ${deductible:,.2f}"),
        ("", f"  Payable:           ${payable:,.2f}"),
        ("", ""),
        ("", "Status: DRAFT — pending adjuster review."),
        ("", ""),
        ("Heading", "Agent Trace (verbatim)"),
    ]

    c = canvas.Canvas(str(path), pagesize=LETTER)
    y = 750
    for kind, line in lines:
        font = "Helvetica-Bold" if kind == "Heading" else "Helvetica"
        c.setFont(font, 11 if kind == "Heading" else 10)
        c.drawString(50, y, line[:120])
        y -= 16
        if y < 80:
            c.showPage()
            y = 750
    # Append the agent's verbatim text in a smaller font
    c.setFont("Helvetica", 8)
    for raw in full_agent_text.splitlines():
        # word-wrap-ish: keep lines under 120 chars
        for chunk_start in range(0, len(raw), 120):
            c.drawString(50, y, raw[chunk_start : chunk_start + 120])
            y -= 11
            if y < 60:
                c.showPage()
                c.setFont("Helvetica", 8)
                y = 750
    c.save()
    return path


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Insurance Claims Triage",
    page_icon="🚗",
    layout="wide",
)

st.title("🚗 Insurance Claims Triage")
st.caption(
    "Customer rep portal. Upload a damage video → VSS analyzes it on AKS → "
    "Foundry agent orchestrates policy lookup + cost estimate + claim draft."
)

policies = load_policies()

# ---- Settings (runtime endpoint overrides) ------------------------------
#
# Seed session_state once from the env-var defaults; the text_inputs below
# then read/write through `key=` directly.

if "vss_base_url" not in st.session_state:
    st.session_state.vss_base_url = DEFAULT_VSS_BASE_URL
if "foundry_endpoint" not in st.session_state:
    st.session_state.foundry_endpoint = DEFAULT_FOUNDRY_ENDPOINT
if "offline_mode" not in st.session_state:
    st.session_state.offline_mode = DEFAULT_OFFLINE_MODE


def _clear_upload_cache() -> None:
    """Re-pointing VSS invalidates cached video_ids — they belong to the old host."""
    st.session_state["vss_uploads"] = {}


# Persistent OFFLINE badge in the page header when active — makes it obvious
# at-a-glance that we're replaying canned responses, not calling live VSS /
# Foundry. Cheap visual cue for demos.
if st.session_state.offline_mode:
    st.warning(
        "🧪 **OFFLINE MODE** — VSS upload and Foundry agent calls are mocked. "
        "Responses are replayed from cached fixtures under `fixtures/`.",
        icon="🧪",
    )

with st.expander("⚙️ Service endpoints", expanded=False):
    st.caption(
        "Point this UI at a different VSS / hosted-agent deployment without "
        "redeploying the container. Values persist for this browser session "
        "only. Re-set the VSS URL to clear any cached video uploads from the "
        "previous deployment."
    )
    st.checkbox(
        "🧪 Offline mode — replay canned fixtures, no VSS, no Foundry",
        key="offline_mode",
        help=(
            "When on: skip the VSS upload + the Foundry agent call entirely. "
            "Show the canned response for whichever policy is selected, with "
            "simulated 'thinking' delays. Useful for demos when the AKS GPU "
            "cluster is shut down to save cost. Only policies with bundled "
            "videos AND a fixture under fixtures/ are usable in this mode."
        ),
    )
    st.text_input(
        "VSS Base URL",
        key="vss_base_url",
        on_change=_clear_upload_cache,
        disabled=st.session_state.offline_mode,
        help=(
            "Base URL of your NVIDIA VSS Agent deployment "
            "(e.g. http://vss.<EXTERNAL_HOST>.nip.io). No trailing slash needed."
        ),
    )
    st.text_input(
        "Foundry Hosted-Agent Endpoint",
        key="foundry_endpoint",
        disabled=st.session_state.offline_mode,
        help=(
            "Full /endpoint/protocols/openai/responses URL of the hosted "
            "Foundry agent. Bearer auth via DefaultAzureCredential."
        ),
    )
    cols = st.columns([1, 1, 4])
    with cols[0]:
        if st.button("🔄 Reset to defaults"):
            st.session_state.pop("vss_base_url", None)
            st.session_state.pop("foundry_endpoint", None)
            _clear_upload_cache()
            st.rerun()
    with cols[1]:
        if st.button("🧹 Clear upload cache"):
            _clear_upload_cache()
            st.toast("Cleared in-session VSS upload cache.", icon="🧹")

# ---- Main: claim filing form --------------------------------------------

left, right = st.columns([1, 1])

with left:
    st.subheader("File a claim")
    selected_id = st.selectbox(
        "Customer policy",
        options=[p["policy_number"] for p in policies],
        format_func=lambda pn: next(
            f"{p['policy_number']} — {p['customer_name']} ({p['vehicle']})"
            for p in policies
            if p["policy_number"] == pn
        ),
    )
    selected = next(p for p in policies if p["policy_number"] == selected_id)

    with st.expander("Policy details", expanded=True):
        st.json(selected)

    # If the policy points to a local sample video (path relative to this
    # script), give the user a radio to pick it instead of uploading.
    sample_path: Path | None = None
    sample_rel = selected.get("sample_video")
    if sample_rel:
        candidate = HERE / sample_rel
        if candidate.is_file():
            sample_path = candidate

    # In OFFLINE mode the only path that produces meaningful output is the
    # local-sample + fixture combo, so we hide the upload option entirely.
    # A policy with no local sample video / no fixture surfaces a clear message.
    offline = bool(st.session_state.offline_mode)
    fixture = load_fixture(selected["policy_number"]) if offline else None

    video_choice = "upload"  # default when no local sample exists
    if offline:
        if sample_path is not None and fixture is not None:
            video_choice = "bundled"
            st.info(
                f"🧪 Offline mode — will replay canned response for "
                f"`{selected['policy_number']}` "
                f"(fixture captured {fixture.get('captured_at', 'unknown')[:10]}).",
                icon="🧪",
            )
        else:
            video_choice = "blocked"
            missing = (
                "no local sample video configured" if sample_path is None
                else f"no fixture at fixtures/{selected['policy_number']}.json"
            )
            st.warning(
                f"This policy can't be demoed in offline mode ({missing}). "
                "Add a local sample video for this policy, or turn off offline mode "
                "in ⚙️ Settings."
            )
    elif sample_path is not None:
        video_choice = st.radio(
            "Damage video",
            options=["bundled", "upload"],
            format_func=lambda v: (
                f"📹 Use local sample ({sample_path.name}, "
                f"{sample_path.stat().st_size / 1e6:.1f} MB)"
                if v == "bundled"
                else "📤 Upload my own"
            ),
            horizontal=False,
            index=0,
        )

    video_file = None
    if video_choice == "upload":
        video_file = st.file_uploader(
            "Damage video", type=["mp4", "mov", "m4v"], accept_multiple_files=False
        )

    # Preview whichever video the user is about to submit (bundled file from
    # disk, or the in-memory UploadedFile). Lets the rep eyeball the clip
    # before paying for a VSS analysis pass.
    preview_source = None
    if video_choice == "bundled" and sample_path is not None:
        preview_source = str(sample_path)
    elif video_choice == "upload" and video_file is not None:
        preview_source = video_file

    if preview_source is not None:
        with st.expander("📺 Preview", expanded=True):
            st.video(preview_source)

    # Submit is enabled if either the local sample is selected (in offline
    # mode this requires a fixture too) or a file has been uploaded.
    have_video = (
        (video_choice == "bundled" and (not offline or fixture is not None))
        or (video_file is not None)
    )
    submit = st.button(
        "Submit claim ↗",
        type="primary",
        disabled=not have_video,
        width="stretch",
    )

with right:
    st.subheader("Result")
    placeholder = st.empty()
    if not submit:
        placeholder.info("Fill out the form on the left and submit to file a claim.")

if submit and have_video:
    # Resolve the bytes to send based on which source the user chose.
    if video_choice == "bundled" and sample_path is not None:
        video_name = sample_path.name
        video_bytes = sample_path.read_bytes()
    else:
        # `video_file` is a Streamlit UploadedFile here (the upload branch).
        video_name = video_file.name
        video_bytes = video_file.getvalue()

    with right:
        with st.status("Filing claim…", expanded=True) as status:
            try:
                if offline and fixture is not None:
                    # --- OFFLINE BRANCH ---------------------------------
                    # Skip both upstream calls. Replay the canned response
                    # from the fixture with simulated delays so the UX flow
                    # still feels like a real submission (~5-7s total).
                    st.write(
                        f"🧪 **Offline mode** — replaying canned response for "
                        f"`{selected['policy_number']}`."
                    )
                    st.write(f"🔍 Checking VSS for **{video_name}**…")
                    time.sleep(1.5)
                    video_id = fixture["video_id"]
                    st.success(
                        f"♻️ (simulated) Found existing video in VSS: "
                        f"video_id = `{video_id}`"
                    )
                    prompt = fixture["prompt"]
                    st.write("🤖 Sending this prompt to the Foundry agent:")
                    st.code(prompt, language="text")
                    with st.spinner(
                        "Agent thinking (simulated — real run takes ~30-60s)…"
                    ):
                        time.sleep(5.0)
                    agent_text = fixture["agent_text"]
                    raw_resp = fixture.get("raw_response", {})
                    st.write("✅ Agent finished (canned). Parsing reply…")
                else:
                    # --- LIVE BRANCH ------------------------------------
                    _validate_live_endpoints()
                    st.write(f"🔍 Checking VSS for **{video_name}**…")
                    video_id, source = upload_video_to_vss_cached(
                        video_name, video_bytes
                    )
                    if source == "session-cache":
                        st.success(
                            f"♻️ Reusing prior upload (session cache): "
                            f"video_id = `{video_id}`"
                        )
                    elif source == "vss-name-match":
                        st.success(
                            f"♻️ Found existing video in VSS by filename: "
                            f"video_id = `{video_id}`"
                        )
                    else:
                        st.success(f"📤 Uploaded fresh — video_id = `{video_id}`")

                    prompt = (
                        f"A customer just submitted a damage video. "
                        f"video_id={video_id}. "
                        f"If the VIN is not visible, use policy "
                        f"{selected['policy_number']} as a fallback. "
                        f"Run the triage workflow."
                    )
                    st.write("🤖 Sending this prompt to the Foundry agent:")
                    st.code(prompt, language="text")
                    with st.spinner("Agent thinking (VSS analysis dominates ~30-60s)…"):
                        raw_resp = call_foundry_agent(prompt)

                    agent_text = extract_agent_text(raw_resp)
                    st.write("✅ Agent finished. Parsing reply…")

                claim = parse_agent_summary(agent_text, selected)

                # Reject videos that don't show vehicle damage — skip PDF.
                is_valid, reject_reason = validate_claim_for_pdf(claim, agent_text)
                pdf_path = None
                if not is_valid:
                    status.update(
                        label="Video rejected — not a vehicle-damage clip.",
                        state="error",
                    )
                else:
                    pdf_path = render_claim_pdf(claim, selected, agent_text)
                    status.update(
                        label=f"Done — claim {claim.get('claim_id') or 'DRAFT'} drafted.",
                        state="complete",
                    )
            except httpx.HTTPStatusError as e:
                status.update(label="HTTP error from upstream service.", state="error")
                st.error(
                    f"Upstream returned {e.response.status_code}:\n```\n"
                    f"{e.response.text[:1000]}\n```"
                )
                st.stop()
            except Exception as e:
                status.update(label="Unexpected error.", state="error")
                st.exception(e)
                st.stop()

        # ---- Display results ------------------------------------------------
        if not is_valid:
            st.error(
                "⚠ **This video doesn't appear to show vehicle damage.**\n\n"
                f"{reject_reason}\n\n"
                "Please upload a clear video of the damaged vehicle — "
                "ideally a slow walk-around with the camera focused on each "
                "affected panel."
            )

        st.markdown("### Prompt sent to agent")
        st.code(prompt, language="text")

        st.markdown("### Agent reply")
        st.code(agent_text, language="text")

        st.markdown("### Parsed claim fields")
        st.json(claim)

        if not is_valid:
            # Skip PDF rendering entirely — show the reason instead.
            st.info(
                "No draft PDF was generated because the video failed validation. "
                "Adjust the upload and resubmit to try again."
            )
            st.stop()

        st.markdown("### Draft PDF")
        pdf_bytes = pdf_path.read_bytes()
        st.download_button(
            "⬇ Download PDF",
            data=pdf_bytes,
            file_name=pdf_path.name,
            mime="application/pdf",
            width="stretch",
        )
        b64 = base64.b64encode(pdf_bytes).decode()
        st.markdown(
            f'<iframe src="data:application/pdf;base64,{b64}" '
            f'width="100%" height="700" type="application/pdf"></iframe>',
            unsafe_allow_html=True,
        )
