---
name: dgx-spark-ai-continuation
description: Use when developing or operating the DGX Spark local multimodal webcam, Nemotron lane, DeepStream/autofocus, Current Time, tool-planning, or voice/talkback system in this directory.
---

# DGX Spark AI continuation skill

This file is a thin compatibility entry point for agents that discover
`SKILL.md`. The previous version referenced `/home/anslutsky/Dev/Cosmos-transfer`
and an obsolete three-process architecture; do not use that old path or model.

Mandatory reading order:

1. `AGENTS.md`
2. `AI_HANDOFF.md`
3. `docs/ai-decision-ledger.yaml`

Then inspect current code and the focused tests named by the applicable ledger
entry. Preserve the live dirty worktree and stage exact source/doc/test paths
only.

Supported service lifecycle entry point:

```bash
.venv/bin/python scripts/webcam_background_stack.py status
.venv/bin/python scripts/webcam_background_stack.py restart --only-service NAME
```

Supported test environment:

```bash
.venv/bin/python -m pytest -n auto -q <focused test paths>
```

If a requested change reverses an invariant, update `AI_HANDOFF.md` and append a
new decision with `supersedes` metadata to `docs/ai-decision-ledger.yaml`; never
erase the rejected approach from history.
