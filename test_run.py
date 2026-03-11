"""Quick test run of the full pipeline."""
import asyncio
import json
import time

from pipeline.config import validate as validate_config
from pipeline.models import SourcingRequest
from pipeline.keyword_gen import generate_keywords
from pipeline.search import search_products
from pipeline.prefilter import prefilter_products
from pipeline.matcher import match_products
from pipeline.ranker import rank_products

validate_config()

# Load test data
with open("test_data/water-bottle.json") as f:
    data = json.load(f)
sr = SourcingRequest(**data)
req = sr.original_requirement
specs = sr.specs


async def run():
    total_start = time.time()

    # Step 1: Keywords
    t0 = time.time()
    print("Step 1: Generating keywords...")
    keywords = await generate_keywords(req.title, req.description)
    print(f"  Done in {time.time()-t0:.2f}s: EN={keywords.en_keywords}, CN={keywords.cn_keywords}")

    # Step 2: Search
    t0 = time.time()
    print("Step 2: Searching products...")
    raw = await search_products(keywords)
    print(f"  Done in {time.time()-t0:.2f}s: {len(raw)} products found")

    if not raw:
        print("No products found! Check TMAPI key and network.")
        return

    # Step 3: Pre-filter
    t0 = time.time()
    print("Step 3: Pre-filtering...")
    filtered = prefilter_products(
        raw, target_price_usd=req.target_price, target_quantity=req.target_quantity
    )
    print(f"  Done in {time.time()-t0:.4f}s: {len(filtered)} products after filter")

    if not filtered:
        print("All products filtered out! Try relaxing price/MOQ constraints.")
        return

    # Step 4: Agentic matching (limit to 10 for quick test)
    test_products = filtered[:10]
    t0 = time.time()
    print(f"Step 4: Agentic spec matching ({len(test_products)} products)...")
    matched = await match_products(test_products, specs.veto_specs, specs.re_rank_specs)
    print(f"  Done in {time.time()-t0:.2f}s: {len(matched)} products matched")

    # Step 5: Ranking
    t0 = time.time()
    print("Step 5: Ranking...")
    ranked = rank_products(
        matched, specs.veto_specs, specs.re_rank_specs, num_options=req.num_options
    )
    print(f"  Done in {time.time()-t0:.4f}s: {len(ranked)} products shortlisted")

    total = time.time() - total_start
    print(f"\n{'='*60}")
    print(f"TOTAL: {total:.2f}s")
    print(f"Shortlisted: {len(ranked)} products")
    print(f"{'='*60}")

    for p in ranked:
        print(f"\n  #{p.rank} [{p.verdict}] {p.title[:70]}")
        print(f"     Price: ${p.price:.2f} | MOQ: {p.moq} | Platform: {p.platform}")
        print(f"     Combined: {p.combined_score:.2f} | Veto: {p.veto_score:.2f} | Rerank: {p.rerank_score:.2f}")
        for s in p.spec_results:
            icon = {"MATCH": "+", "NOT_MATCH": "x", "UNKNOWN": "?"}.get(s.match_type, "?")
            print(f"       [{icon}] {s.spec_name}: {s.match_type} (conf={s.confidence}) {s.reasoning[:50]}")


asyncio.run(run())
