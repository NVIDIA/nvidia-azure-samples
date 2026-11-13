# Learner document maintenance

The reviewed source is [learner-guide.docx](learner-guide.docx), titled
**NVIDIA - Nemoclaw with Microsoft Foundry and AKS**. It contains the shortened
learner instructions and coordinator appendix, with editable shaded code blocks.

## Update the delivery package

1. Edit the Word guide and inspect its rendered pages. Keep commands copyable,
   code blocks together, and expected results beside their tasks.
2. From the sample directory, regenerate and check the Markdown exports:

   ```bash
   python3 scripts/export-learner-docs.py
   python3 scripts/export-learner-docs.py --check
   ```

3. Review all four delivery files together: the Word guide,
   [portable Markdown](../LAB-GUIDE.md),
   [Skillable Markdown](../SKILLABLE-INSTRUCTIONS.md), and
   [coordinator checklist](COORDINATOR-CHECKLIST.md). Commit them together.
4. Replace the complete Skillable Instructions content with
   `SKILLABLE-INSTRUCTIONS.md`, save, and launch a fresh test. Confirm the
   visible revision is **27 October 2026**. GitHub changes do not update the
   saved Skillable instructions automatically.
5. Check the connection page in the learner display: credentials must be beside
   sign-in, the VM NIC public IP must be distinguishable from the NAT IP, and
   the VM must be findable without a resource-group token. Insert any remaining
   replacement tokens from Studio's **@lab** menu; resolve or remove literal
   tokens before delivery. The SSH example uses `labuser`; change it to the
   assigned Ubuntu username from Resources when different.
6. Confirm Task 2 uses Node.js rather than `nano`, saves both replies, and gives
   concrete comparison criteria. Task 4 must name all three required reads and
   stop on a missing final summary. Recheck this displayed content after saving.

The exports retain all nine tasks. The Skillable variant adds runtime context
and `===` page breaks; the coordinator appendix is exported separately.
Edit the Word source rather than the generated Markdown. The documentation
workflow checks parity and shell syntax without installing document libraries
or executing lab commands.

## Publish and validate the lab

GitHub updates do not replace Studio's saved templates. Follow
[infra/SETUP.md](../infra/SETUP.md) to save `infra/main.bicep` in template 6891.
The author reports capacity 100 saved in model template 6838; verify actual
provider and pool limits. Preview the updated instructions in Studio. The
fresh instance 66301586 completed the core learner workflow with a log-prompt
correction. Repeat with a new learner, then validate live token rotation,
reconnection, reset, cleanup and planned delivery concurrency.

See [RESULTS.md](../RESULTS.md) for evidence and remaining release gates.
Documentation checks do not establish successful fresh provisioning or model
capacity for the event.

The October 27 learner report used instructions matching the original
150-second Task 4 command. The live Studio revision and that session's tool
history have not been verified. See the instructor guide's diagnostic steps;
do not count the missing-summary message as a successful diagnosis.
