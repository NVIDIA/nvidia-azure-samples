# Import notes

This sample imports all 64 tracked files from the lab source snapshot
`2828b2e9e96396df57f9ce0697ef94e5331cafb0` under
`agentic/nemoclaw/build-ai-agent`. Git history, local credentials, connection
settings, evidence archives and ignored build outputs are excluded.

The public copy adds Apache 2.0 SPDX headers, replaces old personal test
resource identifiers with documentation examples, updates repository links,
and adds a workflow at the repository root for this nested sample. Run
commands from the sample directory. The workflows under the sample's own
`.github` folder are retained from the source snapshot; GitHub runs the
repository-root workflow `validate-nemoclaw-build-ai-agent.yml`.

The Word handout remains the source for the three generated Markdown delivery
files. The source bundle and coordinator fingerprint are regenerated after
adding license headers. Historical hashes and results in RESULTS.md describe
the original measured rehearsal; they do not validate the imported bundle.
No cloud resources are deployed as part of this import.

The October 16 rehearsal passed the core learner incident and boundary flow.
Known reply-formatting and wait-helper errors, live token rotation, learner
timing, reconnect/reset/cleanup and event concurrency remain delivery checks.
See [results](RESULTS.md) and [the setup contract](INSTALL-KIT-SPEC.md).
