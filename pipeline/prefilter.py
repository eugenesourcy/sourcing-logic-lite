"""Step 3: Pre-filter - Dedup + Price/MOQ filtering."""

import logging
from .models import RawProduct

logger = logging.getLogger(__name__)


def prefilter_products(
    products: list[RawProduct],
    target_price_usd: float = 0,
    target_quantity: int = 0,
    price_tolerance: float = 1.5,
    moq_tolerance: float = 2.0,
) -> list[RawProduct]:
    """
    Fast local filtering:
    1. Dedup by item_id
    2. Dedup by image URL
    3. Price filter (if target_price > 0)
    4. MOQ filter (if target_quantity > 0)
    """
    initial_count = len(products)

    # Step 1: Dedup by item_id
    seen_ids: set[str] = set()
    deduped = []
    for p in products:
        key = f"{p.platform}:{p.item_id}"
        if key not in seen_ids:
            seen_ids.add(key)
            deduped.append(p)

    after_id_dedup = len(deduped)

    # Step 2: Dedup by image URL
    seen_imgs: set[str] = set()
    img_deduped = []
    for p in deduped:
        if not p.img or p.img not in seen_imgs:
            if p.img:
                seen_imgs.add(p.img)
            img_deduped.append(p)

    after_img_dedup = len(img_deduped)

    # Step 3: Price filter
    if target_price_usd > 0:
        max_price = target_price_usd * price_tolerance
        price_filtered = [
            p for p in img_deduped
            if p.price_usd <= 0 or p.price_usd <= max_price  # Keep if no price data
        ]
    else:
        price_filtered = img_deduped

    after_price = len(price_filtered)

    # Step 4: MOQ filter
    if target_quantity > 0:
        max_moq = int(target_quantity * moq_tolerance)
        moq_filtered = [
            p for p in price_filtered
            if p.min_order_quantity <= 0 or p.min_order_quantity <= max_moq  # Keep if no MOQ data
        ]
    else:
        moq_filtered = price_filtered

    after_moq = len(moq_filtered)

    logger.info(
        f"Pre-filter: {initial_count} -> "
        f"id_dedup={after_id_dedup} -> "
        f"img_dedup={after_img_dedup} -> "
        f"price={after_price} -> "
        f"moq={after_moq}"
    )

    return moq_filtered
