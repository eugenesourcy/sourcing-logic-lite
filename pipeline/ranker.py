"""Step 5: Ranking - Veto filter, relaxation, supplier scoring, final sort."""

import logging
from .models import (
    MatchedProduct, RankedProduct, SpecMatchResult,
    VetoSpec, ReRankSpec,
)

logger = logging.getLogger(__name__)


def _compute_veto_verdict(
    spec_results: list[SpecMatchResult],
    veto_spec_ids: set[int],
) -> str:
    """Determine verdict based on veto spec results.

    Logic:
    - eliminated: ANY veto spec explicitly NOT_MATCH
    - shortlisted: No NOT_MATCH on any veto spec (MATCH + UNKNOWN both OK)
    - pending: shouldn't happen with this logic, but kept as fallback

    UNKNOWN means "info not available" (e.g. Chinese titles rarely mention
    BPA-free, dishwasher safe). This is NOT a failure — only explicit
    NOT_MATCH should eliminate products.
    """
    veto_results = [s for s in spec_results if s.spec_id in veto_spec_ids]

    if not veto_results:
        return "shortlisted"  # No veto specs = auto-pass

    has_not_match = any(s.match_type == "NOT_MATCH" for s in veto_results)

    if has_not_match:
        return "eliminated"

    # Count how many veto specs are truly matched vs unknown
    match_count = sum(1 for s in veto_results if s.match_type == "MATCH")
    unknown_count = sum(1 for s in veto_results if s.match_type == "UNKNOWN")

    # If at least one veto spec matched and no NOT_MATCH, shortlist it
    # UNKNOWN specs will reduce the veto_score but shouldn't block shortlisting
    if match_count > 0:
        return "shortlisted"

    # All veto specs are UNKNOWN — still shortlist but with low score
    return "shortlisted"


def _compute_veto_score(
    spec_results: list[SpecMatchResult],
    veto_specs: list[VetoSpec],
) -> float:
    """Weighted veto score (0.0-1.0). MATCH=1.0, UNKNOWN=0.5, NOT_MATCH=0.0."""
    veto_map = {s.spec_id: s.veto_score for s in veto_specs}
    total_weight = 0
    weighted_sum = 0.0

    for result in spec_results:
        weight = veto_map.get(result.spec_id, 0)
        if weight > 0:
            total_weight += weight
            if result.match_type == "MATCH":
                weighted_sum += weight * 1.0
            elif result.match_type == "UNKNOWN":
                weighted_sum += weight * 0.5

    return round(weighted_sum / total_weight, 3) if total_weight > 0 else 0.0


def _compute_rerank_score(
    spec_results: list[SpecMatchResult],
    rerank_specs: list[ReRankSpec],
) -> float:
    """Weighted rerank score (0.0-1.0). Only MATCH counts."""
    rerank_map = {s.spec_id: s.re_rank_score for s in rerank_specs}
    total_weight = sum(rerank_map.values())
    matched_weight = 0

    for result in spec_results:
        weight = rerank_map.get(result.spec_id, 0)
        if weight > 0 and result.match_type == "MATCH":
            matched_weight += weight

    return round(matched_weight / total_weight, 3) if total_weight > 0 else 0.0


def _compute_supplier_score(product: MatchedProduct) -> float:
    """Supplier score (0.0-1.0) based on type, rating, experience."""
    shop = product.product.shop_info

    # Type score (0-3): factory=2, +1 if 5+ years
    type_score = 2 if shop.is_factory else 0
    if shop.tp_year >= 5:
        type_score = min(type_score + 1, 3)

    # Service score (0-2): based on rating
    if shop.comprehensive_rating >= 4.5:
        service_score = 2
    elif shop.comprehensive_rating >= 4.0:
        service_score = 1
    else:
        service_score = 0

    # Combined: 50% type + 30% service + 20% category (always 0 for now)
    return round(0.5 * (type_score / 3) + 0.3 * (service_score / 2), 3)


def rank_products(
    matched_products: list[MatchedProduct],
    veto_specs: list[VetoSpec],
    rerank_specs: list[ReRankSpec],
    num_options: int = 10,
    min_shortlisted: int = 5,
) -> list[RankedProduct]:
    """Apply veto filtering, relaxation, scoring, and ranking."""

    veto_spec_ids = {s.spec_id for s in veto_specs}
    veto_map = {s.spec_id: s for s in veto_specs}

    # Step 1: Compute verdicts and scores
    for mp in matched_products:
        mp.verdict = _compute_veto_verdict(mp.spec_results, veto_spec_ids)
        mp.veto_score = _compute_veto_score(mp.spec_results, veto_specs)
        mp.rerank_score = _compute_rerank_score(mp.spec_results, rerank_specs)

    shortlisted = [p for p in matched_products if p.verdict == "shortlisted"]
    pending = [p for p in matched_products if p.verdict == "pending"]
    eliminated = [p for p in matched_products if p.verdict == "eliminated"]

    logger.info(
        f"Initial verdicts: {len(shortlisted)} shortlisted, "
        f"{len(pending)} pending, {len(eliminated)} eliminated"
    )

    # Step 2: Relaxation (if not enough shortlisted)
    if len(shortlisted) < min_shortlisted and pending:
        logger.info(f"Relaxation: only {len(shortlisted)} shortlisted, need {min_shortlisted}")

        # Sort pending by veto_score descending (best first)
        pending.sort(key=lambda p: p.veto_score, reverse=True)

        for mp in pending:
            if len(shortlisted) >= min_shortlisted:
                break

            # Check which specs are UNKNOWN (and thus relaxable)
            unknown_veto = [
                s for s in mp.spec_results
                if s.spec_id in veto_spec_ids
                and s.match_type == "UNKNOWN"
            ]

            # Relax if unknown specs are reasonable (max half of veto specs)
            max_relaxable = max(2, len(veto_spec_ids) // 2)
            relaxable = unknown_veto  # All UNKNOWN veto specs are candidates

            if len(relaxable) <= max_relaxable:
                mp.verdict = "shortlisted"
                mp.relaxed_specs = [s.spec_name for s in relaxable]
                shortlisted.append(mp)
                logger.info(
                    f"  Relaxed product {mp.product.item_id}: "
                    f"relaxed specs = {mp.relaxed_specs}"
                )

    # Step 3: Build ranked output from shortlisted products
    ranked: list[RankedProduct] = []

    for mp in shortlisted:
        supplier_score = _compute_supplier_score(mp)
        combined = round(mp.rerank_score + supplier_score, 3)
        p = mp.product

        # Extract product specs dict from match results
        product_specs: dict[str, str | None] = {}
        for sr in mp.spec_results:
            pv = sr.product_value
            if pv is not None:
                product_specs[sr.spec_name] = str(pv)
            elif sr.match_type == "MATCH":
                product_specs[sr.spec_name] = "confirmed"
            else:
                product_specs[sr.spec_name] = None
        # Always include base product fields
        product_specs["price_usd"] = str(p.price_usd)
        product_specs["moq"] = str(p.min_order_quantity) if p.min_order_quantity else None
        product_specs["platform"] = p.platform
        product_specs["supplier_type"] = "factory" if p.shop_info.is_factory else "trader"

        ranked.append(RankedProduct(
            product_id=p.item_id,
            title=p.title,
            image_url=p.img,
            product_url=p.url,
            platform=p.platform,
            price=p.price_usd,
            price_original=p.offer_price,
            moq=p.min_order_quantity,
            currency="CNY" if p.platform == "1688" else "USD",
            supplier_name=p.shop_info.shop_name,
            supplier_location=p.shop_info.location,
            supplier_is_factory=p.shop_info.is_factory,
            supplier_rating=p.shop_info.comprehensive_rating,
            supplier_years=p.shop_info.tp_year,
            verdict=mp.verdict,
            veto_score=mp.veto_score,
            rerank_score=mp.rerank_score,
            supplier_score=supplier_score,
            combined_score=combined,
            spec_results=mp.spec_results,
            relaxed_specs=mp.relaxed_specs,
            product_specs=product_specs,
        ))

    # Sort by combined score
    ranked.sort(key=lambda p: p.combined_score, reverse=True)

    # Assign ranks
    for i, p in enumerate(ranked):
        p.rank = i + 1

    # Trim to num_options
    result = ranked[:num_options]

    logger.info(f"Final ranking: {len(result)} products (from {len(shortlisted)} shortlisted)")
    return result
