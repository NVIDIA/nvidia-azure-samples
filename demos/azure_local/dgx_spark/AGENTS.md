# DGX Spark continuation contract

Scope: this directory and all descendants.

Before changing code or runtime state, read these files in order:

1. `AI_HANDOFF.md`
2. `docs/ai-decision-ledger.yaml`
3. The tests named by the relevant ledger entries

The decision ledger is append-only history. An entry with `status: superseded` is
not current behavior, but it explains an approach that must not be accidentally
reintroduced. When intentionally reversing a decision, add a new entry with a
`supersedes` list; do not erase the old entry.

Hard rules:

- Preserve the dirty live worktree. Do not run blanket clean/reset/checkout or
  stage the whole directory. Runtime processes continuously mutate tracked and
  untracked JSON, logs, SQLite databases, PID files, bytecode, images, audio,
  and video.
- Stage source/docs/tests by exact path only. Never commit secrets, key files,
  `.env` contents, camera credentials, TLS private keys, media captures, model
  artifacts, logs, databases, or runtime state.
- Treat lane prompt files as user-owned configuration. Only explicit UI/API
  Save may write them. Do not add startup reconciliation, autosave, request-time
  prompt persistence, or a shared prompt file.
- Do not restore the retired history-less `deepstream-nemotron` worker.
  Autofocus-complete events belong in the normal Wi-Fi lane queue.
- Current Time and other service inputs must traverse the lane input queue and
  remain visible as service-authored turns. Do not bypass inference by writing
  them directly to conversation history.
- Ordinary tool routing is model-contract driven. The sole narrow deterministic
  policy enforcement currently allowed is the trusted-service camera-video
  contract documented as `D-ROUTE-006` in the ledger.
- Visual claims require frozen evidence returned by the relevant tool. Never
  substitute a live snapshot URL for the actual captured frame, and never let a
  model claim it watched a clip when clip capture failed.

Use the project environment for tests:

```bash
.venv/bin/python -m pytest -n auto -q <focused test paths>
```

The system `pytest` may fail because the repository root defines an xdist hook.
The project `.venv` contains `pytest` and `pytest-xdist`.

After any architectural or behavioral change, update `AI_HANDOFF.md` and append
or supersede a decision in `docs/ai-decision-ledger.yaml` in the same commit.
