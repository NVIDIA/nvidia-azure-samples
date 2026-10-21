#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Export the reviewed Word guide to portable and Skillable Markdown.

The DOCX is the shared learner handout. --check detects stale Markdown and
checks the portable guide's shell syntax without running any lab commands.
Only the Python standard library and bash are required.
"""
import argparse
from pathlib import Path
import re
import subprocess
import textwrap
import xml.etree.ElementTree as ET
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/learner-guide.docx"
TITLE = "NVIDIA - Nemoclaw with Microsoft Foundry and AKS"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
NS = {"w": W[1:-1]}
USERNAME = "@lab.CloudResourceTemplate(NemoClaw_AKS_Ignite).Parameters[vmAdminUsername]"
ENVIRONMENT = """| Assigned environment | Value |
| --- | --- |
| Lab instance | @lab.LabInstance.Id |
| Azure username | @lab.CloudPortalCredential(User1).Username |
| Subscription | @lab.CloudSubscription.Name |
| Resource group | @lab.CloudResourceGroup(ResourceGroup1).Name |

"""


def text(element):
    parts = []
    for node in element.iter():
        if node.tag == W + "t":
            parts.append(node.text or "")
        elif node.tag == W + "tab":
            parts.append("\t")
        elif node.tag == W + "br" and node.get(W + "type") != "page":
            parts.append("\n")
    return "".join(parts)


def style(paragraph):
    element = paragraph.find("w:pPr/w:pStyle", NS)
    return element.get(W + "val", "Normal") if element is not None else "Normal"


def paragraph_markdown(paragraph, relationships):
    pieces = []
    for child in paragraph:
        if child.tag == W + "hyperlink":
            label = text(child)
            url = relationships.get(child.get(R + "id"))
            pieces.append(f"[{label}]({url})" if url else label)
        elif child.tag != W + "pPr":
            pieces.append(text(child))
    return "".join(pieces)


def wrap(value, prefix=""):
    value = value.replace("https://portal.azure.com", "<https://portal.azure.com>")
    if value.startswith("Expected:"):
        value = "**Expected:**" + value[len("Expected:"):]
    return textwrap.fill(value, width=88, initial_indent=prefix,
                         subsequent_indent=" " * len(prefix),
                         break_long_words=False, break_on_hyphens=False)


def export():
    with ZipFile(SOURCE) as archive:
        document = ET.fromstring(archive.read("word/document.xml"))
        relationships = {item.get("Id"): item.get("Target") for item in
                         ET.fromstring(archive.read("word/_rels/document.xml.rels"))}
    body = document.find("w:body", NS)
    learner, coordinator = [], []
    output = learner
    counters = {}
    in_appendix = False
    learner_section = False
    for node in body:
        if node.tag == W + "p":
            value = paragraph_markdown(node, relationships)
            if not value:
                continue
            kind = style(node)
            if value == "Coordinator checklist":
                in_appendix = True
                output = coordinator
                output.append("# Coordinator checklist")
                continue
            if kind == "Title":
                if value != TITLE:
                    raise ValueError("Unexpected learner guide title")
                output.append("# " + value)
            elif kind.startswith("Heading"):
                if not in_appendix and kind == "Heading1":
                    learner_section = True
                level = int(kind[len("Heading"):]) + (0 if in_appendix else 1)
                if not in_appendix and not learner_section:
                    level = min(level, 2)
                output.append("#" * level + " " + value)
            elif kind == "LabCode":
                language = "text" if re.fullmatch(r"[a-f0-9]{64}", value) else "bash"
                output.append(f"```{language}\n{value}\n```")
            elif kind == "ListBullet":
                output.append(wrap(value, "- "))
            else:
                if value.startswith("Allow 60 minutes, subject to rehearsal."):
                    value = ("Allow 60 minutes, subject to rehearsal. Follow the tasks in order. "
                             "Delivery setup: [coordinator checklist](docs/COORDINATOR-CHECKLIST.md).")
                num = node.find("w:pPr/w:numPr/w:numId", NS)
                prefix = ""
                if num is not None:
                    key = num.get(W + "val")
                    counters[key] = counters.get(key, 0) + 1
                    prefix = str(counters[key]) + ". "
                output.append(wrap(value, prefix))
        elif node.tag == W + "tbl":
            rows = [["<br>".join(text(p) for p in cell.findall("w:p", NS))
                     .replace("|", r"\|") for cell in row.findall("w:tc", NS)]
                    for row in node.findall("w:tr", NS)]
            rows = [[re.sub(r"http://dependency-api(?:-invalid)?:8081",
                            lambda match: "`" + match.group() + "`", cell)
                     for cell in row] for row in rows]
            lines = ["| " + " | ".join(rows[0]) + " |",
                     "| " + " | ".join("---" for _ in rows[0]) + " |"]
            lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
            output.append("\n".join(lines))

    portable = "\n\n".join(learner) + "\n"
    skillable = portable.replace("ssh labuser@VM_PUBLIC_IP", f"ssh {USERNAME}@VM_PUBLIC_IP")
    skillable = re.sub(
        r"\s*Delivery setup:\s*\[coordinator\s+checklist\]\(docs/COORDINATOR-CHECKLIST\.md\)\.",
        "", skillable, count=1)
    skillable = skillable.replace("## Before you start\n\n", "## Before you start\n\n" + ENVIRONMENT, 1)
    skillable = re.sub(r"(?m)^(## (?:Before you start|Connect to Ubuntu|Task [1-9] [^\n]+|Troubleshooting))$",
                       r"===\n\n\1", skillable)
    generated = "<!-- Generated from docs/learner-guide.docx by scripts/export-learner-docs.py. -->\n\n"
    return {
        ROOT / "LAB-GUIDE.md": generated + portable,
        ROOT / "SKILLABLE-INSTRUCTIONS.md": generated + skillable,
        ROOT / "docs/COORDINATOR-CHECKLIST.md": generated + "\n\n".join(coordinator) + "\n",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="check exports without writing")
    args = parser.parse_args()
    outputs = export()
    for target, rendered in outputs.items():
        if args.check:
            if not target.exists() or target.read_text() != rendered:
                raise SystemExit(f"{target.relative_to(ROOT)} is stale: run python3 scripts/export-learner-docs.py")
        else:
            target.write_text(rendered)
            print("Updated", target.relative_to(ROOT))
    portable = outputs[ROOT / "LAB-GUIDE.md"]
    tasks = re.findall(r"(?m)^## Task (\d) ", portable)
    if tasks != list("123456789"):
        raise SystemExit("The learner guide must retain all nine ordered tasks")
    blocks = re.findall(r"```bash\n(.*?)\n```", portable, re.DOTALL)
    for index, block in enumerate(blocks, 1):
        result = subprocess.run(["bash", "-n"], input=block, text=True, capture_output=True)
        if result.returncode:
            raise SystemExit(f"Learner command block {index}: {result.stderr}")
    print(f"Learner exports current; nine tasks and {len(blocks)} shell blocks checked.")


if __name__ == "__main__":
    main()
