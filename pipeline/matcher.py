"""Step 4: Agentic Spec Matching - Gemini with tool-calling loop."""

import asyncio
import json
import logging
from typing import Optional, Callable
from google import genai
from google.genai import types as genai_types
from .config import GEMINI_API_KEY, GEMINI_MODEL, MAX_CONCURRENT, MAX_TOOL_TURNS
from .models import (
    RawProduct, VetoSpec, ReRankSpec, SpecMatchResult, MatchedProduct,
)
from .tools import TOOL_DECLARATIONS, execute_tool

logger = logging.getLogger(__name__)

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


# ============================================================================
# SYSTEM PROMPT
# ============================================================================

SYSTEM_PROMPT = """You are a product sourcing specialist evaluating whether a supplier product matches a buyer's specifications.

You have tools to help you analyze:
- calculate(expression): Evaluate math expressions for price/dimension comparisons
- convert_unit(value, from_unit, to_unit): Convert between measurement units
- analyze_image(image_url, question): Analyze product images for visual properties (color, material appearance, shape, finish, design)
- extract_from_title(title, spec_name): Extract specs from product titles

CRITICAL: USE YOUR TOOLS BEFORE RESPONDING.
Do NOT just read the title and guess. Follow this process:
1. First, call extract_from_title() for each spec to check the title
2. For ANY visual spec (color, finish, material appearance, shape) → ALWAYS call analyze_image()
3. For numeric specs → call convert_unit() and calculate() when units differ
4. Only AFTER using tools, output your final JSON

IMPORTANT: If a spec has source="image" or spec_type="Visual", you MUST call analyze_image() before marking it.
If analyze_image is not called for a visual spec, the result is invalid.

MATCHING RULES:
- For each spec, determine: MATCH, NOT_MATCH, or UNKNOWN
- VETO specs are MANDATORY: NOT_MATCH on ANY veto spec = product eliminated
- RE-RANK specs are PREFERENCES: affect ranking score, not elimination
- Prefer "UNKNOWN" over guessing. Only say NOT_MATCH if clearly contradicted.
- Titles are often in Chinese. Translate and interpret them before matching.

MATCHING STRATEGIES BY SPEC TYPE:
- product_category: Match against product title. Be generous - if the product is clearly in the right category, it's a MATCH. Translate Chinese titles.
- Numeric specs (capacity, weight, dimensions): Use calculate() and convert_unit() for precise comparison. "within_10_percent" means ±10% of target.
- Visual specs (color, finish): ALWAYS call analyze_image() on the product image. Also check title for keywords.
- Functional specs (BPA-free, dishwasher safe, leak-proof): Check title first. If not found in title and image is available, try analyze_image().
- Material specs: Check title first. If ambiguous, try analyze_image() to confirm material appearance.

CONFIDENCE SCORING (1-5):
5 = Certain match/non-match (explicit in title AND confirmed by image)
4 = High confidence (strong keyword match or clear visual from image)
3 = Moderate (indirect evidence or partial match)
2 = Low confidence (weak evidence, title only without image verification)
1 = Very uncertain (guessing)

You MUST output valid JSON with this exact format:
{
  "specs": [
    {
      "spec_id": <int>,
      "spec_name": "<string>",
      "match_type": "MATCH" | "NOT_MATCH" | "UNKNOWN",
      "product_value": "<what you found or null>",
      "confidence": <1-5>,
      "reasoning": "<brief explanation>",
      "tools_used": ["<tool_name>", ...]
    }
  ]
}"""


# ============================================================================
# MESSAGE BUILDER
# ============================================================================

def _build_user_message(
    product: RawProduct,
    veto_specs: list[VetoSpec],
    rerank_specs: list[ReRankSpec],
) -> str:
    """Build the user message for a single product evaluation."""
    parts = []

    # Product context
    parts.append("PRODUCT TO EVALUATE:")
    parts.append(f"- Title: {product.title}")
    parts.append(f"- Price: {product.offer_price} ({'CNY' if product.platform == '1688' else 'USD'})")
    parts.append(f"- Price (USD): ${product.price_usd}")
    parts.append(f"- MOQ: {product.min_order_quantity} pcs")
    parts.append(f"- Image URL: {product.img}")
    parts.append(f"- Platform: {product.platform}")
    parts.append(f"- Supplier: {product.shop_info.shop_name}")
    parts.append(f"- Factory: {'Yes' if product.shop_info.is_factory else 'No'}")
    parts.append("")

    # Veto specs
    if veto_specs:
        parts.append("VETO SPECS (mandatory - product ELIMINATED if ANY fails):")
        for spec in veto_specs:
            parts.append(f"  [{spec.spec_id}] {spec.spec_name}")
            parts.append(f"      Mandatory values: {spec.mandatory_values}")
            if spec.unacceptable_values:
                parts.append(f"      Unacceptable values: {spec.unacceptable_values}")
            parts.append(f"      Matching rule: {spec.matching_rule}")
            parts.append(f"      Spec type: {spec.spec_type}")
            parts.append("")

    # Rerank specs
    if rerank_specs:
        parts.append("RE-RANK SPECS (preferences - affects ranking, not elimination):")
        for spec in rerank_specs:
            parts.append(f"  [{spec.spec_id}] {spec.spec_name}")
            parts.append(f"      Acceptable values: {spec.acceptable_values}")
            parts.append(f"      Matching rule: {spec.matching_rule}")
            parts.append(f"      Spec type: {spec.spec_type}")
            parts.append("")

    parts.append("Evaluate ALL specs above. Use tools when needed for precise matching.")
    parts.append("Return your evaluation as JSON.")

    return "\n".join(parts)


# ============================================================================
# SINGLE PRODUCT MATCHING
# ============================================================================

async def _match_single_product(
    product: RawProduct,
    veto_specs: list[VetoSpec],
    rerank_specs: list[ReRankSpec],
    semaphore: asyncio.Semaphore,
) -> MatchedProduct:
    """Match a single product against all specs using agentic tool-calling loop."""

    async with semaphore:
        client = _get_client()
        user_msg = _build_user_message(product, veto_specs, rerank_specs)

        contents: list[genai_types.Content] = [
            genai_types.Content(
                role="user",
                parts=[genai_types.Part(text=user_msg)],
            )
        ]

        final_text = ""
        tools_used_total: list[str] = []

        try:
            for turn in range(MAX_TOOL_TURNS):
                response = await client.aio.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=contents,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        tools=[TOOL_DECLARATIONS],
                        temperature=0.0,
                    ),
                )

                # Check if response has candidates
                if not response.candidates or not response.candidates[0].content.parts:
                    logger.warning(f"Empty response for product {product.item_id} on turn {turn}")
                    break

                # Collect function calls from response
                function_calls = []
                text_parts = []
                for part in response.candidates[0].content.parts:
                    if part.function_call:
                        function_calls.append(part.function_call)
                    elif part.text:
                        text_parts.append(part.text)

                if not function_calls:
                    # No more tool calls - this is the final response
                    final_text = "\n".join(text_parts)
                    break

                # Add assistant's response to conversation
                contents.append(response.candidates[0].content)

                # Execute all tools in parallel
                tool_results = []
                for fc in function_calls:
                    fc_name = fc.name
                    fc_args = dict(fc.args) if fc.args else {}
                    tools_used_total.append(fc_name)
                    logger.debug(f"  Tool call: {fc_name}({fc_args})")

                    result = await execute_tool(fc_name, fc_args)
                    tool_results.append(
                        genai_types.Part.from_function_response(
                            name=fc_name,
                            response={"result": str(result)},
                        )
                    )

                # Add tool responses to conversation
                contents.append(
                    genai_types.Content(role="user", parts=tool_results)
                )

            # Parse the final JSON response
            return _parse_response(product, veto_specs, rerank_specs, final_text, tools_used_total)

        except Exception as e:
            logger.error(f"Matching failed for product {product.item_id}: {e}")
            # Return product with all specs UNKNOWN
            return _fallback_result(product, veto_specs, rerank_specs, str(e))


# ============================================================================
# RESPONSE PARSING
# ============================================================================

def _parse_response(
    product: RawProduct,
    veto_specs: list[VetoSpec],
    rerank_specs: list[ReRankSpec],
    response_text: str,
    tools_used: list[str],
) -> MatchedProduct:
    """Parse the Gemini JSON response into a MatchedProduct."""

    all_spec_ids = {s.spec_id for s in veto_specs} | {s.spec_id for s in rerank_specs}
    spec_results: list[SpecMatchResult] = []

    try:
        # Clean up response text (remove markdown code fences if present)
        text = response_text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        if text.startswith("json"):
            text = text[4:].strip()

        # Fix common JSON issues from LLM output
        import re as _re
        # Fix invalid unicode escapes
        text = _re.sub(r'\\u(?![0-9a-fA-F]{4})', r'\\\\u', text)

        data = json.loads(text)
        specs_data = data.get("specs", [])

        parsed_ids = set()
        for s in specs_data:
            spec_id = s.get("spec_id", 0)
            parsed_ids.add(spec_id)

            # Safely get product_value as string
            pv = s.get("product_value")
            if pv is not None:
                pv = str(pv)

            spec_results.append(SpecMatchResult(
                spec_id=spec_id,
                spec_name=s.get("spec_name", ""),
                match_type=s.get("match_type", "UNKNOWN").upper(),
                product_value=pv,
                confidence=min(5, max(1, int(s.get("confidence", 1)))),
                reasoning=str(s.get("reasoning", "")),
                tools_used=s.get("tools_used", []),
            ))

        # Add UNKNOWN for any specs not in the response
        for spec_id in all_spec_ids - parsed_ids:
            spec = next(
                (s for s in list(veto_specs) + list(rerank_specs) if s.spec_id == spec_id),
                None,
            )
            if spec:
                spec_results.append(SpecMatchResult(
                    spec_id=spec_id,
                    spec_name=spec.spec_name,
                    match_type="UNKNOWN",
                    confidence=1,
                    reasoning="Not evaluated by agent",
                ))

    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning(f"Failed to parse response for {product.item_id}: {e}")
        logger.debug(f"Raw response: {response_text[:500]}")
        return _fallback_result(product, veto_specs, rerank_specs, f"Parse error: {e}")

    return MatchedProduct(
        product=product,
        spec_results=spec_results,
    )


def _fallback_result(
    product: RawProduct,
    veto_specs: list[VetoSpec],
    rerank_specs: list[ReRankSpec],
    error_msg: str,
) -> MatchedProduct:
    """Create a fallback result when matching fails."""
    spec_results = []
    for spec in list(veto_specs) + list(rerank_specs):
        spec_results.append(SpecMatchResult(
            spec_id=spec.spec_id,
            spec_name=spec.spec_name,
            match_type="UNKNOWN",
            confidence=1,
            reasoning=f"Matching error: {error_msg}",
        ))
    return MatchedProduct(product=product, spec_results=spec_results)


# ============================================================================
# BATCH MATCHING
# ============================================================================

async def match_products(
    products: list[RawProduct],
    veto_specs: list[VetoSpec],
    rerank_specs: list[ReRankSpec],
    on_progress: Optional[Callable] = None,
) -> list[MatchedProduct]:
    """Match all products against specs using concurrent agentic matching."""

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    total = len(products)
    completed = 0

    async def _match_with_progress(product: RawProduct) -> MatchedProduct:
        nonlocal completed
        result = await _match_single_product(product, veto_specs, rerank_specs, semaphore)
        completed += 1
        if on_progress:
            await on_progress(completed, total)
        logger.info(f"Matched {completed}/{total}: {product.item_id} -> {result.verdict}")
        return result

    tasks = [_match_with_progress(p) for p in products]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    matched = []
    for r in results:
        if isinstance(r, MatchedProduct):
            matched.append(r)
        elif isinstance(r, Exception):
            logger.error(f"Match task exception: {r}")

    logger.info(f"Matching complete: {len(matched)}/{total} products processed")
    return matched
