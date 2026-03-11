"""Step 2: Product Search via TMAPI (1688 + Alibaba)."""

import asyncio
import logging
import re
import traceback
import httpx
from .config import TM_API_KEY, TMAPI_BASE_URL, USD_TO_CNY_RATE
from .models import RawProduct, ShopInfo, Keywords

logger = logging.getLogger(__name__)


def _extract_lowest_price(price_str: str) -> float:
    """Extract lowest price from range like '15.00-22.00' or '2.50'."""
    if not price_str:
        return 0.0
    try:
        numbers = re.findall(r"[\d.]+", str(price_str))
        if numbers:
            return float(numbers[0])
    except (ValueError, IndexError):
        pass
    return 0.0


def _parse_shop_info(data: dict) -> ShopInfo:
    """Parse shop_info from TMAPI response."""
    if not data:
        return ShopInfo()
    return ShopInfo(
        shop_name=str(data.get("shop_name", data.get("shopName", ""))),
        member_id=str(data.get("member_id", data.get("memberId", ""))),
        seller_login_id=str(data.get("seller_login_id", data.get("sellerLoginId", ""))),
        tp_year=int(data.get("tp_year", data.get("tpYear", 0)) or 0),
        is_factory=bool(data.get("is_factory", data.get("isFactory", False))),
        comprehensive_rating=float(data.get("comprehensive_rating", data.get("comprehensiveRating", 0)) or 0),
        location=str(data.get("location", "")),
    )


async def _search_1688(keyword: str, page: int = 1) -> list[RawProduct]:
    """Search 1688 via TMAPI."""
    url = f"{TMAPI_BASE_URL}/1688/search/items"
    params = {
        "keyword": keyword,
        "page": str(page),
        "apiToken": TM_API_KEY,
    }

    try:
        logger.info(f"Searching 1688: '{keyword}' page={page} url={url}")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, params=params)
        logger.info(f"1688 response status: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()

        # TMAPI returns: {code, msg, data: {items: [...]}}
        data_obj = data.get("data", {})
        items = []
        if isinstance(data_obj, dict):
            items = data_obj.get("items", data_obj.get("result", []))
        if not items and isinstance(data_obj, list):
            items = data_obj

        products = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("item_id", item.get("itemId", item.get("offerId", ""))))
            if not item_id:
                continue

            # Price: TMAPI returns "price" or nested "price_info.sale_price"
            price_str = str(
                item.get("price", "")
                or item.get("offer_price", "")
                or (item.get("price_info", {}) or {}).get("sale_price", "0")
            )
            price_val = _extract_lowest_price(price_str)
            price_usd = round(price_val / USD_TO_CNY_RATE, 2) if price_val > 0 else 0.0

            # MOQ: TMAPI returns "moq" or "quantity_begin"
            moq = 0
            moq_raw = item.get("moq") or item.get("quantity_begin") or item.get("min_order_quantity", 0)
            if moq_raw:
                try:
                    moq = int(re.findall(r"\d+", str(moq_raw))[0])
                except (IndexError, ValueError):
                    moq = 0

            # Shop info: parse from nested shop_info
            shop_data = item.get("shop_info", {}) or {}
            shop_info = ShopInfo(
                shop_name=str(shop_data.get("company_name", shop_data.get("login_id", ""))),
                member_id=str(shop_data.get("member_id", "")),
                seller_login_id=str(shop_data.get("login_id", "")),
                tp_year=int(shop_data.get("shop_years", 0) or 0),
                is_factory=bool(shop_data.get("is_factory", False)),
                comprehensive_rating=float(
                    (shop_data.get("score_info", {}) or {}).get("composite_new_score", 0) or 0
                ),
                location=" ".join(shop_data.get("location", []) if isinstance(shop_data.get("location"), list) else [str(shop_data.get("location", ""))]),
            )

            products.append(RawProduct(
                item_id=item_id,
                title=str(item.get("title", "")),
                img=str(item.get("img", "")),
                offer_price=price_str,
                price_usd=price_usd,
                min_order_quantity=moq,
                shop_info=shop_info,
                platform="1688",
                url=str(item.get("product_url", f"https://detail.1688.com/offer/{item_id}.html")),
                keyword_used=keyword,
            ))

        logger.info(f"1688 search returned {len(products)} products for '{keyword}'")
        return products

    except Exception as e:
        logger.error(f"1688 search failed for '{keyword}': {e}\n{traceback.format_exc()}")
        return []


async def _search_alibaba(keyword: str, page: int = 1) -> list[RawProduct]:
    """Search Alibaba via TMAPI."""
    url = f"{TMAPI_BASE_URL}/alibaba/search/items"
    params = {
        "keywords": keyword,  # Alibaba TMAPI uses 'keywords' (plural)
        "page": str(page),
        "apiToken": TM_API_KEY,
    }

    try:
        logger.info(f"Searching Alibaba: '{keyword}' page={page} url={url}")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, params=params)
        logger.info(f"Alibaba response status: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()

        # TMAPI returns: {code, msg, data: {items: [...]}}
        data_obj = data.get("data", {})
        items = []
        if isinstance(data_obj, dict):
            items = data_obj.get("items", data_obj.get("result", []))
        if not items and isinstance(data_obj, list):
            items = data_obj

        products = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("item_id", item.get("itemId", item.get("productId", ""))))
            if not item_id:
                continue

            # Alibaba prices in USD
            price_str = str(item.get("price", "") or item.get("offer_price", "0"))
            price_info = item.get("price_info", {}) or {}
            if price_info.get("price_min"):
                price_str = str(price_info["price_min"])
            price_usd = _extract_lowest_price(price_str)

            moq = 0
            moq_raw = item.get("min_order_quantity", item.get("moq", 0))
            if moq_raw:
                try:
                    moq = int(re.findall(r"\d+", str(moq_raw))[0])
                except (IndexError, ValueError):
                    moq = 0

            # Alibaba shop info has different structure
            shop_data = item.get("shop_info", {}) or {}
            shop_info = ShopInfo(
                shop_name=str(shop_data.get("company_name", "")),
                member_id=str(shop_data.get("company_id", "")),
                seller_login_id=str(shop_data.get("company_id", "")),
                tp_year=int(shop_data.get("shop_level", 0) or 0),
                is_factory=bool(shop_data.get("is_verified_supplier", False)),
                comprehensive_rating=float(
                    (item.get("review_info", {}) or {}).get("rating_score", 0) or 0
                ),
                location=str(shop_data.get("company_region", "")),
            )

            products.append(RawProduct(
                item_id=item_id,
                title=str(item.get("title", "")),
                img=str(item.get("img", "")),
                offer_price=price_str,
                price_usd=price_usd,
                min_order_quantity=moq,
                shop_info=shop_info,
                platform="alibaba",
                url=f"https://www.alibaba.com/product-detail/{item_id}.html",
                keyword_used=keyword,
            ))

        logger.info(f"Alibaba search returned {len(products)} products for '{keyword}'")
        return products

    except Exception as e:
        logger.error(f"Alibaba search failed for '{keyword}': {e}\n{traceback.format_exc()}")
        return []


async def search_products(keywords: Keywords) -> list[RawProduct]:
    """Run parallel searches on 1688 and Alibaba. Returns combined raw products."""

    tasks = []

    # 1688 CN search - use first CN keyword (or first EN if no CN)
    if keywords.cn_keywords:
        tasks.append(_search_1688(keywords.cn_keywords[0]))
    elif keywords.en_keywords:
        tasks.append(_search_1688(keywords.en_keywords[0]))

    # Alibaba search - use first EN keyword (or first CN if no EN)
    if keywords.en_keywords:
        tasks.append(_search_alibaba(keywords.en_keywords[0]))
    elif keywords.cn_keywords:
        tasks.append(_search_alibaba(keywords.cn_keywords[0]))

    # If we have multiple keywords, add more searches (but keep it fast)
    if len(keywords.cn_keywords) > 1:
        tasks.append(_search_1688(keywords.cn_keywords[1]))
    if len(keywords.en_keywords) > 1:
        tasks.append(_search_alibaba(keywords.en_keywords[1]))

    # Run all searches in parallel
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_products = []
    for r in results:
        if isinstance(r, list):
            all_products.extend(r)
        elif isinstance(r, Exception):
            logger.error(f"Search task failed: {r}")

    logger.info(f"Total products from all searches: {len(all_products)}")
    return all_products
