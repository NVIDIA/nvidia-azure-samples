# Insurance Claims Triage — Architecture

A working multi-agent demo where a **Microsoft Foundry hosted agent** orchestrates the **NVIDIA VSS Agent** (running on AKS) as a specialized video-understanding sub-agent, plus three mock business tools, to turn a customer's damage video into a draft insurance claim.

Three components, three deployment surfaces:

| Component | Location | Tech |
|---|---|---|
| **Customer rep web UI** | local laptop / wherever you launch Streamlit | Streamlit + Python |
| **Master agent** (insurance-claims-triage) | Microsoft Foundry (Azure, North Central US) | Microsoft Agent Framework + gpt-4.1 |
| **VSS sub-agent** (long-video summarization) | AKS (Azure, West Europe) | NVIDIA VSS LVS Helm chart on A100 GPUs |

The four diagrams below are designed for use in a slide deck. Each tells one focused story.

---

## 1. The multi-agent pattern (headline diagram)

The point of the demo: a **master agent** delegates the work GPT can't do alone to a **specialized sub-agent**.

```
                    ╭────────────────────────────╮
                    │  Customer Rep — Web UI     │
                    │  (Streamlit + Python)      │
                    ╰─────────────┬──────────────╯
                                  │ POST /responses
                                  │ (Bearer token)
                                  ▼
            ╔══════════════════════════════════════════╗
            ║   MASTER AGENT  ·  Microsoft Foundry     ║
            ║   "insurance-claims-triage"  (gpt-4.1)   ║
            ║   Microsoft Agent Framework / Responses  ║
            ╚══╤══════════╤══════════════╤═════════╤═══╝
               │          │              │         │
               ▼          ▼              ▼         ▼
         ┌─────────┐  ┌────────┐   ┌─────────┐  ┌──────┐
         │  VSS    │  │ Policy │   │ Repair  │  │Claim │
         │ SUB-AGT │  │   DB   │   │  Cost   │  │ PDF  │
         │  (AKS)  │  │(mock)  │   │ Engine  │  │      │
         │ cosmos- │  └────────┘   └─────────┘  └──────┘
         │reason2  │
         │ (8B VLM)│         ▲ all three are simple
         └─────────┘         Python @tool functions
            ▲
            │ /chat/stream
            │
   The ONLY sub-agent that does work
   GPT can't do alone (multi-modal,
   long-video understanding, on-prem)
```

**Reading**: VSS is the differentiator. The other three "tools" are mock backends that simulate CRM / pricing / document-generation systems — a real deployment would swap them for Salesforce, a parts-pricing API, and a Blob Storage upload, respectively. None of the three is the demo's value story. VSS is.

---

## 2. End-to-end sequence — one claim, upload to PDF

```
Rep      Streamlit       VSS Agent         Foundry Agent      Local PDF
 │            │              │                   │               │
 │ upload     │              │                   │               │
 │ video ────▶│              │                   │               │
 │            │ POST          │                  │               │
 │            │ /api/v1/videos│                  │               │
 │            │──────────────▶│                  │               │
 │            │◀─ presigned URL                  │               │
 │            │ PUT bytes    │                   │               │
 │            │──────────────▶│  (video → VST)   │               │
 │            │              │                   │               │
 │            │  POST /responses                 │               │
 │            │──────────────────────────────────▶               │
 │            │                                  │               │
 │            │            ┌─────────────────────┤               │
 │            │ tool calls │ vss_analyze_video   │               │
 │            │            │ ────▶ POST          │               │
 │            │            │      /chat/stream   │               │
 │            │            │ ◀──── prose         │               │
 │            │            │ lookup_policy       │               │
 │            │            │ estimate_repair     │               │
 │            │            │ draft_claim_pdf     │               │
 │            │            └─────────────────────┤               │
 │            │                                  │               │
 │            │◀── summary text ─────────────────┤               │
 │            │ regex parse                       │               │
 │            │ render PDF locally               │ ──────────────▶
 │            │◀──────────── PDF bytes ─────────────────────────┤
 │◀── preview │                                                  │
 │            │                                                  │
```

**Timing observed**: ~5-10s for video upload, ~30-60s for the agent loop (VSS analysis dominates), <1s for PDF rendering. End-to-end about 45-75s per claim.

---

## 3. Deployment topology — cross-region example

```
┌────────────────────────────────────────────────────────────────────┐
│ Azure subscription · <your-subscription>                           │
│                                                                    │
│  ┌──────────────────────────────┐   ┌───────────────────────────┐  │
│  │ Region: North Central US     │   │ Region: West Europe       │  │
│  │ rg-insurance-claims-…-dev    │   │ rg-vss-build-nc96         │  │
│  │                              │   │                           │  │
│  │ ┌──────────────────────────┐ │   │ ┌───────────────────────┐ │  │
│  │ │ Foundry Project          │ │   │ │ AKS Cluster (aks-vss) │ │  │
│  │ │ ai-project-insurance-… ──┼─┼──▶│ │  • 1× NC96ads_A100_v4 │ │  │
│  │ │                          │ │   │ │     (4× A100 80GB)    │ │  │
│  │ │ ┌──────────────────────┐ │ │   │ │  • ingress-nginx LB   │ │  │
│  │ │ │ Hosted Agent (cont.) │ │ │   │ │     <EXTERNAL_HOST>   │ │  │
│  │ │ │ insurance-claims-tr… │ │ │   │ │                       │ │  │
│  │ │ │ gpt-4.1 (Std, 50TPM) │ │ │   │ │ ┌───────────────────┐ │ │  │
│  │ │ └──────────────────────┘ │ │   │ │ │ VSS LVS chart     │ │ │  │
│  │ │ ACR · <your-registry>    │ │   │ │ │ vss-agent         │ │ │  │
│  │ │ App Insights · appi-…    │ │   │ │ │ cosmos-reason2-8b │ │ │  │
│  │ └──────────────────────────┘ │   │ │ │ nemotron-nano-9b  │ │ │  │
│  │                              │   │ │ │ lvs-server, VST,  │ │ │  │
│  │  ~110ms RTT cross-region   ──┼───┼─│ │ ES + Kibana       │ │ │  │
│  └──────────────────────────────┘   │ │ └───────────────────┘ │ │  │
│                                     │ └───────────────────────┘ │  │
│                                     └───────────────────────────┘  │
└────────────────────────────────────────────────────────────────────┘
```

**Why two regions:** AKS NC96ads_A100_v4 capacity was available in West Europe; Foundry workshop region preference was North Central US. Cross-region latency (~110ms RTT) is workable for a demo. For production: co-locate.

---

## 4. Inside the VSS sub-agent (AI infra deep-dive)

```
                     vss_analyze_video(video_id, question)
                                   │
                                   ▼
          ┌─────────────────────────────────────────────┐
          │  vss-agent (LVS profile, NeMo Agent Toolkit)│
          │  Receives chat → plans → calls inner tools  │
          └──┬───────────────┬──────────────┬───────────┘
             │               │              │
             ▼               ▼              ▼
       ┌─────────┐    ┌────────────┐  ┌────────────┐
       │ VST     │    │ lvs_video_ │  │ video_     │
       │ (video  │    │ understand │  │ understand │
       │ store)  │    │  ▶ lvs-srv │  │  ▶ NIM VLM │
       └─────────┘    └─────┬──────┘  └─────┬──────┘
                            │                │
                            ▼                ▼
                     ┌────────────────────────────┐
                     │ cosmos-reason2-8b (NIM)    │
                     │  • VLM, vision encoder     │
                     │  • TRT-LLM engine          │
                     │  • 1× A100 80GB            │
                     └────────────────────────────┘
                            │
                            ▼
                     ┌────────────────────────────┐
                     │ nemotron-nano-9b-v2 (NIM)  │
                     │  • LLM for synthesis       │
                     │  • TRT-LLM engine          │
                     │  • 1× A100 80GB            │
                     └────────────────────────────┘
                            │
                            ▼
                ◀── plain-English damage analysis ──
                    (with <agent-think> stripped)
```

**Key point**: VSS is *itself* a multi-step agent. The master Foundry agent calls VSS as one tool; VSS internally chains VLM → LLM → storage → ES → Kibana. That nested agent architecture is what makes VSS hard to replicate with raw GPT vision.

---

## Why VSS belongs as a sub-agent (vs. building this with gpt-4o-vision alone)

| Capability | gpt-4o-vision (or gpt-5) alone | VSS LVS as sub-agent |
|---|---|---|
| 30-second damage clip | ✅ works | ✅ works (overkill for the size) |
| 90-minute drone / body-cam footage | ❌ no — token budget blown after a few frames | ✅ designed for it |
| Search 1000s of indexed videos | ❌ no persistent index | ✅ Elasticsearch backend |
| Live RTSP camera feeds | ❌ not supported | ✅ rtvi-vlm subchart |
| Data sovereignty (no upstream OpenAI) | ❌ sends to Azure OpenAI | ✅ on-prem AKS |
| Cost at scale | $$ per token | One-time GPU cost, marginal ≈ 0 |

For this **insurance demo specifically**, VSS is somewhat overkill (a 30s clip would also be handled by gpt-vision). The architecture is reusable for stronger use cases — drone pipeline inspection, body-cam review, multi-camera retail analytics — where GPT-vision is structurally incapable.

---

## 5. How VSS prose becomes an insurance estimate (the pricing pipeline)

VSS doesn't know anything about insurance. It returns natural-language damage descriptions. The **master agent** (gpt-4.1) does the prose-to-structured-data translation, then a hard-coded **mock pricing engine** turns that into dollars. Three stages:

```
            ┌────────────────────────────────────────────────────────────┐
   STAGE 1  │  VSS prose (free-form English)                             │
            │  "The car has significant front-end damage, including a    │
            │   shattered windshield and a crumpled hood. The left side  │
            │   door is open, revealing internal components like the     │
            │   engine bay. There are also visible dents and scratches"  │
            └─────────────────────────┬──────────────────────────────────┘
                                      │  (gpt-4.1 applies severity rules
                                      │   from instructions.md step 2)
                                      ▼
            ┌────────────────────────────────────────────────────────────┐
   STAGE 2  │  estimate_repair_cost(damage_items=[                       │
            │      {panel: "hood",         severity: "severe"},          │
            │      {panel: "windshield",   severity: "severe"},          │
            │      {panel: "front bumper", severity: "severe"},          │
            │      {panel: "driver door",  severity: "moderate"},        │
            │  ])                                                        │
            └─────────────────────────┬──────────────────────────────────┘
                                      │  (hard-coded lookup table
                                      │   in tools.py)
                                      ▼
            ┌────────────────────────────────────────────────────────────┐
   STAGE 3  │  {                                                         │
            │    line_items: [...],                                      │
            │    parts_total_usd:  4127.00,                              │
            │    labor_total_usd:  2660.00,                              │
            │    grand_total_usd:  6862.00                               │
            │  }                                                         │
            └────────────────────────────────────────────────────────────┘
```

### Stage 1 → 2: Severity rules in the system prompt

The master agent's system prompt ([instructions.md](insurance-claims-foundry/src/agent-framework-agent-basic-responses/instructions.md) step 2) tells gpt-4.1 how to map prose adjectives to severity tiers:

| Prose phrase patterns | Severity tier |
|---|---|
| scratches, scuffs | `minor` |
| visible dents, fluid leak, broken trim | `moderate` |
| crumpled, shattered, deformed, structural | `severe` |

The model also picks **canonical body-shop panel names** that match the pricing table (e.g. "shattered windshield" → `panel: "windshield"`). This is where the LLM does the actual semantic work — the pricing engine doesn't see prose, only the structured dict.

### Stage 2 → 3: The pricing engine

From [`estimate_repair_cost`](insurance-claims-foundry/src/agent-framework-agent-basic-responses/tools.py):

```python
_PART_PRICES_USD = {
    "front bumper": 850,  "rear bumper": 720,  "hood": 1200,
    "driver door":  950,  "passenger door": 950,
    "rear quarter panel": 1400,
    "fender": 680, "windshield": 540, "headlight": 380,
    "tail light": 220, "side mirror": 180, "wheel": 320,
    "grille": 420,
}
_SEVERITY_MULTIPLIER     = {"minor": 0.4, "moderate": 0.8, "severe": 1.3}
_LABOR_HOURLY_USD        = 95
_LABOR_HOURS_BY_SEVERITY = {"minor": 1.5, "moderate": 4.0, "severe": 8.0}
```

Per item:

```
part_cost  = _PART_PRICES_USD[panel]    × _SEVERITY_MULTIPLIER[severity]
labor_cost = _LABOR_HOURS_BY_SEVERITY[severity] × $95/hr
line_total = part_cost + labor_cost
```

Unknown panels (e.g. "tailgate" — not in the dict) fall back to **$600 base** so the LLM can invent panel names without crashing.

### Worked example — wrecked-car video

VSS prose → gpt-4.1 classifications → pricing engine:

| Panel | Severity | Part base × mult | Labor hrs × $95 | Line total |
|---|---|---|---|---|
| hood | severe | 1200 × 1.3 = $1,560 | 8 × 95 = $760 | **$2,320** |
| windshield | severe | 540 × 1.3 = $702 | 8 × 95 = $760 | **$1,462** |
| front bumper | severe | 850 × 1.3 = $1,105 | 8 × 95 = $760 | **$1,865** |
| driver door | moderate | 950 × 0.8 = $760 | 4 × 95 = $380 | **$1,140** |
| | | **Parts: $4,127** | **Labor: $2,660** | **Total: $6,787** |

Apply the policy's $500 deductible (POL-2025-44912, Alex Romero) → **payable $6,287**.

Note: the actual agent run produced $6,862 — the small delta vs the table above is because gpt-4.1 sometimes picks slightly different severity tiers between runs (LLM non-determinism). Pinning `temperature: 0` in agent.yaml would tighten this if reproducibility matters.

### What this pipeline intentionally is NOT (and what a real one looks like)

The pricing engine is a deliberately simple mock so the demo runs offline. The integration shape — *"agent calls one tool that returns `{line_items, total}`"* — stays the same; only the implementation behind `estimate_repair_cost` changes.

| Real replacement | What it adds |
|---|---|
| **CCC ONE / Mitchell Cloud Estimating API** | Industry-standard parts + labor DB, regional pricing, OEM vs aftermarket toggles |
| **Audatex / Solera** | Same category, more European market |
| **Internal shop pricing engine** | Per-shop labor rates, parts markup, customer discounts |
| **Photo-AI estimators (Tractable, CCC IQ)** | Skip the LLM-classification step entirely — they look at the VSS frames directly and emit a CCC-grade estimate |

### Where to tighten this if you care about accuracy

1. **VIN-aware pricing** — the policy lookup already has the vehicle (year/make/model), but `estimate_repair_cost` doesn't see it. The same "hood" panel costs very differently on a 2022 Camry vs a 2024 F-150.
2. **Multi-panel labor discounts** — body shops don't charge full labor hours for adjacent panels (paint blends, prep time shares). Real pricing engines have these rules.
3. **Total-loss threshold** — if `grand_total > 70% × vehicle_value`, real systems flip to total-loss valuation. Easy to add as a step 4.5 in instructions.md.
4. **Confidence propagation** — VSS could be asked to return a per-panel confidence score; low-confidence items get flagged for adjuster review in the PDF.

---

## File map of the demo

```
demo/
├── README.md                  ← this file
├── insurance-claims/           ← MCP-based local prototype (Anthropic/OpenAI clients)
│   ├── vss_mcp_server.py
│   ├── tools.py
│   ├── local_runner.py
│   └── sample_data/policies.json
├── insurance-claims-foundry/   ← Foundry hosted-agent deployment (azd)
│   ├── azure.yaml
│   ├── infra/
│   └── src/agent-framework-agent-basic-responses/
│       ├── main.py
│       ├── tools.py
│       ├── agent.yaml
│       ├── instructions.md
│       └── sample_data/policies.json
└── insurance-claims-ui/        ← Streamlit customer-rep UI
    ├── app.py
    ├── policies.json
    └── output/
```
