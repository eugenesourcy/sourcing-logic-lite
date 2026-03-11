"""Pydantic models for the lite pipeline input/output."""

from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional


# ============================================================================
# INPUT MODELS
# ============================================================================

class VetoSpec(BaseModel):
    spec_id: int
    spec_name: str
    mandatory_values: list[str] = []
    unacceptable_values: list[str] = []
    spec_type: str = "Functional"
    source: str = "text"
    text_confirmation: bool = True
    classification: str = "VETO"
    matching_rule: str = "exact_match"
    reasoning: str = ""
    veto_score: int = 5
    priority_signal: str = ""


class ReRankSpec(BaseModel):
    spec_id: int
    spec_name: str
    acceptable_values: list[str] = []
    spec_type: str = "Functional"
    source: str = "text"
    text_confirmation: bool = True
    classification: str = "RE-RANK"
    matching_rule: str = "keyword_match"
    reasoning: str = ""
    re_rank_score: int = 3
    priority_signal: str = ""


class OriginalRequirement(BaseModel):
    title: str
    description: str = ""
    reference_images: list[str] = []
    target_price: float = 0
    target_price_currency: str = "USD"
    target_quantity: int = 0
    target_quantity_unit: str = "pcs"
    needs_customization: bool = False
    num_options: int = 10
    customization_reference_url: list[str] = []
    customization_type: Optional[str] = None
    customization_description: Optional[str] = None


class SpecsInput(BaseModel):
    veto_specs: list[VetoSpec] = []
    re_rank_specs: list[ReRankSpec] = []


class SourcingRequest(BaseModel):
    """Input schema - compatible with sourcing-logic test data format."""
    runId: str = "lite-run"
    original_requirement: OriginalRequirement
    specs: SpecsInput = SpecsInput()
    reference_images: list[str] = []
    sourcing_type: str = "specific"


# ============================================================================
# INTERNAL MODELS
# ============================================================================

class Keywords(BaseModel):
    en_keywords: list[str] = []
    cn_keywords: list[str] = []


class ShopInfo(BaseModel):
    shop_name: str = ""
    member_id: str = ""
    seller_login_id: str = ""
    tp_year: int = 0
    is_factory: bool = False
    comprehensive_rating: float = 0.0
    location: str = ""


class RawProduct(BaseModel):
    item_id: str
    title: str = ""
    img: str = ""
    offer_price: str = ""
    price_usd: float = 0.0
    min_order_quantity: int = 0
    shop_info: ShopInfo = ShopInfo()
    platform: str = ""
    url: str = ""
    keyword_used: str = ""


class SpecMatchResult(BaseModel):
    spec_id: int
    spec_name: str
    match_type: str = "UNKNOWN"  # MATCH / NOT_MATCH / UNKNOWN
    product_value: Optional[str | int | float] = None
    confidence: int = 1  # 1-5
    reasoning: str = ""
    tools_used: list[str] = []


class MatchedProduct(BaseModel):
    product: RawProduct
    spec_results: list[SpecMatchResult] = []
    verdict: str = "pending"  # shortlisted / eliminated / pending
    veto_score: float = 0.0
    rerank_score: float = 0.0
    relaxed_specs: list[str] = []


# ============================================================================
# OUTPUT MODELS
# ============================================================================

class RankedProduct(BaseModel):
    rank: int = 0
    product_id: str
    title: str
    image_url: str = ""
    product_url: str = ""
    platform: str = ""
    price: float = 0.0
    price_original: str = ""
    moq: int = 0
    currency: str = "USD"

    # Supplier
    supplier_name: str = ""
    supplier_location: str = ""
    supplier_is_factory: bool = False
    supplier_rating: float = 0.0
    supplier_years: int = 0

    # Scores
    verdict: str = "pending"
    veto_score: float = 0.0
    rerank_score: float = 0.0
    supplier_score: float = 0.0
    combined_score: float = 0.0

    # Details
    spec_results: list[SpecMatchResult] = []
    relaxed_specs: list[str] = []

    # Extracted product specs (for agent consumption)
    # e.g. {"material": "stainless steel", "capacity": "500ml", "color": "blue"}
    product_specs: dict[str, str | None] = {}


class StepTiming(BaseModel):
    keyword_gen: float = 0.0
    spec_gen: float = 0.0
    search: float = 0.0
    prefilter: float = 0.0
    agentic_matching: float = 0.0
    ranking: float = 0.0


class SourcingResult(BaseModel):
    run_id: str
    request_title: str
    total_searched: int = 0
    total_after_prefilter: int = 0
    total_matched: int = 0
    total_shortlisted: int = 0
    execution_time_seconds: float = 0.0
    step_timings: StepTiming = StepTiming()
    products: list[RankedProduct] = []
    keywords_generated: Keywords = Keywords()
