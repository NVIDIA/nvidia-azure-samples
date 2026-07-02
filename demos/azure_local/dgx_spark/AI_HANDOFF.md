# AI continuation handoff: DGX Spark local multimodal monitor

This document is optimized for another coding agent. It records current truths,
historical reversals, and the reasons behind non-obvious constraints. It is not
an operator tutorial.

## Bootstrap protocol

1. Read `AGENTS.md` and `docs/ai-decision-ledger.yaml` before proposing changes.
2. Run `git status --short --branch`; assume unrelated dirty files are live
   process output and belong to the user.
3. Inspect the exact current implementation and focused tests named by a ledger
   entry. Thread summaries are historical evidence, not stronger than code.
4. Diagnose from lane-scoped state. Shared top-level state files can be
   overwritten by another worker's idle update.
5. Make the smallest coherent source/test change. Restart only affected services.
6. Validate through the real queue/tool/evidence/playback path when behavior is
   hardware dependent. A healthy endpoint alone is not end-to-end proof.
7. Stage exact paths. Update this handoff and the decision ledger when the design
   changes.

Authority order for conflicts:

1. Latest explicit user instruction.
2. Current committed code plus focused tests.
3. Active entries in `docs/ai-decision-ledger.yaml`.
4. This narrative.
5. Old thread conclusions and old docs.
6. Runtime JSON/logs, which are observations unless explicitly listed as
   persisted configuration.

## Repository and Git state at handoff

- Working directory: `demos/azure_local/dgx_spark` inside the repository rooted
  at `/home/anslutsky/Dev/nvidia-azure-samples`.
- Branch at creation: `azure_dgx_spark_demo`.
- Historical pre-sanitization handoff commit: `535a4c9d` (`why these files are
  sailient`). The clean PR branch intentionally does not carry that commit's
  ancestry because the older history contained multi-gigabyte model/cache blobs,
  logs, runtime state, and media. The source decisions were preserved in this
  document and the ledger instead.
- The branch had no configured upstream when this handoff was created.
- The checkout is a live runtime and is intentionally dirty.
- A historical import committed thousands of bytecode/runtime-like files. Do not
  attempt a mass cleanup as part of feature work. Do not infer that a tracked
  runtime file should be included in a new commit merely because Git tracks it.
- `deepstream-yolo-coco/DeepStream-Yolo` behaves as a nested gitlink/checkout but
  has no usable mapping in the parent `.gitmodules`. It contains generated model
  and build outputs. Handle it as a separate repository only with explicit user
  direction.

At handoff, the intended source change was already committed. Logs, runtime JSON,
SQLite, bytecode, snapshots, clips, and audio remained uncommitted.

## Current process architecture

The active conversational architecture is one persistent lane worker per input
lane, sharing a dashboard/server and model services:

```text
USB/server microphone ----> voicechat-server --+
                                                +--> Nemotron Omni/vLLM --> tools --> Piper --> server audio
Wi-Fi camera audio --------> voicechat-wifi ----+                               `--> Wi-Fi camera talkback

DeepStream/autofocus completion --(normal Wi-Fi lane input queue)--> voicechat-wifi
Current Time publisher ----------(lane input queue)----------------> enabled running lane worker(s)
Typed lane input ----------------(same lane queue semantics)--------> selected lane worker

webcam_stream_server.py: dashboard, camera/audio endpoints, persisted UI settings,
                         queue ingress, frozen tool-media serving
nemotron_voicechat_pipeline.py: ASR/turn boundary, lane history, routing, answer, TTS/playback
nemotron_voice_responder.py: tool planner/executor implementations
```

The obsolete `deepstream-nemotron` worker must remain absent from the default
service registry. It was a second history-less agent and caused autofocus events
to bypass the ongoing Wi-Fi conversation.

Services observed running when this file was created:

```text
stream
nemotron-omni
dedicated-asr
voicechat-server
voicechat-wifi
```

This is a timestamped observation, not a startup guarantee. Check with:

```bash
.venv/bin/python scripts/webcam_background_stack.py status \
  --only-service stream \
  --only-service nemotron-omni \
  --only-service dedicated-asr \
  --only-service voicechat-server \
  --only-service voicechat-wifi
```

Restart narrowly, for example:

```bash
.venv/bin/python scripts/webcam_background_stack.py restart --only-service stream
.venv/bin/python scripts/webcam_background_stack.py restart --only-service voicechat-wifi
```

Do not copy credentials from process command lines into docs, tests, or commits.
Credential values come from an external secrets environment file; preserve that
indirection.

## Source-of-truth file map

Core code:

- `scripts/webcam_stream_server.py`: dashboard HTML/JS, HTTP(S) endpoints,
  Current Time publisher, lane prompt API, queue ingress, frozen attachment
  serving, camera/audio/talkback integration.
- `scripts/nemotron_voicechat_pipeline.py`: lane worker, ASR and utterance
  boundaries, conversation history, model calls, trusted-service routing,
  evidence handling, TTS and playback.
- `scripts/nemotron_voice_responder.py`: general planner and tool execution,
  including snapshot and camera clip capture.
- `scripts/webcam_background_stack.py`: supported service lifecycle and generated
  command lines.
- `scripts/tool_planner_config.py`: planner template validation/rendering.
- `scripts/nemotron_dialog_config.py`: persistent model-input-window setting.
- `scripts/llm_token_usage.py`: exact/estimated usage extraction and storage.

User-owned persisted configuration:

- `webcam-nemotron-server-system-prompt.json`: server lane prompt.
- `webcam-nemotron-wifi-system-prompt.json`: Wi-Fi lane prompt. It currently
  requires a five-second Wi-Fi camera clip with audio for every Current Time
  notification and forbids snapshot/web substitution.
- `webcam-tool-planner-template.json`: shared planner contract template.
- `webcam-current-time-skill-settings.json`: enabled lanes and independently
  redrawn `U(60s, 300s)` schedules. Code default is server-only. The live file
  may intentionally enable Wi-Fi too.
- `webcam-nemotron-dialog-settings.json`: hot-reloaded retained-history/model
  input window. Missing file means 32768 tokens; valid values are 1024..32768 in
  1024-token steps.
- `webcam-component-audio-settings.json`, `webcam-asr-settings.json`, and
  `webcam-speech-pipeline-mode.json`: live controls. Read before diagnosing muted
  input/output.

Runtime/observational data, not source:

- `webcam-voicechat-response.json`, `webcam-voicechat-history.json`,
  `webcam-tool-planner-queue.json`, `webcam-runtime-stats.json`, monitor/state
  JSON, logs, PID files, SQLite databases, `__pycache__`, `.pyc`, snapshots,
  clips, WAV/ALAW files, model weights/engines, and DeepStream build products.
- These may be tracked due to repository history. Keep them out of ordinary
  commits anyway.

## Non-negotiable current behavior

### Lane prompts

- Server and Wi-Fi have independent files. Never restore the old shared
  `webcam-nemotron-system-prompt.json` design.
- Files are the sole authority. Dashboard load is read-only; retry after a
  restart is read-only; only explicit Save writes.
- Do not let browser local storage, startup/rebuild, manual text submission, or
  request payloads overwrite a lane prompt.
- Prompt formatting is lossless: line breaks, blank lines, indentation,
  repeated spaces, and trailing newlines round-trip. The 3000-character cap is
  intentional.
- The current user constraint from the clip work was: put behavioral prompt
  wording only in the existing Wi-Fi system prompt; do not create additional
  prompts to duplicate it.

### Service inputs and history

- Current Time flow is `publisher -> lane queue -> lane worker -> Nemotron ->
  response`. The first implementation wrote directly to history; that was
  explicitly rejected.
- Time service entries are user-side service turns labeled `Time service`, not
  Nemotron turns. Preserve one durable input turn, not duplicate ingestion-stage
  records.
- Raw Live Stream and other service inputs must remain visible even when
  suppressed, delayed, repeated, or answered with no model turn. Raw structured
  payload UI is collapsible and collapsed by default.
- Only lanes with active workers should receive queued scheduled events.
- Each enabled lane independently redraws its next delay uniformly between one
  and five minutes after firing. Default eligibility is server only; persisted
  user configuration may differ.

### Autofocus and visual event routing

- Completed autofocus events enter the normal Wi-Fi lane queue and retain that
  lane's history/environment context.
- Generic detector chatter remains suppressed; do not turn every detection into
  a conversational event.
- Do not re-enable `deepstream_nemotron_worker.py` in default startup as a second
  agent. Its old context-free behavior was deliberately retired.
- Visual claims must be grounded in the new crop/tool result, not stale lane
  imagery.

### Tool planning policy

- General/manual tool planning is model-contract driven. Required argument
  schemas live in the planner contract. In particular, `web_search` requires a
  non-empty query and timestamp-news queries must copy the full supplied
  `YYYY-MM-DD`; year-only/partial dates are invalid.
- Do not restore generic deterministic query synthesis, argument repair, or
  query rewriting. The user explicitly rejected those as deterministic hacks.
- Narrow later exception (`D-ROUTE-006`): trusted service policy can enforce an explicit
  camera-video directive already present in the lane system prompt. Function
  `plan_trusted_service_tools()` recognizes an explicit N-second video/clip
  instruction and preserves `camera_clip`; this exists because Omni repeatedly
  substituted `current_snapshot` or no tool and then hallucinated visual claims.
  Do not generalize this validator to arbitrary policy text or ordinary user
  input without explicit approval.
- The resident Omni router is used for trusted Current Time events and explicit
  manual camera requests; the small `nemotron-mini` planner previously selected
  web search for an explicit video request.
- Planner policy/input and mandatory routing rules must remain early in the
  prompt because the small planner runs with a constrained context and previously
  truncated decisive instructions.

### Camera evidence

- `current_snapshot` persists the exact captured JPEG under the tool-snapshot
  directory and exposes a constrained stable URL. Do not attach the live camera
  endpoint as though it were the captured frame.
- `camera_clip` supports server/Wi-Fi/bulb, clamps duration to 1..10 seconds,
  defaults to 3 seconds, persists MP4, sends it as `video_url` evidence, and
  renders it in Human Dialog. Common aliases `duration` and `seconds` are
  normalized to `duration_seconds`.
- MJPEG input cadence matters. Wi-Fi is approximately 5 FPS and server is 8 FPS
  for clip capture. Letting FFmpeg assume 25 FPS caused a requested five-second
  clip to take roughly 25 seconds and time out.
- Optional clip audio uses the lane audio endpoint and AAC. The live Wi-Fi
  acceptance test produced exactly 5.000 seconds of H.264 plus 5.000 seconds of
  AAC. Media outputs are validation artifacts, never commit candidates.
- No returned evidence means no visual claim. Preserve this boundary even if a
  fluent answer would otherwise sound plausible.

### Context and token accounting

- The Nemotron Dialog slider is persistent and hot-read. It controls the actual
  retained history budget, not merely display metadata.
- Effective context is bounded by model capacity. Response reserve and current
  system/context text are deducted before retained history.
- Tool output labels display the exact input tokens used by the decision-model
  call that selected the tool. Do not confuse tool execution payload size with
  decision-model input tokens.
- Relevant functions: `configured_answer_context_window()`,
  `conversation_history_token_budget()`, planner usage extraction, and the
  dashboard's `toolOutputLabel()`.

### ASR, loop guard, and playback

- Enforce the 18-second utterance hard cap after every appended audio chunk,
  including chunks still classified as speech. The old path accumulated
  44..729-second Wi-Fi buffers; dedicated ASR rejects over 30 seconds.
- Decode and attribute upstream ASR HTTP errors as Dedicated ASR errors. Do not
  relabel them as Omni failures.
- The acoustic loop guard is application-level. A `listen` result intentionally
  suppresses response and tool calls. There is currently no supported disable
  flag; disabling dedicated ASR is not an equivalent workaround.
- Empty response text can be intentional during tool selection or loop-guard
  suppression. Do not add a broad `I heard: ...` recovery that turns those states
  into repeat-back behavior.
- Wi-Fi talkback retries are allowed only for definite pre-playback
  `CLIENT_LoginEx2` failures. Release stale sessions between bounded retries.
  Never retry an ambiguous post-send failure, which could duplicate speech.
- If model text exists but no sound is heard, separately inspect TTS generation,
  selected output target, component audio controls, and lane-scoped playback
  result. Hearing/model generation and camera playback are different boundaries.

### Dashboard resilience

- Prompt editor reads retry after server restart, but retries must never write or
  replace unsaved edits.
- Planner Details has a server-rendered initial template/path. Network fetch is a
  refresh path, not a prerequisite for a truthful initial `ready` state. This was
  added because HTTP 200 followed by a client exception falsely showed
  `load failed`.
- The Agent Stack separator freezes its starting geometry, positions only the
  left track in pixels, and keeps the Human/Nemotron track fluid so it always
  consumes the remaining width. Updates run at most once per animation frame.
  Never fix both tracks in pixels: that leaves unused space to the right on a
  wide Agent Stack. Do not feed each measured width back into new `fr` weights
  either; unequal minimum widths make that feedback visibly jump.
- UI or endpoint code is loaded in-process; restart `stream` after changing it
  and refresh the browser. Distinguish stale browser JavaScript from stale server
  process code before changing logic.

## Verification strategy

Use `.venv`, which contained pytest 8.4.2 and xdist 3.8.0 at handoff:

```bash
.venv/bin/python -m pytest -n auto -q \
  tests/test_ai_handoff.py \
  tests/test_nemotron_dialog_config.py \
  tests/test_current_time_skill_publisher.py \
  tests/test_voicechat_fast_dialog.py \
  tests/test_system_prompt_persistence.py \
  tests/test_tool_planner_template.py \
  tests/test_nemotron_omni_migration.py
```

Add the smallest domain-specific test file for the component being changed. Also:

```bash
.venv/bin/python -m py_compile <changed Python files>
git diff --check -- <exact changed paths>
```

For dashboard JavaScript, render the HTML and run the established Node syntax
check used by the focused tests. Do not assume Python compilation validates the
embedded browser script.

Historical broad-suite note: several threads repeatedly observed three unrelated
focus-routing failures around unfinished/removed focus APIs. Re-check current
HEAD before calling them pre-existing; do not weaken tests merely to preserve
that historical label.

Hardware acceptance must inspect durable evidence, not only logs:

- queue request consumed by intended lane;
- lane-scoped planner result and exact tool args;
- frozen JPEG/MP4 exists and constrained endpoint returns it;
- media duration/streams match the request;
- model answer records the evidence source;
- TTS artifact exists when speech is enabled;
- correct physical output reports success.

## Git and secret hygiene

- Run a secret-pattern scan on changed lines before commit.
- Never display or commit values from external secret files, camera passwords,
  API keys, bearer tokens, TLS private keys, or private-key material.
- A file containing credential-handling code is not itself a secret. Evaluate
  changed values, not keyword presence alone.
- Never `git add .` in this live checkout. Use exact source/doc/test paths.
- Do not commit generated audio/video/images even when they prove an acceptance
  test. Record only non-sensitive measurements in tests/docs.
- Preserve unrelated dirty tracked files. Do not use destructive cleanup to make
  status look clean.

## Thread archaeology retained here

Codex thread IDs are included only as provenance; other vendors may not be able
to read them. All actionable conclusions from them are encoded above and in the
ledger.

- `019f200d-d97e-7bb3-972e-8a67baba6cd8`: Omni 400 root cause, frozen snapshots,
  camera clips, trusted service routing, five-second audio/video verification.
- `019f202f-4cb6-7483-aedf-6326b580f9de`: persistent input window and exact
  decision-token display.
- `019f191e-ee44-7fe2-9531-2b9f5ac10281`: Current Time evolution, queue routing,
  attribution, visibility, per-lane scheduling, and rejection of deterministic
  web-query repair.
- `019f1936-f3e9-7093-a248-6d35d7abecaa`: prompt split, sole file authority,
  explicit Save, read-only retry, lossless formatting.
- `019f18af-e9c6-7b43-99c9-d76349af696c`: preview debugging and retirement of
  the history-less DeepStream agent in favor of the Wi-Fi lane queue.
- `019f1969-ce5c-7782-a7d0-bca302a7d6fa`: persisted Planner Details contract,
  prompt ordering under a constrained context, server-rendered fallback.
- `019f19c5-44b1-75e1-a4af-9e0d92a86148`: repeat-back diagnosis, intentional
  empty-response states, camera login failure and safe retry boundary.
- `019f194a-0bfb-78e0-a6fb-6570a94735c6`: acoustic loop guard behavior.

Compacted thread turns were specifically inspected. The important reversals from
those compacted histories are represented as superseded entries in the ledger.

## Updating this record

When a user changes an invariant:

1. Implement and test the new behavior.
2. Append a new ledger decision with a new ID and `supersedes` references.
3. Mark old entries superseded without deleting their rationale.
4. Update the applicable current-behavior section here.
5. Commit code, tests, this file, and the ledger together.

This prevents a later compacted thread from reviving an approach that was tried,
rejected, and replaced.
