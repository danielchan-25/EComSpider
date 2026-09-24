from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from api.platforms.wildberries.scraper import WildberriesScraper

logger = logging.getLogger("ozon_api.wildberries.adapter")


def _rub(kopecks: int | None) -> float | None:
    """WB 价格单位为戈比(kopecks),转换为卢布。"""
    if kopecks is None:
        return None
    return round(kopecks / 100, 2)


def _discount(basic: int | None, product: int | None) -> str | None:
    if not basic or not product or basic <= product:
        return None
    percent = round((1 - product / basic) * 100)
    return f"-{percent}%" if percent > 0 else None


def _product_url(product_id: int) -> str:
    return f"https://www.wildberries.ru/catalog/{product_id}/detail.aspx"


class WildberriesAdapter:
    """将 Wildberries 搜索接口返回的商品 JSON 映射为数据库记录。"""

    async def collect(self, keyword: str, max_products: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        scraper = WildberriesScraper()
        products: list[dict[str, Any]] = []
        try:
            logger.info("wb_collection_started keyword=%s max_products=%s", keyword, max_products)
            await scraper.init_browser()
            raw_items = await scraper.search(keyword, max_products)
            if scraper.last_error and not raw_items:
                raise RuntimeError(scraper.last_error)
            details = await scraper.fetch_details(raw_items)
            for raw in raw_items:
                products.append(self._product_record(keyword, raw, details.get(raw.get("id"))))
        finally:
            await scraper.close()
            logger.info("wb_collection_finished products=%s", len(products))
        return products, []

    def _product_record(self, keyword: str, raw: dict[str, Any], card: dict[str, Any] | None = None) -> dict[str, Any]:
        product_id = raw.get("id")
        sizes = raw.get("sizes") or []
        price = sizes[0].get("price") if sizes else None
        basic = price.get("basic") if price else None
        product = price.get("product") if price else None
        characteristics = (raw.get("meta") or {}).get("characteristics") or []
        card = card or {}
        media = card.get("media") or {}
        photo_count = media.get("photo_count")
        scraper_shard = None
        # card.json does not expose its shard. The scraper stores it only for
        # URL generation, so resolve it from the cached vol when available.
        if product_id is not None:
            scraper_shard = card.get("_shard")
        if isinstance(scraper_shard, int) and product_id is not None:
            images = WildberriesScraper.image_urls(product_id, scraper_shard, photo_count)
        else:
            images = []
        category = card.get("subj_name")
        root_category = card.get("subj_root_name")
        category_path = " > ".join(part for part in (root_category, category) if part) or None
        specs = {"search_characteristics": characteristics, "options": card.get("options") or []}
        crawled_at = datetime.now(timezone.utc).isoformat()
        return {
            "platform_product_id": str(product_id) if product_id is not None else None,
            "keyword": keyword,
            "title": raw.get("name"),
            "brand": raw.get("brand"),
            "price": _rub(product),
            "original_price": _rub(basic),
            "currency": "RUB",
            "discount": _discount(basic, product),
            "rating": raw.get("rating"),
            "review_count": raw.get("feedbacks"),
            "sales_count": None,
            "seller_id": str(raw["supplierId"]) if raw.get("supplierId") is not None else None,
            "seller_name": raw.get("supplier"),
            "seller_rating": raw.get("supplierRating"),
            "product_url": _product_url(product_id) if product_id is not None else None,
            "main_image_url": images[0] if images else None,
            "description": card.get("description") or None,
            "category_path": category_path,
            "specs_json": json.dumps(specs, ensure_ascii=False),
            "images_json": json.dumps(images, ensure_ascii=False),
            "raw_jsonld": None,
            "raw_data_json": json.dumps({"search": raw, "card": card or None}, ensure_ascii=False),
            "crawled_at": crawled_at,
        }
