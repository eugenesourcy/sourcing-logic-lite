"""Auto-generate veto/rerank specs from a product title+description (or raw query) using Gemini.

Incorporates classification patterns from the original sourcing-logic
veto-rerank-generation-tool and spec-matching-v2.5 models.
"""

import json
import logging
from google import genai
from google.genai import types as genai_types
from .config import GEMINI_API_KEY, GEMINI_MODEL
from .models import VetoSpec, ReRankSpec, SpecsInput

logger = logging.getLogger(__name__)

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


# ---------------------------------------------------------------------------
# Prompt adapted from the original sourcing-logic veto-rerank-generation-tool
# (hitl/veto-rerank-combined) and spec-matching-v2.5 models.
# ---------------------------------------------------------------------------

SPEC_GEN_PROMPT = """You are a product sourcing specification specialist for a B2B sourcing platform (1688.com & Alibaba.com). Given a buyer's product requirement — which can be a raw query like "Coffee cup" or a detailed description — generate specifications for matching against Chinese supplier product listings.

## YOUR TASK
Classify and generate product specs into TWO categories:

### VETO specs (mandatory — product ELIMINATED if it doesn't match):
These are hard requirements. A single NOT_MATCH on any veto spec eliminates the product.

Priority rules for VETO classification:
- **Product Category** (ALWAYS include): What type of product must this be? Include synonyms.
- **Critical Material**: If a specific material is mentioned or implied (e.g. "stainless steel", "glass", "ceramic")
- **Key Functional Requirements**: Binary yes/no features explicitly stated (e.g. "insulated", "waterproof", "rechargeable")
- **Explicit Exclusions**: If buyer says "not plastic", "no glass", etc. → add to unacceptable_values

### RE-RANK specs (preferences — affect ranking score, don't eliminate):
These are soft preferences that score products higher or lower but never eliminate.

- **Dimensions/Capacity**: Preferred size, volume, weight
- **Visual Preferences**: Color, finish, pattern
- **Price Target**: Target price per unit
- **Quantity/MOQ**: Target order quantity
- **Nice-to-have Features**: Features that are preferred but not mandatory

## SPEC TYPES (from original system):
- "Functional" — binary features (leak-proof, BPA-free, insulated)
- "Numeric" — measurable values (500ml, 10cm, $5.00)
- "Visual" — appearance specs (color, finish, shape) — source should be "image" or "both"
- "Commercial" — price, MOQ, supplier type
- "Basic" — product category, material

## MATCHING RULES:
- "exact_match" — product must contain one of the mandatory values
- "semantic_match" — LLM determines equivalence (e.g. "SS" = "Stainless Steel")
- "contains" — product value contains spec value
- "less_than_or_equal" — for price/quantity comparisons
- "within_10_percent" — numeric tolerance for dimensions

## SOURCE:
- "text" — extractable from product title/description
- "image" — only visible in product images
- "both" — can be confirmed from either text or image

## RULES:
1. ALWAYS start with a product_category veto spec (spec_id=1, veto_score=10)
2. For raw/short queries (e.g. "Coffee cup"): infer reasonable specs from common sense
   - Still generate material, capacity, and usage context veto specs where obvious
   - Be conservative — only make things VETO if they're clearly implied
3. For detailed descriptions: extract ALL explicitly stated requirements
4. Keep spec_name short and snake_case (e.g. "material", "capacity_ml", "color")
5. Be generous with mandatory_values — include synonyms, Chinese equivalents, abbreviations
   - e.g. material: ["stainless steel", "SS", "304", "316", "不锈钢"]
6. veto_score: 10=critical (wrong product type), 7-9=important, 5-6=moderate
7. re_rank_score: 5=very preferred, 3-4=nice to have, 1-2=minor preference
8. Include unacceptable_values for veto specs where it helps (e.g. material="glass" → unacceptable=["plastic", "paper"])
9. Generate 3-6 veto specs and 2-5 rerank specs typically
10. For supplier preferences: add rerank specs for "factory" preference (factories > traders)

## OUTPUT FORMAT (valid JSON):
{
  "veto_specs": [
    {
      "spec_id": 1,
      "spec_name": "product_category",
      "mandatory_values": ["keyword1", "keyword2", "chinese_equivalent"],
      "unacceptable_values": ["wrong_type1"],
      "spec_type": "Basic",
      "source": "text",
      "text_confirmation": true,
      "classification": "VETO",
      "matching_rule": "semantic_match",
      "reasoning": "Must be the correct product category.",
      "veto_score": 10,
      "priority_signal": "product_type"
    }
  ],
  "re_rank_specs": [
    {
      "spec_id": 100,
      "spec_name": "price",
      "acceptable_values": ["under $X"],
      "spec_type": "Commercial",
      "source": "text",
      "text_confirmation": true,
      "classification": "RE-RANK",
      "matching_rule": "less_than_or_equal",
      "reasoning": "Prefer competitive pricing.",
      "re_rank_score": 4,
      "priority_signal": ""
    }
  ]
}"""


async def generate_specs(
    title: str,
    description: str = "",
    target_price: float = 0,
    target_quantity: int = 0,
) -> SpecsInput:
    """Generate veto and rerank specs from product title + description, or raw query.

    If title is a short raw query (e.g. "Coffee cup"), the system will infer
    reasonable specs. If it's a detailed description, it extracts all explicit
    requirements.
    """

    client = _get_client()

    # Build user message — handle both raw queries and structured input
    parts = [f"Product: {title}"]
    if description:
        parts.append(f"Description: {description}")
    if target_price > 0:
        parts.append(f"Target Price: ${target_price} USD per unit")
    if target_quantity > 0:
        parts.append(f"Target Quantity: {target_quantity} pcs")

    if not description and len(title.split()) <= 5:
        # Short raw query — add hint
        parts.append(
            "\nThis is a short raw query. Infer reasonable product specs from common "
            "sense. Still generate proper veto and rerank specs."
        )

    user_msg = "\n".join(parts)

    try:
        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_msg,
            config=genai_types.GenerateContentConfig(
                system_instruction=SPEC_GEN_PROMPT,
                temperature=0.1,
                response_mime_type="application/json",
            ),
        )

        text = response.text.strip()
        data = json.loads(text)

        veto_specs = [VetoSpec(**s) for s in data.get("veto_specs", [])]
        rerank_specs = [ReRankSpec(**s) for s in data.get("re_rank_specs", [])]

        # Always ensure price and quantity rerank specs exist
        spec_ids = {s.spec_id for s in veto_specs + rerank_specs}
        has_price = any(s.spec_name == "price" for s in rerank_specs)
        has_qty = any(s.spec_name == "quantity" for s in rerank_specs)

        next_id = max(spec_ids, default=0) + 1

        if not has_price and target_price > 0:
            rerank_specs.append(ReRankSpec(
                spec_id=next_id,
                spec_name="price",
                acceptable_values=[f"under ${target_price}"],
                spec_type="Commercial",
                matching_rule="less_than_or_equal",
                reasoning=f"Target price is ${target_price} USD per unit.",
                re_rank_score=4,
            ))
            next_id += 1

        if not has_qty and target_quantity > 0:
            rerank_specs.append(ReRankSpec(
                spec_id=next_id,
                spec_name="quantity",
                acceptable_values=[f"MOQ <= {target_quantity}"],
                spec_type="Commercial",
                matching_rule="less_than_or_equal",
                reasoning=f"Need {target_quantity} units, MOQ must be at or below this.",
                re_rank_score=3,
            ))

        logger.info(f"Generated specs: {len(veto_specs)} veto, {len(rerank_specs)} rerank")
        return SpecsInput(veto_specs=veto_specs, re_rank_specs=rerank_specs)

    except Exception as e:
        logger.error(f"Spec generation failed: {e}")
        # Fallback: minimal specs
        return SpecsInput(
            veto_specs=[
                VetoSpec(
                    spec_id=1,
                    spec_name="product_category",
                    mandatory_values=[title.lower()],
                    veto_score=10,
                    matching_rule="semantic_match",
                    reasoning="Must be the correct product type.",
                ),
            ],
            re_rank_specs=[
                ReRankSpec(
                    spec_id=100,
                    spec_name="price",
                    acceptable_values=[f"under ${target_price}"] if target_price > 0 else ["competitive"],
                    spec_type="Commercial",
                    matching_rule="less_than_or_equal",
                    re_rank_score=4,
                ),
            ],
        )
