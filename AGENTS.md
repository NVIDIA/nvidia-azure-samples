# Repository Agent Guidance

## Repository Organization

This repository contains independent samples organized by use case. Read the
nearest README and relevant manifests before changing a sample.

Place every new sample under the appropriate existing top-level use-case
directory. Do not add samples at the repository root or create overlapping
category structures. Update the root README sample listing when adding or
moving a sample.

## Working Practices

- Keep documentation, commands, manifests, and referenced files synchronized.
- Reuse patterns within the affected sample. Avoid cross-sample abstractions
  unless several samples have the same demonstrated need.
- Preserve existing user changes and avoid unrelated formatting churn.
- Never commit secrets, credentials, local environment files, or generated
  artifacts.
- Do not deploy, modify, or delete cloud resources without explicit
  authorization.

## License Headers

Preserve existing SPDX headers. For new source, infrastructure, or
configuration files, follow the nearest comparable files. When those files use
SPDX and the format supports comments, use the matching comment syntax with:

```text
SPDX-FileCopyrightText: Copyright (c) <current year> NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
```

Do not add comment headers to JSON or other formats that prohibit comments. Do
not modify restored or unrelated files solely to add headers.

## Validation

Run `pre-commit run --all-files` and the sample-specific checks documented by
the nearest README or manifest. Report skipped or unavailable checks honestly;
do not present dependency-based skips as successful coverage.
