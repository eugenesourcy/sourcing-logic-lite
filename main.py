"""SourcingBot Lite - Main entry point (CLI + FastAPI API + UI)."""

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from pipeline.config import validate as validate_config
from pipeline.models import (
    SourcingRequest, SourcingResult, StepTiming, Keywords, SpecsInput,
)
from pipeline.keyword_gen import generate_keywords
from pipeline.search import search_products
from pipeline.prefilter import prefilter_products
from pipeline.matcher import match_products
from pipeline.ranker import rank_products
from pipeline.spec_gen import generate_specs

# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sourcing-lite")

# ============================================================================
# FASTAPI APP
# ============================================================================

app = FastAPI(
    title="SourcingBot Lite",
    description="Fast product sourcing with agentic spec matching (20-30s target)",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files (UI)
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the UI."""
    index_path = static_dir / "index.html"
    if index_path.exists():
        return HTMLResponse(content=index_path.read_text())
    return HTMLResponse(content="<h1>SourcingBot Lite</h1><p>POST to /source with SR JSON</p>")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "sourcing-lite"}


# ============================================================================
# PIPELINE ORCHESTRATOR
# ============================================================================

async def run_pipeline(
    request: SourcingRequest,
    on_step=None,
) -> SourcingResult:
    """Run the full lite sourcing pipeline."""
    start_time = time.time()
    timings = StepTiming()

    req = request.original_requirement
    specs = request.specs

    async def emit(step: str, status: str, **extra):
        if on_step:
            await on_step({"step": step, "status": status, **extra})

    needs_spec_gen = not specs.veto_specs and not specs.re_rank_specs

    if needs_spec_gen:
        # ── Run Spec Gen + Keyword Gen in PARALLEL ────────────────
        await emit("spec_gen", "running")
        await emit("keyword_gen", "running")
        t0 = time.time()

        spec_task = generate_specs(
            title=req.title,
            description=req.description,
            target_price=req.target_price,
            target_quantity=req.target_quantity,
        )
        keyword_task = generate_keywords(req.title, req.description)

        generated_specs, keywords = await asyncio.gather(spec_task, keyword_task)

        elapsed = time.time() - t0
        specs = generated_specs
        request.specs = generated_specs
        timings.spec_gen = round(elapsed, 2)
        timings.keyword_gen = round(elapsed, 2)

        await emit("spec_gen", "done", time=timings.spec_gen,
                   data={"veto": len(specs.veto_specs), "rerank": len(specs.re_rank_specs)})
        await emit("keyword_gen", "done", time=timings.keyword_gen,
                   data={"en": keywords.en_keywords, "cn": keywords.cn_keywords})
        logger.info(f"Parallel step done: {elapsed:.2f}s - Generated {len(specs.veto_specs)} veto, {len(specs.re_rank_specs)} rerank specs + {len(keywords.en_keywords)} EN, {len(keywords.cn_keywords)} CN keywords")
    else:
        # ── STEP 1: Keyword Generation (specs already provided) ──
        await emit("keyword_gen", "running")
        t0 = time.time()

        keywords = await generate_keywords(req.title, req.description)

        timings.keyword_gen = round(time.time() - t0, 2)
        await emit("keyword_gen", "done", time=timings.keyword_gen,
                   data={"en": keywords.en_keywords, "cn": keywords.cn_keywords})
        logger.info(f"Step 1 done: {timings.keyword_gen}s - {len(keywords.en_keywords)} EN, {len(keywords.cn_keywords)} CN keywords")

    # ── STEP 2: Product Search ──────────────────────────────────────
    await emit("search", "running")
    t0 = time.time()

    raw_products = await search_products(keywords)

    timings.search = round(time.time() - t0, 2)
    await emit("search", "done", time=timings.search,
               data={"count": len(raw_products)})
    logger.info(f"Step 2 done: {timings.search}s - {len(raw_products)} raw products")

    # ── STEP 3: Pre-Filter ──────────────────────────────────────────
    await emit("prefilter", "running")
    t0 = time.time()

    filtered = prefilter_products(
        raw_products,
        target_price_usd=req.target_price if req.target_price_currency == "USD" else 0,
        target_quantity=req.target_quantity,
    )

    timings.prefilter = round(time.time() - t0, 2)
    await emit("prefilter", "done", time=timings.prefilter,
               data={"count": len(filtered), "from": len(raw_products)})
    logger.info(f"Step 3 done: {timings.prefilter}s - {len(filtered)} products after filter")

    # ── STEP 4: Agentic Spec Matching ───────────────────────────────
    # Cap products to match: 3x num_options (enough for ranking after elimination)
    max_to_match = max(req.num_options * 3, 15)
    products_to_match = filtered[:max_to_match]
    logger.info(f"Matching {len(products_to_match)} of {len(filtered)} filtered products (cap={max_to_match})")

    await emit("agentic_matching", "running", data={"total": len(products_to_match)})
    t0 = time.time()

    match_progress = {"completed": 0}

    async def on_match_progress(completed: int, total: int):
        match_progress["completed"] = completed
        await emit("agentic_matching", "progress",
                    data={"completed": completed, "total": total})

    matched = await match_products(
        products_to_match,
        specs.veto_specs,
        specs.re_rank_specs,
        on_progress=on_match_progress,
    )

    timings.agentic_matching = round(time.time() - t0, 2)
    await emit("agentic_matching", "done", time=timings.agentic_matching,
               data={"matched": len(matched)})
    logger.info(f"Step 4 done: {timings.agentic_matching}s - {len(matched)} products matched")

    # ── STEP 5: Ranking ─────────────────────────────────────────────
    await emit("ranking", "running")
    t0 = time.time()

    ranked = rank_products(
        matched,
        specs.veto_specs,
        specs.re_rank_specs,
        num_options=req.num_options,
    )

    timings.ranking = round(time.time() - t0, 2)
    await emit("ranking", "done", time=timings.ranking,
               data={"shortlisted": len(ranked)})
    logger.info(f"Step 5 done: {timings.ranking}s - {len(ranked)} products ranked")

    # ── BUILD RESULT ────────────────────────────────────────────────
    total_time = round(time.time() - start_time, 2)

    result = SourcingResult(
        run_id=request.runId,
        request_title=req.title,
        total_searched=len(raw_products),
        total_after_prefilter=len(filtered),
        total_matched=len(matched),
        total_shortlisted=len(ranked),
        execution_time_seconds=total_time,
        step_timings=timings,
        products=ranked,
        keywords_generated=keywords,
    )

    await emit("complete", "done", time=total_time,
               data={"total_shortlisted": len(ranked)})
    logger.info(f"Pipeline complete: {total_time}s total, {len(ranked)} products")

    return result


# ============================================================================
# API ENDPOINTS
# ============================================================================

@app.post("/source")
async def source_products(request: SourcingRequest):
    """Run the sourcing pipeline and return results (non-streaming)."""
    validate_config()
    result = await run_pipeline(request)
    return result


@app.post("/source/stream")
async def source_products_stream(request: Request):
    """Run the sourcing pipeline with streaming progress updates (SSE)."""
    validate_config()

    body = await request.json()
    sr = SourcingRequest(**body)

    async def event_stream() -> AsyncGenerator[str, None]:
        result_holder = {}

        async def on_step(data: dict):
            yield_data = json.dumps(data)
            # Store for later (can't yield from callback directly)
            result_holder.setdefault("events", []).append(yield_data)

        # We need a different approach for SSE - use a queue
        queue: asyncio.Queue = asyncio.Queue()

        async def on_step_queue(data: dict):
            await queue.put(data)

        async def run_and_signal():
            try:
                result = await run_pipeline(sr, on_step=on_step_queue)
                await queue.put({"step": "result", "status": "done", "data": result.model_dump()})
            except Exception as e:
                await queue.put({"step": "error", "status": "error", "message": str(e)})
            await queue.put(None)  # Sentinel

        # Start pipeline in background
        task = asyncio.create_task(run_and_signal())

        # Yield events as they come
        while True:
            event = await queue.get()
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"

        await task

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/generate-specs")
async def generate_specs_endpoint(request: Request):
    """Generate veto/rerank specs from a product title + description, or raw query.

    Accepts either:
      {"title": "...", "description": "...", "target_price": 5, "target_quantity": 1000}
    or:
      {"query": "Coffee cup ceramic 300ml"}  (raw text → title)
    """
    validate_config()
    body = await request.json()

    # Support raw query OR structured title+description
    query = body.get("query", "")
    title = body.get("title", "") or query
    description = body.get("description", "")
    target_price = body.get("target_price", 0)
    target_quantity = body.get("target_quantity", 0)

    if not title:
        return {"error": "title or query is required"}

    specs = await generate_specs(
        title=title,
        description=description,
        target_price=float(target_price),
        target_quantity=int(target_quantity),
    )
    return specs.model_dump()


@app.get("/test-data")
async def get_test_data():
    """Return available test data files."""
    test_dir = Path(__file__).parent / "test_data"
    files = []
    if test_dir.exists():
        for f in sorted(test_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                title = data.get("original_requirement", {}).get("title", f.stem)
                files.append({"name": f.stem, "title": title, "path": f.name})
            except Exception:
                files.append({"name": f.stem, "title": f.stem, "path": f.name})
    return {"files": files}


@app.get("/test-data/{filename}")
async def get_test_file(filename: str):
    """Return a specific test data file."""
    test_dir = Path(__file__).parent / "test_data"
    file_path = test_dir / filename
    if not file_path.suffix:
        file_path = file_path.with_suffix(".json")
    if file_path.exists() and file_path.is_relative_to(test_dir):
        return json.loads(file_path.read_text())
    return {"error": f"File not found: {filename}"}


@app.get("/debug/search-test")
async def debug_search_test():
    """Debug endpoint to test TMAPI connectivity."""
    import httpx
    import traceback
    from pipeline.config import TM_API_KEY, TMAPI_BASE_URL

    results = {
        "tm_api_key_set": bool(TM_API_KEY),
        "tm_api_key_len": len(TM_API_KEY) if TM_API_KEY else 0,
        "tmapi_base_url": TMAPI_BASE_URL,
    }

    # Test 1688 search
    try:
        url = f"{TMAPI_BASE_URL}/1688/search/items"
        params = {"keyword": "coffee cup", "page": "1", "apiToken": TM_API_KEY}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params)
        results["1688_status"] = resp.status_code
        data = resp.json()
        results["1688_response_keys"] = list(data.keys()) if isinstance(data, dict) else str(type(data))
        data_obj = data.get("data", {})
        if isinstance(data_obj, dict):
            items = data_obj.get("items", data_obj.get("result", []))
            results["1688_item_count"] = len(items)
        elif isinstance(data_obj, list):
            results["1688_item_count"] = len(data_obj)
        else:
            results["1688_data_type"] = str(type(data_obj))
        results["1688_code"] = data.get("code")
        results["1688_msg"] = data.get("msg", "")[:200]
    except Exception as e:
        results["1688_error"] = f"{type(e).__name__}: {e}"
        results["1688_traceback"] = traceback.format_exc()[-500:]

    # Test Alibaba search
    try:
        url = f"{TMAPI_BASE_URL}/alibaba/search/items"
        params = {"keywords": "coffee cup", "page": "1", "apiToken": TM_API_KEY}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params)
        results["alibaba_status"] = resp.status_code
        data = resp.json()
        results["alibaba_response_keys"] = list(data.keys()) if isinstance(data, dict) else str(type(data))
        data_obj = data.get("data", {})
        if isinstance(data_obj, dict):
            items = data_obj.get("items", data_obj.get("result", []))
            results["alibaba_item_count"] = len(items)
        elif isinstance(data_obj, list):
            results["alibaba_item_count"] = len(data_obj)
        else:
            results["alibaba_data_type"] = str(type(data_obj))
        results["alibaba_code"] = data.get("code")
        results["alibaba_msg"] = data.get("msg", "")[:200]
    except Exception as e:
        results["alibaba_error"] = f"{type(e).__name__}: {e}"
        results["alibaba_traceback"] = traceback.format_exc()[-500:]

    return results


# ============================================================================
# CLI MODE
# ============================================================================

async def cli_main(input_path: str):
    """Run pipeline from CLI with a JSON input file."""
    validate_config()

    logger.info(f"Loading input from: {input_path}")
    with open(input_path) as f:
        data = json.load(f)

    sr = SourcingRequest(**data)
    logger.info(f"Running pipeline for: {sr.original_requirement.title}")

    async def on_step(data: dict):
        step = data.get("step", "")
        status = data.get("status", "")
        t = data.get("time", "")
        extra = data.get("data", {})
        if status == "running":
            print(f"  {'>'} {step}...", flush=True)
        elif status == "done":
            print(f"  {'>'} {step}: {t}s {extra}", flush=True)
        elif status == "progress":
            c = extra.get("completed", 0)
            tot = extra.get("total", 0)
            print(f"    [{c}/{tot}]", end="\r", flush=True)

    result = await run_pipeline(sr, on_step=on_step)

    print(f"\n{'='*60}")
    print(f"RESULTS: {result.request_title}")
    print(f"{'='*60}")
    print(f"Total time: {result.execution_time_seconds}s")
    print(f"Products searched: {result.total_searched}")
    print(f"After pre-filter: {result.total_after_prefilter}")
    print(f"Matched: {result.total_matched}")
    print(f"Shortlisted: {result.total_shortlisted}")
    print(f"\nTimings: {result.step_timings.model_dump()}")

    if result.products:
        print(f"\nTop {len(result.products)} Products:")
        for p in result.products:
            print(f"\n  #{p.rank} | {p.title[:60]}...")
            print(f"     Price: ${p.price:.2f} | MOQ: {p.moq} | Platform: {p.platform}")
            print(f"     Verdict: {p.verdict} | Combined: {p.combined_score}")
            print(f"     Veto: {p.veto_score} | Rerank: {p.rerank_score} | Supplier: {p.supplier_score}")
            if p.relaxed_specs:
                print(f"     Relaxed: {p.relaxed_specs}")
            for sr in p.spec_results:
                icon = {"MATCH": "+", "NOT_MATCH": "x", "UNKNOWN": "?"}
                print(f"       [{icon.get(sr.match_type, '?')}] {sr.spec_name}: {sr.match_type} (conf={sr.confidence}) - {sr.reasoning[:60]}")
    else:
        print("\nNo products shortlisted.")

    # Save results
    output_path = Path(input_path).parent / f"result-{Path(input_path).stem}.json"
    output_path.write_text(json.dumps(result.model_dump(), indent=2, default=str))
    print(f"\nResults saved to: {output_path}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        # CLI mode: python main.py <input.json>
        asyncio.run(cli_main(sys.argv[1]))
    else:
        # Server mode
        import uvicorn
        port = 8000
        for i, arg in enumerate(sys.argv):
            if arg == "--port" and i + 1 < len(sys.argv):
                port = int(sys.argv[i + 1])

        print(f"\n{'='*60}")
        print(f"  SourcingBot Lite - http://localhost:{port}")
        print(f"  UI: http://localhost:{port}/")
        print(f"  API: POST http://localhost:{port}/source")
        print(f"  Stream: POST http://localhost:{port}/source/stream")
        print(f"{'='*60}\n")

        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
