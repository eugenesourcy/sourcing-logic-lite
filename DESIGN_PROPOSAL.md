# SourcingBot Lite Version - Deep Analysis & Proposal

## Table of Contents
1. [Current System Overview](#current-system-overview)
2. [Full Pipeline Flow](#full-pipeline-flow)
3. [Timing Breakdown (Current)](#timing-breakdown-current)
4. [Bottleneck Analysis](#bottleneck-analysis)
5. [Lite Version Proposal (10min → 15s)](#lite-version-proposal)
6. [Architecture Comparison](#architecture-comparison)
7. [What We Cut & Why](#what-we-cut--why)
8. [Implementation Plan](#implementation-plan)

---

## Current System Overview

The SourcingBot consists of **two services**:

### 1. Sourcing Logic (`sourcing-logic/`)
- **Framework:** Mastra (TypeScript/Bun)
- **Purpose:** End-to-end sourcing pipeline - from SR input to ranked product list
- **Scale:** 47+ tools, 25+ workflows, 3 AI agents, 20+ schemas
- **LLMs:** GPT-4o-mini (qualification), Gemini 2.5 Flash (image analysis)
- **External APIs:** 1688 (native + TMAPI), Alibaba (native + TMAPI), Tavily (web research), Jina (reranking)
- **Storage:** PostgreSQL, Redis, S3, Langfuse

### 2. Spec Matching (`spec-matching-v2.5/`)
- **Framework:** FastAPI (Python)
- **Purpose:** 3-stage product filtering - Veto → Rerank → Supplier Score
- **LLMs:** Gemini 2.0 Flash (text validation + vision)
- **Embeddings:** thenlper/gte-large (GPU, sentence-transformers)
- **Storage:** Firestore
- **Deployment:** Cloud Run with NVIDIA L4 GPU

---

## Full Pipeline Flow

```
SR Input (from Scube JSON)
│
├── STAGE 0: INPUT PARSING (~100ms)
│   └── Parse SR: title, description, specs, images, quantity, price
│
├── STAGE 1: KEYWORD GENERATION + SHALLOW SEARCH (~90s)
│   ├── LLM generates initial keywords from SR (GPT-4o-mini)
│   ├── Search 1688 EN (TMAPI) ──────────┐
│   ├── Search 1688 CN (TMAPI) ──────────┤ PARALLEL
│   └── Search Alibaba (TMAPI) ──────────┘
│   └── Result: ~120-200 products
│
├── STAGE 2: EXPLORATORY SEARCH (~150s)
│   ├── Deep keyword gen (Tavily web research + LLM)
│   ├── Search 1688 Image ───────────────┐
│   ├── Search 1688 Global ──────────────┤
│   ├── Search Alibaba Native ───────────┤ PARALLEL
│   ├── Search 1688 Suppliers ───────────┘
│   └── Shallow search with refined keywords
│   └── Result: ~300-500 additional products
│
├── STAGE 3: DEDUPLICATION (~90s)
│   ├── Simple dedup (item_id, image_url)
│   └── Cluster dedup (Gemini semantic similarity)
│   └── Result: ~200-400 unique products
│
├── STAGE 4: LLM QUALIFICATION (~240s) ← BIGGEST BOTTLENECK
│   ├── For EACH product (200-400x):
│   │   ├── Compare specs vs SR requirements
│   │   ├── Price validation (CNY→USD conversion)
│   │   ├── MOQ check
│   │   └── Score 1-5
│   └── Result: ~180-240 scored products
│
├── STAGE 5: FILTERING + ANALYSIS (~45s)
│   ├── Filter: score >= 2
│   ├── Calculate per-endpoint stats
│   └── Merge unevaluated products
│   └── Result: ~100-200 qualified products
│
├── STAGE 6: SUPPLIER CAPABILITY MAPPING (~45s)
│   └── Map supplier capabilities to specs
│
├── STAGE 7: SPEC MATCHING API (~5-300s) ← SECOND BOTTLENECK
│   ├── Send ~100-200 products to spec-matching service
│   ├── Per product, per spec:
│   │   ├── SpecialSpecMatcher (Price/MOQ/Lead Time)
│   │   ├── VectorMatcher (GPU embeddings, cosine similarity)
│   │   ├── LLMMatcher (Gemini, if similarity 0.65-0.92)
│   │   └── ImageAnalyzer (Gemini Vision fallback)
│   ├── Veto filtering (pass ALL mandatory specs)
│   ├── Rerank scoring (preference weighting)
│   └── Supplier scoring (type + service + category)
│   └── Result: Ranked shortlist
│
├── STAGE 8: OBSERVABILITY + EXPORT (~60s)
│   ├── Collect metrics
│   ├── Upload to S3
│   └── Generate final result set
│
└── OUTPUT: 100-300 ranked products + S3 URL
    Total: 8-20 minutes
```

---

## Timing Breakdown (Current) - REAL BENCHMARKS

### Sourcing Logic Pipeline (Water Bottle SR, 787 raw products)
> Tested: 2026-03-11, Local machine, Node 22, Mastra 1.3.1

| Step | What | Actual Time | % of Total |
|------|------|-------------|------------|
| shallow-search-wrapper | Keyword gen + 1688/Alibaba search | **50.3s** | 28.3% |
| exploratory-search-workflow | Deep search + Tavily | **36.6s** | 20.6% |
| supplier-search-workflow | 1688 supplier discovery | 0.9s | 0.5% |
| cluster-deduplicate | Gemini semantic dedup | **75.1s** | 42.3% |
| deduplicated-search-results | Merge results | 0.1s | <0.1% |
| llm-qualification | GPT-4o-mini per-product scoring | **45.3s** | 25.5% |
| stage-7-analysis | Filter + stats | 0.2s | 0.1% |
| supplier-capability-mapping | Supplier matching | 0.2s | 0.1% |
| spec-matching-api | Send to spec-matching service | 1.6s | 0.9% |
| observability-collection | Metrics + S3 | 0.3s | 0.2% |
| aggregate-results | Final compilation | 1.2s | 0.7% |
| **TOTAL PIPELINE** | | **177.7s (3.0 min)** | **100%** |

### Spec Matching Service (57 products, Army Coasters SR)
> Tested: 2026-03-11, Local machine, Python 3.12, MPS (Metal GPU)

| Phase | What | Actual Time |
|-------|------|-------------|
| Model loading | gte-large embeddings on MPS | **73s** |
| Product processing | 57 products x 2 veto specs | **~5 min 18s** |
| **TOTAL** | | **6 min 51s** |
| Per product avg | Vector + LLM + image per spec | **~7.2s/product** |

### Combined End-to-End (Sourcing Logic + Spec Matching)
| Phase | Time |
|-------|------|
| Sourcing Logic (search + qualify) | ~3 min |
| Spec Matching (veto + rerank + score) | ~7 min |
| **TOTAL END-TO-END** | **~10 min** |

---

## Bottleneck Analysis

### Top 4 Time Killers (from real benchmarks)

1. **Cluster Deduplication - 75.1s (42% of sourcing-logic)**
   - Gemini semantic similarity on 787 products
   - WHY SLOW: LLM-based clustering on hundreds of products
   - LITE FIX: Simple dedup only (item_id), since we search fewer products

2. **Shallow Search - 50.3s (28% of sourcing-logic)**
   - Keyword generation + multi-endpoint TMAPI search
   - Includes 5.5s rate-limit delays per API call
   - WHY SLOW: Multiple endpoints + artificial delays
   - LITE FIX: 2 endpoints only, single keyword gen call

3. **Spec Matching - 6:51 total (57 products)**
   - 73s model loading + 5:18 processing
   - ~7.2s per product (vector embed + LLM validate + image analyze)
   - WHY SLOW: Per-product-per-spec LLM calls to Gemini
   - LITE FIX: Direct field matching only, skip embeddings + LLM

4. **LLM Qualification - 45.3s**
   - GPT-4o-mini scores each product individually
   - WHY SLOW: Sequential per-product API calls
   - LITE FIX: Single batch LLM call for all products at once

5. **Exploratory Search - 36.6s**
   - Tavily research + deep keyword gen + additional endpoints
   - WHY SLOW: Web research + extra search rounds
   - LITE FIX: Remove entirely (first-page search is sufficient)

---

## Lite Version Proposal (10min → 15s)

### Target: 15 seconds total

### Strategy: "Fast First Page, Smart Filter"

```
SR Input
│
├── STEP 1: FAST KEYWORD GEN (~1s)
│   └── Single GPT-4o-mini call → 3 keywords (EN + CN)
│
├── STEP 2: PARALLEL FIRST-PAGE SEARCH (~5s)
│   ├── 1688 first page only (20-40 items) ──┐
│   └── Alibaba first page only (20-40 items) ┘ PARALLEL
│   └── Result: 40-80 products
│
├── STEP 3: SIMPLE DEDUP (~0.5s)
│   └── Remove exact duplicates (item_id only, no LLM)
│   └── Result: 30-60 unique products
│
├── STEP 4: BATCH LLM FILTER + RANK (~6s)
│   └── ONE LLM call with ALL products
│   │   Model: GPT-4o-mini or Gemini 2.0 Flash
│   │   Prompt: "Given these specs and these 30-60 products,
│   │            score each 1-5, return JSON array"
│   └── Result: Products with scores, sorted
│
├── STEP 5: TOP-N SPEC VERIFICATION (~5s)
│   └── Take top 10-15 products only
│   └── Lightweight spec check (direct field matching only)
│   │   - Price check (numeric comparison)
│   │   - MOQ check (numeric comparison)
│   │   - Category match (string match)
│   │   - Skip vector embeddings, skip GPU
│   │   - Skip image analysis
│   └── Result: 8-12 verified products
│
└── OUTPUT: Top 10 ranked products
    Total: ~12-15 seconds
```

---

## Architecture Comparison

### Current (Full) vs Lite

| Aspect | Current (Full) | Lite Version |
|--------|---------------|--------------|
| **Keywords** | Multi-round LLM + Tavily research | Single LLM call → 3 keywords |
| **Search scope** | 6+ endpoints, multiple pages, image search | 2 endpoints (1688 + Alibaba), first page only |
| **Products found** | 500-800 raw | 40-80 raw |
| **Dedup** | Simple + Gemini cluster dedup | Simple dedup only (item_id) |
| **Qualification** | Per-product LLM calls (200-400x) | Single batch LLM call (all at once) |
| **Spec matching** | GPU embeddings + LLM + Vision per spec per product | Direct field matching only (top 10-15) |
| **Supplier scoring** | Full capability mapping | Skip (or basic type check) |
| **Image analysis** | Gemini Vision for image specs | Skip entirely |
| **Observability** | S3 upload, Langfuse, metrics | Skip (log only) |
| **Infrastructure** | Mastra framework + Cloud Run GPU | Single script, no GPU needed |
| **Total time** | 10-20 min | 12-15 sec |
| **Output quality** | Comprehensive, high recall | Good enough, fast feedback |

---

## What We Cut & Why

### REMOVED entirely (saves ~6 min)

| Feature | Time Saved | Why Safe to Cut |
|---------|-----------|----------------|
| Exploratory search (Stage 2) | ~2.5 min | First page usually has most relevant results |
| Tavily web research | ~30s | Not needed for keyword gen |
| Cluster deduplication (Gemini) | ~1.5 min | Simple dedup is sufficient for 40-80 products |
| Image search (1688) | ~30s | Text search covers most cases |
| Supplier search (1688) | ~30s | Not needed for initial results |
| GPU vector embeddings | ~varies | Direct field matching is faster for top-N |
| Image analysis (Gemini Vision) | ~varies | Skip image-based specs initially |
| S3 upload + observability | ~60s | Not needed for lite results |
| Supplier capability mapping | ~45s | Basic supplier type is enough |
| Jina reranking | ~varies | LLM scoring replaces this |

### SIMPLIFIED (saves ~3 min)

| Feature | Before | After | Time Saved |
|---------|--------|-------|------------|
| Keyword generation | Multi-round + research | Single call → 3 keywords | ~60s |
| LLM qualification | Per-product calls (200-400x) | Single batch call (all products) | ~3.5 min |
| Spec matching | Vector + LLM + Vision per spec | Direct field matching (price/MOQ/category) | ~2 min |
| Search endpoints | 6+ with rate limiting | 2 (1688 + Alibaba TMAPI) | ~60s |

### KEPT (core value)

| Feature | Why Keep |
|---------|----------|
| 1688 search (first page) | Primary supplier platform |
| Alibaba search (first page) | Secondary supplier platform |
| LLM-based scoring | Core intelligence for ranking |
| Price/MOQ filtering | Critical business requirements |
| Basic deduplication | Prevents duplicate results |

---

## Implementation Plan

### Phase 1: Understand & Measure - COMPLETED
- [x] Deep-read sourcing-logic codebase (47+ tools, 25+ workflows, 3 agents)
- [x] Deep-read spec-matching codebase (3-stage pipeline: Veto → Rerank → Supplier)
- [x] Map full pipeline flow (13 steps from SR input to ranked output)
- [x] Identify bottlenecks (cluster dedup 42%, shallow search 28%, LLM qual 26%)
- [x] Run sourcing-logic end-to-end: **3.0 min** for 787 products (water-bottle SR)
- [x] Run spec-matching end-to-end: **6.9 min** for 57 products (army coasters SR)
- [x] Combined end-to-end estimate: **~10 min total**

### Phase 2: Design Lite Pipeline (NEXT)
- [ ] Define minimal input schema (from Scube SR JSON)
- [ ] Define minimal output schema
- [ ] Design single-file architecture (Python recommended for simplicity)
- [ ] Mock up the 5-step flow
- [ ] Validate with sample data

### Phase 3: Build Core (Estimated: 1-2 days)
- [ ] Step 1: Fast keyword generator (1 LLM call)
- [ ] Step 2: Parallel search (1688 + Alibaba, first page only)
- [ ] Step 3: Simple dedup (item_id only)
- [ ] Step 4: Batch LLM filter (single call, all 30-60 products)
- [ ] Step 5: Lightweight spec verification (field matching, top 10-15 only)
- [ ] End-to-end test targeting <15s

### Phase 4: Optimize to 15s
- [ ] Profile each step
- [ ] Optimize API calls (connection reuse, asyncio.gather)
- [ ] Consider caching (Redis for repeated searches)
- [ ] Consider streaming responses
- [ ] Benchmark: 10 different SRs, measure p50/p95

### Phase 5: Integration
- [ ] Accept Scube SR JSON format
- [ ] Return compatible output format
- [ ] API endpoint (FastAPI or Express)
- [ ] Error handling + retry logic

---

## Key Technical Decisions Needed

1. **Language:** TypeScript (reuse existing tools) or Python (simpler, FastAPI)?
2. **LLM for batch scoring:** GPT-4o-mini (proven, ~2s) or Gemini 2.0 Flash (faster, cheaper)?
3. **Search API:** TMAPI only (simpler) or include Native 1688 (richer data)?
4. **Output format:** Same as current (Firestore compatible) or simplified JSON?
5. **Hosting:** Standalone script, Docker, or Cloud Run?

---

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| Lower recall (fewer products) | First page typically has highest relevance; iterate later |
| Less accurate scoring (batch vs per-product) | Modern LLMs handle batch well; verify with A/B test |
| No image-based matching | Add as optional Phase 2 enhancement |
| TMAPI rate limits on parallel calls | Only 2 calls, well within limits |
| Batch LLM call too large | 30-60 products fits in one context window easily |

---

## Proposed Lite Flow Diagram

```
                    ┌─────────────────────┐
                    │   Scube SR Input     │
                    │  (JSON: title, specs,│
                    │   images, price, MOQ)│
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  STEP 1: Keyword Gen │  ~1s
                    │  (1x GPT-4o-mini)    │
                    │  → 3 keywords (EN+CN)│
                    └──────────┬──────────┘
                               │
                 ┌─────────────┴─────────────┐
                 │                           │
        ┌────────▼────────┐        ┌────────▼────────┐
        │  1688 Search    │        │  Alibaba Search │   ~5s
        │  (first page)   │        │  (first page)   │   PARALLEL
        │  20-40 items    │        │  20-40 items    │
        └────────┬────────┘        └────────┬────────┘
                 │                           │
                 └─────────────┬─────────────┘
                               │
                    ┌──────────▼──────────┐
                    │  STEP 3: Dedup      │  ~0.5s
                    │  (item_id match)    │
                    │  30-60 products     │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  STEP 4: Batch LLM  │  ~6s
                    │  Score ALL products │
                    │  in ONE call        │
                    │  (GPT-4o-mini)      │
                    │  → Scored + ranked  │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  STEP 5: Quick Spec │  ~5s
                    │  Verify top 10-15   │
                    │  (field matching)   │
                    │  Price ✓ MOQ ✓      │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  OUTPUT: Top 10     │
                    │  Ranked Products    │
                    │  with scores        │
                    └─────────────────────┘

                    Total: ~12-15 seconds
```

---

## Phase 1 Summary

Phase 1 is complete with real benchmarks.

**Key insight from benchmarks:** The actual bottleneck distribution is different from initial estimates:
- Cluster dedup is the #1 bottleneck (42%), not LLM qualification (26%)
- The full pipeline ran in 3 min (not 10-15 min) because some steps run in parallel
- Spec matching is the second biggest cost at ~7 min for 57 products
- Combined total: ~10 min end-to-end (validates original estimate)

**For Lite version, the critical optimizations are:**
1. Skip cluster dedup entirely (saves 75s) - simple dedup is enough for 40-80 products
2. Skip exploratory search (saves 37s) - first page search is sufficient
3. Batch LLM qualification into 1 call (saves ~44s)
4. Replace full spec matching with field-level checks (saves ~7 min)
5. Only search 2 endpoints instead of 6+ (saves ~30s in rate-limit delays)

---
---

# PHASE 2: LITE PIPELINE DESIGN

> **Decisions confirmed:** Python, localhost prototype, 20-30s target
> **Agentic re-check complete:** spec-matching-v2.5 has experimental agentic mode; sourcing-logic is hybrid (agentic agents inside fixed DAG workflows)
> **Engineer's requirement:** "agentic spec matching flow" → aligns with existing `src/agentic/` in spec-matching-v2.5

---

## Design Philosophy

### The Core Insight

The current system's 10-minute runtime comes from **two independent bottlenecks**:

1. **Sourcing Logic (3 min):** Searches 6+ endpoints with rate-limit delays, clusters 787 products via LLM, scores each product individually
2. **Spec Matching (7 min):** Runs an 8-stage "staircase" of fallbacks per-product-per-spec (vector embed → LLM validate → LLM compare → title extract → image analyze → ...)

The lite version collapses BOTH into a single unified pipeline by:
- **Searching less** (2 endpoints, first page only → 40-80 products)
- **Pre-filtering faster** (price/MOQ math, not LLM → 0.1s)
- **Matching smarter** (agentic: 1 Gemini call per product with tool-calling → replaces 8-stage staircase)

### Why Agentic Over Batch-LLM

Our Phase 1 proposal used a "batch LLM" approach (one giant call scoring all products). After the deep-dive, the **agentic approach is better** because:

| Factor | Batch LLM (1 call) | Agentic (1 call/product) |
|--------|--------------------|-----------------------|
| **Accuracy** | Lower (products blur together in 1 context) | Higher (focused per-product analysis) |
| **Tool access** | None (pure text reasoning) | 5 tools (calculate, convert, image analyze, web search) |
| **Image analysis** | Cannot analyze product images | Can call Gemini Vision per-product |
| **Spec depth** | Shallow (surface-level matching) | Deep (attribute lookup, unit conversion, calculation) |
| **Speed at 30-50 products** | ~3-5s | ~10-20s (10 concurrent) |
| **Alignment with engineer** | No agentic flow | True agentic flow ✓ |
| **Code reuse** | Write from scratch | Adapt existing `src/agentic/` from spec-matching-v2.5 |

**Trade-off:** Agentic is ~10-15s slower than batch-LLM, but fits within 20-30s budget and delivers much better quality matching.

---

## Pipeline Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    SOURCING-LOGIC-LITE                           │
│                    Python 3.12 / FastAPI                         │
│                    Target: 20-30 seconds                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  INPUT: Scube SR JSON                                           │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │ {                                                          │ │
│  │   "title": "Eco-Friendly Water Bottle",                   │ │
│  │   "description": "Sustainable bottle...",                  │ │
│  │   "target_price": 6.5,                                    │ │
│  │   "target_quantity": 500,                                  │ │
│  │   "reference_images": ["https://..."],                     │ │
│  │   "veto_specs": [...],                                     │ │
│  │   "rerank_specs": [...]                                    │ │
│  │ }                                                          │ │
│  └────────────────────┬───────────────────────────────────────┘ │
│                       │                                         │
│  ┌────────────────────▼───────────────────────────────────────┐ │
│  │  STEP 1: KEYWORD GENERATION                    ~1-2s       │ │
│  │  ─────────────────────────────────────────────────         │ │
│  │  Model: gemini-2.0-flash (single call)                     │ │
│  │  Input: SR title + description                             │ │
│  │  Output: {                                                 │ │
│  │    en_keywords: ["eco water bottle", "recycled bottle"],   │ │
│  │    cn_keywords: ["环保水壶", "再生材料水瓶"]                  │ │
│  │  }                                                         │ │
│  │  Prompt: Generate 2 EN + 3 CN search keywords for          │ │
│  │          sourcing this product on 1688/Alibaba.             │ │
│  └────────────────────┬───────────────────────────────────────┘ │
│                       │                                         │
│           ┌───────────┴───────────┐                             │
│           │                       │                             │
│  ┌────────▼────────┐   ┌─────────▼────────┐                    │
│  │ STEP 2A: 1688   │   │ STEP 2B: Alibaba │       ~2-4s       │
│  │ CN Search       │   │ TMAPI Search     │       PARALLEL    │
│  │ ──────────────  │   │ ──────────────── │                    │
│  │ API: TMAPI      │   │ API: TMAPI       │                    │
│  │ Keywords: CN[0] │   │ Keywords: EN[0]  │                    │
│  │ Page: 1         │   │ Page: 1          │                    │
│  │ ~20-40 products │   │ ~20-40 products  │                    │
│  └────────┬────────┘   └─────────┬────────┘                    │
│           │                       │                             │
│           └───────────┬───────────┘                             │
│                       │                                         │
│  ┌────────────────────▼───────────────────────────────────────┐ │
│  │  STEP 3: PRE-FILTER                            ~0.1s       │ │
│  │  ─────────────────────────────────────────────────         │ │
│  │  3a. Dedup by item_id (exact match)                        │ │
│  │  3b. Dedup by image URL (exact match)                      │ │
│  │  3c. Price filter: reject if price > target * 1.5          │ │
│  │  3d. MOQ filter: reject if MOQ > target_qty * 2            │ │
│  │  Result: 20-50 products                                    │ │
│  └────────────────────┬───────────────────────────────────────┘ │
│                       │                                         │
│  ┌────────────────────▼───────────────────────────────────────┐ │
│  │  STEP 4: AGENTIC SPEC MATCHING               ~10-20s      │ │
│  │  ─────────────────────────────────────────────────         │ │
│  │  Model: gemini-2.0-flash (1 call/product, 15 concurrent)  │ │
│  │  Max tool turns: 8                                         │ │
│  │                                                            │ │
│  │  Per product, Gemini receives:                             │ │
│  │  - Product context (title, price, MOQ, image URLs)         │ │
│  │  - All veto specs + rerank specs                           │ │
│  │  - Reference images from SR                                │ │
│  │                                                            │ │
│  │  Tools available to agent:                                 │ │
│  │  ┌──────────────────────────────────────────────────────┐  │ │
│  │  │ calculate(expression) → float                        │  │ │
│  │  │   e.g. "6.5 * 7.2" for CNY→USD conversion          │  │ │
│  │  │                                                      │  │ │
│  │  │ convert_unit(value, from_unit, to_unit) → float      │  │ │
│  │  │   e.g. "500ml" to "oz" → 16.9                      │  │ │
│  │  │                                                      │  │ │
│  │  │ analyze_image(image_url, question) → str             │  │ │
│  │  │   Gemini Vision: check visual specs (color, finish)  │  │ │
│  │  │                                                      │  │ │
│  │  │ extract_from_title(title, spec_name) → str           │  │ │
│  │  │   Regex + fuzzy match to pull specs from title       │  │ │
│  │  └──────────────────────────────────────────────────────┘  │ │
│  │                                                            │ │
│  │  Output per product:                                       │ │
│  │  - Per-spec: match_type (MATCH/NOT_MATCH/UNKNOWN)         │ │
│  │  - Per-spec: confidence (1-5), reasoning, product_value    │ │
│  │  - Verdict: shortlisted / eliminated / pending             │ │
│  │  - Veto score (weighted)                                   │ │
│  │  - Rerank score (weighted)                                 │ │
│  └────────────────────┬───────────────────────────────────────┘ │
│                       │                                         │
│  ┌────────────────────▼───────────────────────────────────────┐ │
│  │  STEP 5: RANK + OUTPUT                         ~0.1s       │ │
│  │  ─────────────────────────────────────────────────         │ │
│  │  5a. Veto filter: eliminate products failing mandatory      │ │
│  │  5b. Relaxation: if < 5 shortlisted, relax low-weight      │ │
│  │      veto specs (score <= 5), max 2 specs relaxed          │ │
│  │  5c. Supplier score: type(50%) + service(30%) + cat(20%)   │ │
│  │  5d. Combined: rerank_score + supplier_score               │ │
│  │  5e. Sort descending, return top N                         │ │
│  │                                                            │ │
│  │  Output: Top 10-15 ranked products with full match details │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                 │
│  TOTAL: ~13-26 seconds                                          │
└─────────────────────────────────────────────────────────────────┘
```

---

## Detailed Step Design

### Step 1: Keyword Generation

**What:** Generate optimized search keywords for 1688 and Alibaba from the SR.

**Model:** `gemini-2.0-flash` (fast, cheap, good at structured output)

**Prompt Design:**
```
You are a sourcing expert. Given a product requirement, generate search keywords
optimized for Chinese B2B platforms (1688.com and Alibaba.com).

Product: {title}
Description: {description}

Return JSON:
{
  "en_keywords": ["keyword1", "keyword2"],   // 2 English keywords for Alibaba
  "cn_keywords": ["关键词1", "关键词2", "关键词3"]  // 3 Chinese keywords for 1688
}

Rules:
- Keywords should be product-focused (not feature-focused)
- EN keywords: broad product category + specific material/type
- CN keywords: direct Chinese product names used on 1688
- Prioritize keywords that match actual supplier listings
```

**Why 2 EN + 3 CN:** 1688 has more products, CN keywords give better results. Alibaba works well with EN. We use only the first keyword per platform for search (more keywords = more API calls = more time).

**Input:** `{ title: str, description: str }`
**Output:** `{ en_keywords: list[str], cn_keywords: list[str] }`
**Time:** 1-2s (single Gemini call)

---

### Step 2: Parallel Product Search

**What:** Search 1688 and Alibaba simultaneously using TMAPI.

**API calls (parallel):**

| Call | Endpoint | Keyword | Expected |
|------|----------|---------|----------|
| 2A | `GET http://api.tmapi.top/1688/search/items` | `cn_keywords[0]` | 20-40 products |
| 2B | `GET http://api.tmapi.top/alibaba/search/items` | `en_keywords[0]` | 20-40 products |

**TMAPI Response Fields Used:**
```python
# 1688 search result
{
    "item_id": "742069508527",
    "title": "环保再生材料水壶500ml...",
    "img": "https://cbu01.alicdn.com/...",
    "offer_price": "15.00-22.00",  # CNY range
    "min_order_quantity": 100,
    "shop_info": {
        "shop_name": "义乌市某某工厂",
        "member_id": "b2b-12345",
        "seller_login_id": "factory123",
        "tp_year": 5,           # years on platform
        "is_factory": true,
        "comprehensive_rating": 4.8,
        "location": "浙江 义乌"
    },
    "platform": "1688",
    "url": "https://detail.1688.com/offer/742069508527.html"
}

# Alibaba search result (similar structure)
{
    "item_id": "1600854321",
    "title": "Eco Friendly Water Bottle 500ml Recycled...",
    "img": "https://s.alicdn.com/@sc04/...",
    "offer_price": "2.50-4.00",  # USD range
    "min_order_quantity": 500,
    "shop_info": { ... },
    "platform": "alibaba",
    "url": "https://www.alibaba.com/product-detail/..."
}
```

**Price Normalization:** Extract lowest price from range ("15.00-22.00" → 15.00), convert 1688 CNY to USD (÷ 7.2 hardcoded rate, or 1 API call).

**Input:** `{ en_keywords: list[str], cn_keywords: list[str], api_token: str }`
**Output:** `list[RawProduct]` (40-80 products)
**Time:** 2-4s (parallel, first call is fast; 5.5s delay only on retries)

---

### Step 3: Pre-Filter

**What:** Fast local filtering. No API calls, pure computation.

**Sub-steps:**
1. **Dedup by item_id:** Remove exact duplicate product IDs across platforms
2. **Dedup by image URL:** Remove products with identical product images
3. **Price filter:** Remove if `lowest_price_usd > target_price * 1.5`
   - Why 1.5x: Suppliers negotiate, catalog price is higher than actual
4. **MOQ filter:** Remove if `min_order_quantity > target_quantity * 2`
   - Why 2x: MOQ is often negotiable, especially for repeat orders

**Input:** `list[RawProduct]` + `{ target_price: float, target_quantity: int }`
**Output:** `list[RawProduct]` (20-50 products)
**Time:** <0.1s

---

### Step 4: Agentic Spec Matching (THE CORE)

**What:** For each product, run a Gemini agent with tool access that evaluates ALL specs (veto + rerank) in a single conversational loop.

**Model:** `gemini-2.0-flash` (proven in production, lower cost than gemini-3-flash-preview)
**Concurrency:** `asyncio.Semaphore(15)` — 15 products simultaneously
**Max tool turns:** 8 per product
**Expected time:** 2-4s per product, 10-20s total for 30-50 products (in 2-3 concurrent batches)

#### Agent System Prompt (adapted from spec-matching-v2.5 `src/agentic/prompt_builder.py`)

```
You are a product sourcing specialist evaluating whether a supplier product
matches a buyer's specifications. You have tools to help you analyze.

MATCHING RULES:
- For each spec, determine: MATCH, NOT_MATCH, or UNKNOWN
- VETO specs are mandatory: NOT_MATCH on ANY veto spec = product eliminated
- RE-RANK specs are preferences: affect ranking score, not elimination
- Prefer "UNKNOWN" over guessing. Only say NOT_MATCH if clearly contradicted.

MATCHING STRATEGIES BY SPEC TYPE:
- Numeric specs (capacity, weight, dimensions):
  Use calculate() and convert_unit() for precise comparison.
  "within_10_percent" means product value must be within ±10% of spec value.
- Visual specs (color, finish, material appearance):
  Use analyze_image() on the product image.
- Functional specs (BPA-free, dishwasher safe, leak-proof):
  Check product title for keywords. If not found, use analyze_image().
- Category specs (product_category):
  Match against product title directly.

OUTPUT FORMAT (JSON):
{
  "specs": [
    {
      "spec_id": 1,
      "spec_name": "product_category",
      "match_type": "MATCH",
      "product_value": "water bottle",
      "confidence": 5,
      "reasoning": "Title clearly states 'water bottle'",
      "tools_used": []
    },
    ...
  ]
}
```

#### Agent User Message (built per product)

```
PRODUCT TO EVALUATE:
- Title: {product.title}
- Price: {product.offer_price} {currency}
- MOQ: {product.min_order_quantity} pcs
- Image: {product.img}
- Platform: {product.platform}

SPECIFICATIONS TO CHECK:

VETO SPECS (mandatory - product eliminated if ANY fails):
1. [spec_id=1] product_category
   Mandatory values: ["water bottle", "reusable bottle"]
   Unacceptable values: ["disposable bottle"]
   Matching rule: exact_match

2. [spec_id=2] feature_safety
   Mandatory values: ["dishwasher safe"]
   Matching rule: exact_match
   ...

RE-RANK SPECS (preferences - affects ranking score):
5. [spec_id=5] material_composition
   Preferred values: ["recycled materials", "rPET"]
   Matching rule: keyword_match
   ...

Evaluate ALL specs above. Use tools when needed.
```

#### Tools (4 tools, simplified from spec-matching-v2.5's 5)

| Tool | Signature | Purpose | Implementation |
|------|-----------|---------|---------------|
| `calculate` | `(expression: str) → str` | Safe math eval | `ast.literal_eval` or `simpleeval` library |
| `convert_unit` | `(value: float, from_unit: str, to_unit: str) → str` | Unit conversion | Pre-built lookup tables (ml↔oz, cm↔inch, kg↔lb, etc.) |
| `analyze_image` | `(image_url: str, question: str) → str` | Gemini Vision | `gemini-2.0-flash` with image part, answers specific visual questions |
| `extract_from_title` | `(title: str, spec_name: str) → str` | Title spec extraction | Regex patterns + fuzzy keyword matching in title |

**Why we removed `get_answer` (Google Search):** It adds latency (3-5s per call) and is rarely decisive. The title + image tools cover 95% of cases.

**Why we removed `get_attribute_value`:** TMAPI search results don't include detailed product attributes (unlike Firestore product documents in the full pipeline). Title + image analysis is sufficient for lite.

#### Agent Loop (pseudocode)

```python
async def match_product(product, specs, semaphore):
    async with semaphore:  # max 15 concurrent
        # Build messages
        messages = [
            {"role": "user", "content": build_user_message(product, specs)}
        ]

        # Tool-calling loop
        for turn in range(8):
            response = await gemini.generate(
                model="gemini-2.0-flash",
                system=SYSTEM_PROMPT,
                contents=messages,
                tools=TOOL_DECLARATIONS,
                config={"temperature": 0.0, "response_mime_type": "application/json"}
            )

            # Check for tool calls
            tool_calls = extract_tool_calls(response)
            if not tool_calls:
                break  # Final text response

            # Execute tools in parallel
            tool_results = await asyncio.gather(*[
                execute_tool(tc) for tc in tool_calls
            ])

            # Append tool results to conversation
            messages.append(response)
            messages.append(tool_results)

        # Parse final JSON response
        return parse_spec_results(response.text, product)
```

**Input:** `list[RawProduct]` + `{ veto_specs: list, rerank_specs: list }`
**Output:** `list[MatchedProduct]` with per-spec results
**Time:** 10-20s (15 concurrent, 2-3 batches)

---

### Step 5: Rank + Output

**What:** Apply veto filtering, relaxation, supplier scoring, and final ranking.

**Sub-steps:**

#### 5a. Veto Filtering
```python
for product in matched_products:
    has_not_match = any(s.match_type == "NOT_MATCH" for s in product.veto_results)
    has_unknown_high = any(
        s.match_type == "UNKNOWN" and s.veto_score > 5
        for s in product.veto_results
    )
    if has_not_match or has_unknown_high:
        product.verdict = "eliminated"
    elif all(s.match_type == "MATCH" for s in product.veto_results):
        product.verdict = "shortlisted"
    else:
        product.verdict = "pending"  # has UNKNOWN on low-weight specs
```

#### 5b. Relaxation (if < 5 shortlisted)
```python
if count_shortlisted < 5:
    # Promote PENDING products (those with only low-weight UNKNOWNs)
    for product in pending_products:
        relaxable = [s for s in product.veto_results
                     if s.match_type == "UNKNOWN" and s.veto_score <= 5]
        if len(relaxable) <= 2:
            product.verdict = "shortlisted"
            product.relaxed_specs = [s.spec_name for s in relaxable]
```

#### 5c. Supplier Scoring (pure computation, from spec-matching-v2.5)
```python
def score_supplier(shop_info):
    # Type score (0-3)
    type_score = 2 if shop_info.get("is_factory") else 0
    if shop_info.get("tp_year", 0) >= 5:
        type_score = min(type_score + 1, 3)

    # Service score (0-2)
    rating = shop_info.get("comprehensive_rating", 0)
    service_score = 2 if rating >= 4.5 else (1 if rating >= 4.0 else 0)

    return 0.5 * (type_score/3) + 0.3 * (service_score/2) + 0.2 * 0  # category=0
```

#### 5d. Combined Score + Sort
```python
# Rerank score from agentic matching (0.0 - 1.0)
# Supplier score (0.0 - 1.0)
combined_score = product.rerank_score + product.supplier_score
products.sort(key=lambda p: p.combined_score, reverse=True)
return products[:num_options]  # Default: top 10
```

**Input:** `list[MatchedProduct]`
**Output:** `list[RankedProduct]` (top 10-15)
**Time:** <0.1s

---

## Timing Budget

| Step | What | Time | Concurrent |
|------|------|------|------------|
| 1 | Keyword generation (1 Gemini call) | 1-2s | — |
| 2 | Product search (2 TMAPI calls) | 2-4s | ✓ parallel |
| 3 | Pre-filter (local computation) | <0.1s | — |
| 4 | Agentic spec matching (15 concurrent) | 10-20s | ✓ parallel |
| 5 | Rank + output (local computation) | <0.1s | — |
| **TOTAL** | | **13-26s** | |

### Worst Case Analysis

| Factor | Worst Case | Impact |
|--------|-----------|--------|
| TMAPI slow response | 6s (instead of 2s) | +4s |
| Many products (60+) | 4 batches of 15 | +5s |
| Agent needs many tool turns (image analysis) | 8 turns × 2s each | +10s/batch |
| Gemini rate limiting | 429 + 1 retry | +3s |
| **Worst case total** | | **~35s** |

Worst case exceeds 30s but is unlikely (requires all worst cases simultaneously). P95 should be under 30s.

---

## Input/Output Schemas

### Input Schema (Scube SR JSON)

```python
class SourcingRequest(BaseModel):
    """Input from Scube - same structure as sourcing-logic's workflow input."""

    # Required
    title: str                          # "Eco-Friendly Water Bottle"
    description: str                    # "Sustainable water bottle..."

    # Specs (at least one of veto or rerank required)
    veto_specs: list[VetoSpec]          # Mandatory specs (product eliminated if fails)
    rerank_specs: list[ReRankSpec] = [] # Preference specs (affects ranking)

    # Commercial
    target_price: float = 0            # USD, 0 = no price filter
    target_price_currency: str = "USD"
    target_quantity: int = 0           # pcs, 0 = no MOQ filter
    target_quantity_unit: str = "pcs"

    # Images
    reference_images: list[str] = []   # URLs for visual comparison

    # Options
    num_options: int = 10              # How many results to return
    needs_customization: bool = False  # Currently unused in lite

class VetoSpec(BaseModel):
    spec_id: int
    spec_name: str
    mandatory_values: list[str]
    unacceptable_values: list[str] = []
    spec_type: str = "Functional"      # Functional, Numeric, Visual, Commercial
    matching_rule: str = "exact_match"  # exact_match, keyword_match, semantic_match, within_10_percent
    veto_score: int = 5                # Weight (1-10), higher = harder to relax

class ReRankSpec(BaseModel):
    spec_id: int
    spec_name: str
    acceptable_values: list[str]
    spec_type: str = "Functional"
    matching_rule: str = "keyword_match"
    re_rank_score: int = 3             # Weight contribution when matched
```

### Output Schema

```python
class SourcingResult(BaseModel):
    """Output of the lite pipeline."""

    # Metadata
    run_id: str
    request_title: str
    total_searched: int               # Products found by search
    total_after_prefilter: int        # Products after price/MOQ filter
    total_matched: int                # Products after spec matching
    total_shortlisted: int            # Products passing veto
    execution_time_seconds: float

    # Results
    products: list[RankedProduct]     # Top N ranked products

    # Debug (optional, for prototype)
    step_timings: dict[str, float]    # Per-step timing breakdown

class RankedProduct(BaseModel):
    # Product info
    product_id: str                   # item_id from TMAPI
    title: str
    image_url: str
    product_url: str
    platform: str                     # "1688" or "alibaba"
    price: float                      # Normalized to USD
    price_original: str               # Raw price string
    moq: int
    currency: str                     # Original currency

    # Supplier info
    supplier_name: str
    supplier_location: str
    supplier_is_factory: bool
    supplier_rating: float
    supplier_years: int

    # Matching results
    verdict: str                      # "shortlisted" / "eliminated" / "pending"
    veto_score: float                 # 0.0-1.0 weighted
    rerank_score: float               # 0.0-1.0 weighted
    supplier_score: float             # 0.0-1.0
    combined_score: float             # rerank + supplier
    rank: int                         # 1-based rank

    # Per-spec details
    spec_results: list[SpecMatchResult]
    relaxed_specs: list[str] = []     # Specs that were relaxed

class SpecMatchResult(BaseModel):
    spec_id: int
    spec_name: str
    match_type: str                   # "MATCH" / "NOT_MATCH" / "UNKNOWN"
    product_value: str | None         # What the agent found
    confidence: int                   # 1-5
    reasoning: str                    # Why this verdict
    tools_used: list[str] = []        # Which tools the agent called
```

---

## Project Structure

```
SourcingLogicLiteVersion/
├── readme.MD                    # This document
├── .env                         # API keys (existing)
├── credentials.json             # Google credentials (existing)
├── requirements.txt             # Python dependencies (NEW)
│
├── main.py                      # Entry point: CLI + FastAPI (NEW)
│
├── pipeline/                    # Core pipeline modules (NEW)
│   ├── __init__.py
│   ├── config.py                # Env vars, constants, settings
│   ├── models.py                # Pydantic schemas (input/output)
│   ├── keyword_gen.py           # Step 1: Gemini keyword generation
│   ├── search.py                # Step 2: TMAPI product search
│   ├── prefilter.py             # Step 3: Dedup + price/MOQ filter
│   ├── matcher.py               # Step 4: Agentic spec matching loop
│   ├── tools.py                 # Step 4 tools: calculate, convert, image, title
│   └── ranker.py                # Step 5: Veto filter + relaxation + scoring
│
├── test_data/                   # Test inputs (NEW, copied from sourcing-logic)
│   ├── water-bottle.json        # From sourcing-logic test data
│   └── icecream-container.json  # Additional test SR
│
└── tests/                       # Unit tests (Phase 4)
    └── test_pipeline.py
```

---

## Dependencies (requirements.txt)

```
# Core
google-genai>=1.0.0              # Gemini SDK (new unified SDK)
pydantic>=2.0                    # Data validation
httpx>=0.27                      # Async HTTP client (for TMAPI)
python-dotenv>=1.0               # .env loading

# API
fastapi>=0.115                   # REST API server
uvicorn>=0.30                    # ASGI server

# Utilities
simpleeval>=1.0                  # Safe math expression evaluation
```

**Total: 6 dependencies** (vs 20+ in spec-matching-v2.5, 955 in sourcing-logic)

**What we DON'T need:**
- ❌ `sentence-transformers` / `torch` (no embedding model)
- ❌ `numpy` (no vector math)
- ❌ `google-cloud-firestore` (no Firestore)
- ❌ `boto3` (no S3)
- ❌ `langfuse` (no observability)
- ❌ `redis` (no caching layer)
- ❌ `jina` (no reranking service)

---

## Environment Variables Needed

```bash
# Only 2 required!
GEMINI_API_KEY=AIzaSy...        # For keyword gen + agentic matching
TM_API_KEY=eyJhbGci...          # For TMAPI product search

# Optional
USD_TO_CNY_RATE=7.2             # Default exchange rate (fallback)
MAX_CONCURRENT=15               # Gemini concurrency limit
MAX_TOOL_TURNS=8                # Agent tool-calling turns limit
LOG_LEVEL=INFO                  # Logging verbosity
```

Both keys already exist in the `.env` file:
- `GEMINI_API_KEY` → existing `GOOGLE_GENERATIVE_AI_API_KEY`
- `TM_API_KEY` → existing `TM_API_KEY`

---

## How to Run (Prototype)

### CLI Mode (for testing)
```bash
cd SourcingLogicLiteVersion
pip install -r requirements.txt
python main.py test_data/water-bottle.json
```

### API Mode (for integration)
```bash
cd SourcingLogicLiteVersion
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000

# Then:
curl -X POST http://localhost:8000/source \
  -H "Content-Type: application/json" \
  -d @test_data/water-bottle.json
```

### Expected Output (water-bottle SR)
```json
{
  "run_id": "lite-water-bottle-20260311",
  "request_title": "Eco-Friendly Water Bottle",
  "total_searched": 67,
  "total_after_prefilter": 42,
  "total_matched": 42,
  "total_shortlisted": 8,
  "execution_time_seconds": 18.4,
  "step_timings": {
    "keyword_gen": 1.2,
    "search": 3.1,
    "prefilter": 0.05,
    "agentic_matching": 13.8,
    "ranking": 0.03
  },
  "products": [
    {
      "rank": 1,
      "product_id": "742069508527",
      "title": "BPA-Free Eco Water Bottle 500ml Recycled Material...",
      "platform": "1688",
      "price": 2.78,
      "moq": 200,
      "verdict": "shortlisted",
      "combined_score": 1.45,
      "veto_score": 1.0,
      "rerank_score": 0.82,
      "supplier_score": 0.63,
      "spec_results": [
        {
          "spec_id": 1,
          "spec_name": "product_category",
          "match_type": "MATCH",
          "product_value": "water bottle",
          "confidence": 5,
          "reasoning": "Title explicitly mentions 'Water Bottle'",
          "tools_used": []
        }
      ]
    }
  ]
}
```

---

## Key Design Decisions Explained

### 1. Why gemini-2.0-flash (not gemini-3-flash-preview)?
- gemini-2.0-flash is proven in production spec-matching pipeline
- gemini-3-flash-preview is "preview" — less stable, may change
- 2.0-flash has sufficient tool-calling capability
- Lower cost per call
- If 2.0-flash proves insufficient during testing, easy to swap to 3-flash

### 2. Why 15 concurrent (not 10 or 20)?
- spec-matching-v2.5 agentic uses 10, production staircase uses 20
- 15 is a balanced middle ground for prototype
- Google's Gemini API free tier allows ~15 RPM on flash models
- Tunable via `MAX_CONCURRENT` env var

### 3. Why 4 tools (not 5)?
- Removed `get_attribute_value` — TMAPI search results don't have detailed attributes
- Removed `get_answer` (Google Search) — too slow (+3-5s), rarely decisive
- Added `extract_from_title` — specialized for extracting specs from product titles (cheaper than LLM, faster than regex alone)
- `calculate`, `convert_unit`, `analyze_image` kept as-is from spec-matching-v2.5

### 4. Why no embedding model?
- The staircase pipeline's vector matching (gte-large) takes 73s just to load
- The agentic approach doesn't use embeddings — it uses title analysis + image analysis
- For 30-50 products, Gemini's reasoning is sufficient without vector similarity
- Eliminates GPU dependency entirely

### 5. Why not include Tavily/web research?
- Adds 5-10s per call
- First-page TMAPI search returns relevant products for 90%+ of SRs
- Web research is for edge cases (obscure products, very specific materials)
- Can be added as optional Phase 4 enhancement

---

## Phase 2 Complete — Implementation Phases

### Phase 2: Design ✅ (this document)

### Phase 3: Build Core (Next)

Build order (each step independently testable):

1. **`config.py` + `models.py`** — Set up schemas and env loading
2. **`keyword_gen.py`** — Test: title in → keywords out
3. **`search.py`** — Test: keywords in → products out
4. **`prefilter.py`** — Test: products in → filtered products out
5. **`tools.py`** — Test each tool independently
6. **`matcher.py`** — Test: 1 product + specs → match results (most complex)
7. **`ranker.py`** — Test: matched products → ranked output
8. **`main.py`** — Wire it all together, CLI + FastAPI
9. **End-to-end test** with water-bottle.json → target <30s

### Phase 4: Optimize
- Profile per-step timing
- Tune concurrency (find optimal semaphore value)
- Add connection pooling for TMAPI (httpx client reuse)
- Consider: pre-generate keywords for common product categories (cache)
- Consider: stream results to frontend as they complete

### Phase 5: Polish
- Error handling + retries for all external APIs
- Rate limit detection + backoff for Gemini
- Input validation (malformed SRs, missing fields)
- Logging with structured output (JSON logs)
- Optional: compare lite results vs full pipeline results for quality assessment
