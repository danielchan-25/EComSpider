from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict
from typing import Any

from api.platforms.ozon.scraper import AdvancedOzonScraper, Product

logger = logging.getLogger("ozon_api.adapter")


def _number(value: str | None) -> float | None:
    if not value:
        return None
    text = value.replace("\u2009", "").replace(" ", "").replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


def _integer(value: str | None) -> int | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return int(digits) if digits else None


def _currency(value: str | None) -> str | None:
    if not value:
        return None
    if "¥" in value:
        return "CNY"
    if "₽" in value:
        return "RUB"
    return None


class OzonAdapter:
    """Maps Ozon scraper output to the API database contract."""

    async def collect(self, keyword: str, max_products: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        scraper = AdvancedOzonScraper()
        products: list[dict[str, Any]] = []
        reviews: list[dict[str, Any]] = []
        try:
            logger.info("collection_started keyword=%s max_products=%s", keyword, max_products)
            await scraper.init_browser()
            page_count = max(1, (max_products + 35) // 36)
            search_items = await scraper.search_products(keyword, page_count, max_products)
            if scraper.last_error:
                raise RuntimeError(scraper.last_error)

            for item in search_items:
                logger.info("product_collection_started url=%s", item.url)
                detail = await self._detail_or_list_item(scraper, item)
                product = self._product_record(keyword, detail)
                products.append(product)
                reviews.extend(await self._review_records(scraper, product))
                logger.info("product_collection_finished product_id=%s reviews_total=%s", product["platform_product_id"], len(reviews))
                await asyncio.sleep(0)
        finally:
            await scraper.close()
            logger.info("collection_finished products=%s reviews=%s", len(products), len(reviews))
        return products, reviews

    async def _detail_or_list_item(self, scraper: AdvancedOzonScraper, item: Product) -> Product:
        try:
            detail = await scraper.scrape_product(item.url)
            if detail and detail.title:
                return detail
        except Exception:
            pass
        return item

    def _product_record(self, keyword: str, item: Product) -> dict[str, Any]:
        raw = asdict(item)
        price_source = item.price or item.old_price
        return {
            "platform_product_id": item.product_id or self._id_from_url(item.url),
            "keyword": keyword,
            "title": item.title,
            "brand": None,
            "price": _number(item.price),
            "original_price": _number(item.old_price),
            "currency": _currency(price_source),
            "discount": item.discount,
            "rating": _number(item.rating),
            "review_count": _integer(item.reviews_count),
            "sales_count": None,
            "seller_id": None,
            "seller_name": item.seller_name,
            "seller_rating": _number(item.seller_rating),
            "product_url": item.url,
            "main_image_url": item.images[0] if item.images else None,
            "description": item.description,
            "category_path": None,
            "specs_json": json.dumps(item.characteristics, ensure_ascii=False),
            "images_json": json.dumps(item.images, ensure_ascii=False),
            "raw_jsonld": None,
            "raw_data_json": json.dumps(raw, ensure_ascii=False),
            "crawled_at": item.scraped_at,
        }

    async def _review_records(self, scraper: AdvancedOzonScraper, product: dict[str, Any]) -> list[dict[str, Any]]:
        if not scraper.page or not product["platform_product_id"]:
            return []
        try:
            review_url = product["product_url"].split("?", 1)[0].rstrip("/") + "/reviews/"
            try:
                review_loaded = await asyncio.wait_for(
                    scraper.navigate_with_retry(review_url), timeout=12
                )
            except asyncio.TimeoutError:
                logger.warning("review_navigation_timeout product_id=%s url=%s timeout_seconds=12", product["platform_product_id"], review_url)
                return []
            if not review_loaded:
                logger.warning("review_navigation_failed product_id=%s url=%s", product["platform_product_id"], review_url)
                return []
            texts = await scraper.page.evaluate(r"""
                () => {
                    const body = document.body?.innerText || '';
                    if (/目前还没有评级|没有评论|Нет отзывов/i.test(body)) return [];
                    const chunks = body.match(/(?:优点|Достоинства)\\s*[:：]?\\s*[\\s\\S]{20,1200}?(?=(?:优点|Достоинства)\\s*[:：]?|$)/gi) || [];
                    return chunks.slice(0, 10).map(x => x.trim());
                }
            """)
        except Exception:
            return []
        return [{
            "platform_product_id": product["platform_product_id"], "platform_review_id": None,
            "user_name": None, "user_id": None, "rating": None, "content": text,
            "review_time": None, "like_count": None, "dislike_count": None,
            "variant_info": None, "images_json": "[]",
            "raw_data_json": json.dumps({"source": "ozon_reviews_page"}, ensure_ascii=False),
            "crawled_at": product["crawled_at"],
        } for text in texts if text]

    @staticmethod
    def _id_from_url(url: str) -> str | None:
        match = re.search(r"-(\d+)(?:/|\?)", url)
        return match.group(1) if match else None
