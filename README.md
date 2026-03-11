# SourcingBot Lite

**Fast product sourcing with agentic spec matching — 15-30 seconds end-to-end.**

A dramatically simplified version of the full SourcingBot system (10+ minutes → under 30 seconds), built as a standalone Python/FastAPI service. Designed to be used as a tool by other agents in the Sourcy ecosystem.

---

## What It Does

Given a buyer's product requirement (title, description, target price, quantity), SourcingBot Lite:

1. **Generates search keywords** (English + Chinese) using Gemini
2. **Searches 1688 + Alibaba** concurrently via TMAPI
3. **Pre-filters** by price, MOQ, duplicates
4. **Matches each product against specs** using an agentic Gemini agent with tool-calling (image analysis, unit conversion, title extraction)
5. **Ranks and shortlists** the best products with veto/rerank scoring + supplier quality scoring
6. **Returns structured JSON** with extracted product specs — ready for downstream agent consumption

Users can provide their own veto/rerank specs, or let the AI auto-generate them from just a title + description.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     SourcingBot Lite                         │
│                                                             │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌─────────┐ │
│  │ FastAPI   │   │ Pipeline │   │ Gemini   │   │ TMAPI   │ │
│  │ Server    │──▶│ Engine   │──▶│ 2.0 Flash│   │ Search  │ │
│  │ + SSE     │   │          │   │ (Agents) │   │ API     │ │
│  └──────────┘   └──────────┘   └──────────┘   └─────────┘ │
│       │                                                     │
│  ┌──────────┐                                               │
│  │ Static   │                                               │
│  │ UI (HTML)│                                               │
│  └──────────┘                                               │
└─────────────────────────────────────────────────────────────┘
```

### Pipeline Flow (5-6 Steps)

```
Input SR JSON
│
├── STEP 0 (optional): AI Spec Generation ─── Gemini generates veto/rerank specs
│   │                                          from title + description
│   │                                          (runs in PARALLEL with Step 1)
│   ▼
├── STEP 1: Keyword Generation ────────────── Gemini → EN + CN search keywords
│   ▼
├── STEP 2: Product Search ────────────────── TMAPI → 1688 + Alibaba (concurrent)
│   ▼
├── STEP 3: Pre-filter ────────────────────── Dedup + price + MOQ filtering
│   ▼
├── STEP 4: Agentic Spec Matching ─────────── Per-product Gemini agent with tools:
│   │                                          • extract_from_title()
│   │                                          • analyze_image() (vision)
│   │                                          • calculate()
│   │                                          • convert_unit()
│   ▼
├── STEP 5: Ranking ───────────────────────── Veto verdict → Relaxation → Scoring
│   ▼
└── Output: Ranked products with specs ────── JSON with product_specs dict per item
```

### Timing Breakdown (Typical)

| Step | With Specs | Without Specs (AI Gen) |
|------|-----------|----------------------|
| AI Spec Gen | — | ~7-10s (parallel) |
| Keyword Gen | ~1-2s | ~1-2s (parallel) |
| Search | ~3-7s | ~3-7s |
| Pre-filter | <0.1s | <0.1s |
| Agentic Matching | ~5-8s | ~5-8s |
| Ranking | <0.1s | <0.1s |
| **Total** | **~10-18s** | **~17-28s** |

---

## Project Structure

```
SourcingLogicLiteVersion/
├── main.py                    # FastAPI app + CLI + pipeline orchestrator
├── requirements.txt           # Python dependencies
├── test_run.py               # Quick CLI test runner
├── DESIGN_PROPOSAL.md        # Original analysis & design proposal
│
├── pipeline/                  # Core pipeline modules
│   ├── __init__.py
│   ├── config.py             # Environment config (API keys, model names)
│   ├── models.py             # Pydantic models (input/output/internal)
│   ├── keyword_gen.py        # Step 1: Gemini keyword generation
│   ├── search.py             # Step 2: TMAPI search (1688 + Alibaba)
│   ├── prefilter.py          # Step 3: Price/MOQ/dedup filtering
│   ├── matcher.py            # Step 4: Agentic spec matching (Gemini + tools)
│   ├── tools.py              # Matcher tools: image analysis, calc, extract
│   ├── ranker.py             # Step 5: Veto/rerank scoring + ranking
│   └── spec_gen.py           # Step 0: AI spec generation from title+desc
│
├── static/
│   └── index.html            # Single-file UI (dark theme, SSE streaming)
│
└── test_data/                 # Sample SR JSONs for testing
    ├── water-bottle.json           # With manual specs
    ├── water-bottle-no-specs.json  # Without specs (AI generates them)
    └── ice-cream-container.json    # Edge case test
```

---

## Key Technical Decisions

### Why Gemini 2.0 Flash?
- Fast inference (~1-2s per call)
- Native tool-calling (function calling) support
- Vision capability for image analysis
- JSON response mode for structured output
- Cost-effective at scale

### Agentic Matching (not just LLM prompting)
Each product is matched by a Gemini agent that has **tools** it can call:
- `extract_from_title(title, spec_name)` — parse Chinese/English titles
- `analyze_image(url, question)` — Gemini Vision on product images
- `calculate(expression)` — math for price/unit conversions
- `convert_unit(value, from, to)` — metric/imperial conversion

The agent decides which tools to use per spec, making it much more accurate than a single prompt.

### Veto/ReRank Scoring System
- **Veto specs** = mandatory (NOT_MATCH → eliminated, UNKNOWN → tolerated)
- **ReRank specs** = preferences (affect ranking but don't eliminate)
- **UNKNOWN ≠ failure** — Chinese product titles rarely mention BPA-free, dishwasher-safe, etc. Missing info is treated as "insufficient data", not as a spec failure.
- **Relaxation** — if too few products pass strict veto, UNKNOWN specs are relaxed to allow more through

### AI Spec Generation (Two Flows, One Engine)
When no specs are provided, the system can generate them automatically. This is exposed in two ways:

1. **"Generate Specs with AI" button** — standalone `POST /generate-specs` endpoint. Specs are generated, injected into the editor, and the user can review/edit before running the pipeline. This is the pre-pipeline manual flow.

2. **Auto spec_gen inside the pipeline** — when `veto_specs: []` and `re_rank_specs: []`, the pipeline detects empty specs and runs `generate_specs()` in parallel with keyword generation. Zero extra wall time since keyword gen is faster. This is the fully autonomous flow.

Both use the **exact same `pipeline/spec_gen.py`** function — same Gemini prompt, same logic. The original SourcingBot required manually authored specs; the Lite version can operate with zero specs input.

### Product Specs Extraction
Each shortlisted product includes a `product_specs` dict with extracted values:
```json
{
  "product_category": "water bottle",
  "material": "stainless steel",
  "capacity_ml": null,
  "insulation": "double wall insulated",
  "price_usd": "2.6",
  "moq": "1",
  "platform": "1688",
  "supplier_type": "factory"
}
```
This is designed for downstream agent consumption — other agents can use these specs for training, comparison, or further decision-making.

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Web UI |
| `GET` | `/health` | Health check |
| `POST` | `/source` | Run pipeline (returns full result) |
| `POST` | `/source/stream` | Run pipeline with SSE streaming progress |
| `POST` | `/generate-specs` | Generate veto/rerank specs from title+description |
| `GET` | `/test-data` | List available test data files |
| `GET` | `/test-data/{filename}` | Get a specific test data file |

### POST /source/stream — SSE Streaming

Returns Server-Sent Events with real-time progress updates:
```
data: {"step": "spec_gen", "status": "running"}
data: {"step": "spec_gen", "status": "done", "time": 7.5, "data": {"veto": 6, "rerank": 4}}
data: {"step": "keyword_gen", "status": "done", "time": 7.5, "data": {"en": [...], "cn": [...]}}
data: {"step": "search", "status": "done", "time": 3.2, "data": {"count": 136}}
data: {"step": "agentic_matching", "status": "progress", "data": {"completed": 5, "total": 15}}
data: {"step": "result", "status": "done", "data": {<full SourcingResult>}}
```

### POST /generate-specs — Standalone Spec Generation

```json
// Request
{
  "title": "500ml Stainless Steel Water Bottle",
  "description": "Double wall insulated, BPA-free, leak-proof lid",
  "target_price": 5.0,
  "target_quantity": 1000
}

// Response → SpecsInput with generated veto_specs + re_rank_specs
```

---

## Input Format (SourcingRequest)

**Minimal input** (specs will be auto-generated):
```json
{
  "original_requirement": {
    "title": "500ml Stainless Steel Water Bottle",
    "description": "Double wall insulated, BPA-free, leak-proof lid",
    "target_price": 5.0,
    "target_price_currency": "USD",
    "target_quantity": 1000,
    "num_options": 5
  },
  "specs": {
    "veto_specs": [],
    "re_rank_specs": []
  }
}
```

**Full input** (with manual specs): see `test_data/water-bottle.json` for a complete example.

---

## Output Format (Agent JSON)

The JSON (Agent) view provides a clean structure for downstream agent consumption:

```json
{
  "run_id": "my-run-id",
  "request_title": "500ml Stainless Steel Water Bottle",
  "execution_time_seconds": 17.91,
  "total_searched": 136,
  "total_shortlisted": 5,
  "keywords": {
    "en_keywords": ["stainless steel water bottle"],
    "cn_keywords": ["不锈钢保温杯"]
  },
  "products": [
    {
      "rank": 1,
      "product_id": "930374004918",
      "title": "316不锈钢保温杯...",
      "product_url": "https://detail.1688.com/offer/...",
      "image_url": "https://...",
      "platform": "1688",
      "price_usd": 2.2,
      "moq": 1,
      "supplier": {
        "name": "...",
        "location": "浙江 义乌市",
        "is_factory": true,
        "rating": 4.8,
        "years": 6
      },
      "scores": {
        "veto_score": 0.667,
        "rerank_score": 0.714,
        "supplier_score": 0.483,
        "combined_score": 1.197
      },
      "verdict": "shortlisted",
      "product_specs": {
        "product_category": "water bottle",
        "material": "stainless steel",
        "capacity_ml": null,
        "insulation": null,
        "price_usd": "2.2",
        "platform": "1688",
        "supplier_type": "factory"
      },
      "spec_results": [
        {
          "spec_name": "material",
          "match_type": "MATCH",
          "product_value": "stainless steel",
          "confidence": 5,
          "reasoning": "Title mentions '316 stainless steel'."
        }
      ]
    }
  ]
}
```

---

## Setup & Running

### Prerequisites
- Python 3.11+
- Gemini API key (`GEMINI_API_KEY` or `GOOGLE_API_KEY` env var)
- TMAPI token (configured in `pipeline/config.py`)

### Install
```bash
cd SourcingLogicLiteVersion
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Run Server (UI + API)
```bash
python main.py                    # default port 8000
python main.py --port 9000        # custom port
# → http://localhost:8000
```

### Run CLI
```bash
python main.py test_data/water-bottle.json
python main.py test_data/water-bottle-no-specs.json
```

---

## UI Features

The single-page web UI (`static/index.html`) includes:

- **Test data dropdown** — load sample SRs or templates (with/without specs)
- **JSON editor** — paste or edit SR JSON directly
- **"Generate Specs with AI" button** — pre-generate specs, review in editor before running
- **Real-time pipeline progress** — step pills with timing (AI Specs → Keywords → Search → Pre-filter → Matching → Ranking)
- **AI-Generated Specs banner** — shows when specs were auto-generated
- **Products view** — cards with images, score bars (Spec Match, Preferences, Supplier, Overall), expandable spec details
- **JSON (Agent) view** — structured JSON output with Copy button for agent consumption
- **Keyword display** — EN (blue) + CN (orange) keyword tags

---

## Comparison: Original vs Lite

| Aspect | Original SourcingBot | SourcingBot Lite |
|--------|---------------------|-----------------|
| **Speed** | 5-10+ minutes | 15-30 seconds |
| **Languages** | TypeScript + Python | Python only |
| **Frameworks** | Mastra + FastAPI | FastAPI only |
| **LLMs** | GPT-4o-mini + Gemini | Gemini 2.0 Flash only |
| **Search** | Multi-stage (shallow + deep) | Single-stage concurrent |
| **Spec Matching** | Embedding + LLM (GPU) | Agentic LLM with tools |
| **Image Analysis** | Separate service | Inline Gemini Vision |
| **Spec Input** | Manual only | Manual or AI-generated |
| **Infrastructure** | Cloud Run + GPU + Redis + PG | Single process, no GPU |
| **Deployment** | Multi-service | Single `python main.py` |
| **Agent Output** | Internal use | Structured JSON for agents |

### What Was Cut
- Embedding-based matching (replaced with agentic tool-calling)
- GPU requirement (Gemini handles vision natively)
- Multi-stage search (shallow → deep → research)
- Redis/PostgreSQL/S3/Langfuse dependencies
- TypeScript/Mastra orchestration layer
- Tavily web research step
- Jina reranking step

### What Was Added
- AI spec generation (zero-config operation)
- Parallel execution (spec gen + keywords, concurrent search)
- Product specs extraction (for agent consumption)
- SSE streaming UI with real-time progress
- JSON (Agent) view for downstream tool integration
