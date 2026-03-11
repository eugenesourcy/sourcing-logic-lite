"""Agent Tools - calculate, convert_unit, analyze_image, extract_from_title."""

import re
import logging
from google import genai
from google.genai import types as genai_types
from .config import GEMINI_API_KEY, GEMINI_MODEL

logger = logging.getLogger(__name__)

_vision_client: genai.Client | None = None


def _get_vision_client() -> genai.Client:
    global _vision_client
    if _vision_client is None:
        _vision_client = genai.Client(api_key=GEMINI_API_KEY)
    return _vision_client


# ============================================================================
# TOOL: calculate
# ============================================================================

def tool_calculate(expression: str) -> str:
    """Safely evaluate a mathematical expression."""
    try:
        from simpleeval import simple_eval
        result = simple_eval(expression)
        return str(round(float(result), 4))
    except Exception as e:
        return f"Error: {e}"


# ============================================================================
# TOOL: convert_unit
# ============================================================================

# Conversion tables (to base SI unit)
_CONVERSIONS: dict[str, dict[str, float]] = {
    "length": {
        "mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0,
        "in": 0.0254, "inch": 0.0254, "inches": 0.0254,
        "ft": 0.3048, "foot": 0.3048, "feet": 0.3048,
        "yd": 0.9144, "yard": 0.9144,
    },
    "volume": {
        "ml": 0.001, "cl": 0.01, "l": 1.0, "liter": 1.0, "litre": 1.0,
        "oz": 0.0295735, "fl oz": 0.0295735, "floz": 0.0295735,
        "cup": 0.236588, "pt": 0.473176, "pint": 0.473176,
        "qt": 0.946353, "quart": 0.946353,
        "gal": 3.78541, "gallon": 3.78541,
    },
    "weight": {
        "mg": 0.000001, "g": 0.001, "kg": 1.0,
        "oz": 0.0283495, "lb": 0.453592, "lbs": 0.453592,
        "pound": 0.453592, "pounds": 0.453592,
        "ton": 1000.0, "tonne": 1000.0,
    },
    "temperature": {},  # Special handling
}


def tool_convert_unit(value: float, from_unit: str, to_unit: str) -> str:
    """Convert between units."""
    from_unit = from_unit.lower().strip()
    to_unit = to_unit.lower().strip()

    # Temperature special case
    if from_unit in ("c", "celsius", "°c") and to_unit in ("f", "fahrenheit", "°f"):
        return str(round(value * 9 / 5 + 32, 2))
    if from_unit in ("f", "fahrenheit", "°f") and to_unit in ("c", "celsius", "°c"):
        return str(round((value - 32) * 5 / 9, 2))

    for category, units in _CONVERSIONS.items():
        if from_unit in units and to_unit in units:
            base_value = value * units[from_unit]
            result = base_value / units[to_unit]
            return str(round(result, 4))

    return f"Cannot convert from '{from_unit}' to '{to_unit}'"


# ============================================================================
# TOOL: analyze_image
# ============================================================================

async def tool_analyze_image(image_url: str, question: str) -> str:
    """Use Gemini Vision to analyze a product image by downloading it first."""
    import httpx as _httpx

    client = _get_vision_client()
    prompt = f"""Analyze this product image and answer the following question concisely.
Question: {question}
Provide a direct, factual answer. If you cannot determine the answer from the image, say "Cannot determine from image"."""

    # Strategy 1: Download image bytes and send inline
    try:
        async with _httpx.AsyncClient(timeout=10.0, follow_redirects=True) as http:
            img_resp = await http.get(image_url)
            img_resp.raise_for_status()
            img_bytes = img_resp.content

            # Detect MIME type from content-type header or URL
            content_type = img_resp.headers.get("content-type", "image/jpeg")
            if "png" in content_type or image_url.lower().endswith(".png"):
                mime = "image/png"
            elif "webp" in content_type or image_url.lower().endswith(".webp"):
                mime = "image/webp"
            else:
                mime = "image/jpeg"

        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                genai_types.Content(
                    role="user",
                    parts=[
                        genai_types.Part.from_bytes(data=img_bytes, mime_type=mime),
                        genai_types.Part(text=prompt),
                    ],
                )
            ],
            config=genai_types.GenerateContentConfig(temperature=0.0),
        )
        return response.text.strip()

    except Exception as e:
        logger.warning(f"Image download/analysis failed for {image_url}: {e}")

    # Strategy 2: Try with URL passed as URI (works for some public URLs)
    try:
        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                genai_types.Content(
                    role="user",
                    parts=[
                        genai_types.Part.from_uri(file_uri=image_url, mime_type="image/jpeg"),
                        genai_types.Part(text=prompt),
                    ],
                )
            ],
            config=genai_types.GenerateContentConfig(temperature=0.0),
        )
        return response.text.strip()
    except Exception as e2:
        logger.warning(f"Image URI analysis also failed: {e2}")
        return f"Image analysis unavailable: could not access image"


# ============================================================================
# TOOL: extract_from_title
# ============================================================================

def tool_extract_from_title(title: str, spec_name: str) -> str:
    """Extract spec value from product title using regex and keyword matching."""
    title_lower = title.lower()
    spec_lower = spec_name.lower()

    # Common patterns
    patterns: dict[str, list[str]] = {
        "capacity": [
            r"(\d+(?:\.\d+)?)\s*(?:ml|l|oz|fl\.?\s*oz|liter|litre)",
            r"(\d+(?:\.\d+)?)\s*(?:cc|gallon|gal|cup|pint|quart)",
        ],
        "weight": [
            r"(\d+(?:\.\d+)?)\s*(?:g|kg|lb|lbs|oz|gram|kilogram|pound)",
        ],
        "dimension": [
            r"(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*(?:[xX×]\s*(\d+(?:\.\d+)?))?\s*(?:cm|mm|m|inch|in)",
        ],
        "color": [
            r"\b(red|blue|green|yellow|black|white|pink|purple|orange|brown|beige|gray|grey|silver|gold|navy|teal|matte|glossy)\b",
        ],
        "material": [
            r"\b(stainless\s*steel|plastic|glass|aluminum|aluminium|bamboo|wood|wooden|silicone|ceramic|pp|pe|pet|rpet|tritan|bpa[\s-]?free|recycled|eco[\s-]?friendly)\b",
        ],
        "feature": [
            r"\b(leak[\s-]?proof|dishwasher[\s-]?safe|bpa[\s-]?free|insulated|vacuum|double[\s-]?wall|reusable|portable|foldable|collapsible)\b",
        ],
    }

    # Try spec-specific patterns
    for category, pats in patterns.items():
        if category in spec_lower or spec_lower in category:
            for pat in pats:
                matches = re.findall(pat, title_lower)
                if matches:
                    if isinstance(matches[0], tuple):
                        return " x ".join(m for m in matches[0] if m)
                    return str(matches[0])

    # Generic: search for spec name keywords in title
    spec_words = spec_lower.replace("_", " ").split()
    for word in spec_words:
        if len(word) > 2 and word in title_lower:
            # Find surrounding context
            idx = title_lower.index(word)
            start = max(0, idx - 20)
            end = min(len(title), idx + len(word) + 30)
            return title[start:end].strip()

    # Try all feature patterns as fallback
    for pat in patterns.get("feature", []):
        matches = re.findall(pat, title_lower)
        if matches:
            return ", ".join(matches)

    return "Not found in title"


# ============================================================================
# TOOL DECLARATIONS (for Gemini function calling)
# ============================================================================

TOOL_DECLARATIONS = genai_types.Tool(
    function_declarations=[
        genai_types.FunctionDeclaration(
            name="calculate",
            description="Evaluate a mathematical expression. Use for price conversions, percentage calculations, numeric comparisons. Example: '6.5 * 7.2' for USD to CNY.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "expression": {
                        "type": "STRING",
                        "description": "Mathematical expression to evaluate (e.g., '500 * 0.9' or '6.5 * 7.2')",
                    },
                },
                "required": ["expression"],
            },
        ),
        genai_types.FunctionDeclaration(
            name="convert_unit",
            description="Convert a value between units. Supports length (mm/cm/m/in/ft), volume (ml/l/oz/cup/gal), weight (g/kg/lb/oz), temperature (C/F).",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "value": {"type": "NUMBER", "description": "The numeric value to convert"},
                    "from_unit": {"type": "STRING", "description": "Source unit (e.g., 'ml', 'oz', 'cm', 'kg')"},
                    "to_unit": {"type": "STRING", "description": "Target unit (e.g., 'oz', 'ml', 'inch', 'lb')"},
                },
                "required": ["value", "from_unit", "to_unit"],
            },
        ),
        genai_types.FunctionDeclaration(
            name="analyze_image",
            description="Analyze a product image to check visual properties like color, finish, material appearance, shape, design features. Use when specs require visual verification.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "image_url": {"type": "STRING", "description": "URL of the product image to analyze"},
                    "question": {"type": "STRING", "description": "Specific question about the image (e.g., 'What color is this product?' or 'Does this bottle have a matte finish?')"},
                },
                "required": ["image_url", "question"],
            },
        ),
        genai_types.FunctionDeclaration(
            name="extract_from_title",
            description="Extract a specific specification value from the product title using pattern matching. Good for finding capacity, dimensions, materials, colors, and features mentioned in the title.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING", "description": "The product title to search"},
                    "spec_name": {"type": "STRING", "description": "The specification to look for (e.g., 'capacity', 'material', 'color')"},
                },
                "required": ["title", "spec_name"],
            },
        ),
    ]
)


async def execute_tool(name: str, args: dict) -> str:
    """Execute a tool by name and return the result as a string."""
    try:
        if name == "calculate":
            return tool_calculate(args.get("expression", ""))
        elif name == "convert_unit":
            return tool_convert_unit(
                float(args.get("value", 0)),
                str(args.get("from_unit", "")),
                str(args.get("to_unit", "")),
            )
        elif name == "analyze_image":
            return await tool_analyze_image(
                str(args.get("image_url", "")),
                str(args.get("question", "")),
            )
        elif name == "extract_from_title":
            return tool_extract_from_title(
                str(args.get("title", "")),
                str(args.get("spec_name", "")),
            )
        else:
            return f"Unknown tool: {name}"
    except Exception as e:
        return f"Tool error: {e}"
