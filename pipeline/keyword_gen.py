"""Step 1: Keyword Generation using Gemini."""

import json
import logging
from google import genai
from google.genai import types as genai_types
from .config import GEMINI_API_KEY, GEMINI_MODEL
from .models import Keywords

logger = logging.getLogger(__name__)

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


KEYWORD_PROMPT = """You are a B2B product sourcing expert specializing in Chinese manufacturing platforms.

Given a product requirement, generate optimized search keywords for 1688.com (Chinese) and Alibaba.com (English).

Product Title: {title}
Product Description: {description}

Return ONLY valid JSON (no markdown, no code fences):
{{
  "en_keywords": ["keyword1", "keyword2"],
  "cn_keywords": ["关键词1", "关键词2", "关键词3"]
}}

Rules:
- en_keywords: 2 English keywords optimized for Alibaba.com search
  - First keyword: broad product category (e.g., "stainless steel water bottle")
  - Second keyword: more specific variant (e.g., "BPA free recycled water bottle 500ml")
- cn_keywords: 3 Chinese keywords optimized for 1688.com search
  - Use actual Chinese product names that suppliers list on 1688
  - Include material/type specifics in Chinese
- Keywords should be product-focused, not feature lists
- Prioritize terms that match real supplier listings"""


async def generate_keywords(title: str, description: str) -> Keywords:
    """Generate EN + CN search keywords from product requirements."""
    client = _get_client()

    prompt = KEYWORD_PROMPT.format(title=title, description=description)

    try:
        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )

        text = response.text.strip()
        logger.info(f"Keyword gen raw response: {text[:200]}")

        data = json.loads(text)
        keywords = Keywords(
            en_keywords=data.get("en_keywords", []),
            cn_keywords=data.get("cn_keywords", []),
        )

        logger.info(f"Generated keywords: EN={keywords.en_keywords}, CN={keywords.cn_keywords}")
        return keywords

    except Exception as e:
        logger.error(f"Keyword generation failed: {e}")
        # Fallback: use title directly
        return Keywords(
            en_keywords=[title],
            cn_keywords=[title],
        )
