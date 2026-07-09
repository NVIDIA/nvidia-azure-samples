# Contributing Guidelines

We invite contributions that improve the samples, documentation, and setup
workflow in this repository.

## Pull Requests

Developer workflow for code contributions is as follows:

1. Create a topic branch with a descriptive name.
2. Make the change with tests or deployment verification where practical.
3. Update documentation when setup, behavior, environment variables, or user
   workflows change.
4. Open a merge request or pull request and complete the checklist.

## Local Checks

Install and enable the repository's pre-commit hooks:

```bash
python3 -m pip install pre-commit==4.6.0
pre-commit install
```

Run every check against the full repository:

```bash
pre-commit run --all-files
```

After installation, `git commit` checks staged files. GitHub Actions reruns all
hooks when a pull request is opened or updated.

## Signing Your Work

We require that all contributors sign off on their commits. This certifies that
the contribution is your original work, or that you have rights to submit it
under the same license or a compatible license.

Any contribution that contains commits that are not signed off will not be
accepted. To sign off on a commit, use the `--signoff` or `-s` option:

```bash
git commit -s -m "Add sample update"
```

This appends a `Signed-off-by` line to your commit message.

## Developer Certificate of Origin

Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this license
document, but changing it is not allowed.

Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I have the right
    to submit it under the open source license indicated in the file; or

(b) The contribution is based upon previous work that, to the best of my
    knowledge, is covered under an appropriate open source license and I have
    the right under that license to submit that work with modifications,
    whether created in whole or in part by me, under the same open source
    license (unless I am permitted to submit under a different license), as
    indicated in the file; or

(c) The contribution was provided directly to me by some other person who
    certified (a), (b), or (c) and I have not modified it; and

(d) I understand and agree that this project and the contribution are public
    and that a record of the contribution, including all personal information I
    submit with it, including my sign-off, is maintained indefinitely and may be
    redistributed consistent with this project or the open source licenses
    involved.
