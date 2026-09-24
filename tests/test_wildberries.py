from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from api.platforms.wildberries.adapter import WildberriesAdapter
from api.platforms.wildberries.config import REQUEST_CONFIG
from api.platforms.wildberries.scraper import WildberriesScraper


class WildberriesHelperTests(unittest.TestCase):
    def test_card_paths_and_image_urls(self) -> None:
        product_id = 1380363124
        self.assertEqual(WildberriesScraper.card_parts(product_id), ("13803", "1380363"))
        self.assertEqual(
            WildberriesScraper.card_path(product_id),
            "/vol13803/part1380363/1380363124/info/ru/card.json",
        )
        self.assertEqual(
            WildberriesScraper.image_urls(product_id, 46, 2),
            [
                "https://basket-46.wbbasket.ru/vol13803/part1380363/1380363124/images/big/1.webp",
                "https://basket-46.wbbasket.ru/vol13803/part1380363/1380363124/images/big/2.webp",
            ],
        )

    def test_adapter_maps_search_and_card_details(self) -> None:
        raw = {
            "id": 1380363124, "name": "商品", "brand": "品牌", "rating": 5,
            "feedbacks": 9, "supplier": "卖家", "supplierId": 12, "supplierRating": 4.8,
            "totalQuantity": 4, "sizes": [{"price": {"basic": 10000, "product": 7500}}],
            "meta": {"characteristics": [{"name": "内存", "values": ["16GB"]}]},
        }
        card = {
            "subj_name": "笔记本", "subj_root_name": "电脑", "description": "完整描述",
            "media": {"photo_count": 2}, "options": [{"name": "颜色"}], "_shard": 46,
        }
        record = WildberriesAdapter()._product_record("电脑", raw, card)
        self.assertEqual(record["price"], 75.0)
        self.assertEqual(record["discount"], "-25%")
        self.assertEqual(record["category_path"], "电脑 > 笔记本")
        self.assertEqual(record["main_image_url"], "https://basket-46.wbbasket.ru/vol13803/part1380363/1380363124/images/big/1.webp")
        self.assertEqual(len(json.loads(record["images_json"])), 2)
        self.assertEqual(json.loads(record["specs_json"])["options"], [{"name": "颜色"}])


class WildberriesRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_retries_then_returns_payload(self) -> None:
        scraper = WildberriesScraper()
        scraper.device_id = "site_test"
        scraper.page = type("Page", (), {"evaluate": AsyncMock(side_effect=[
            {"status": 429, "body": ""}, {"status": 200, "body": '{"products": []}'},
        ])})()
        with patch.dict(REQUEST_CONFIG, {"max_retries": 2, "retry_backoff_seconds": 1}), patch(
            "api.platforms.wildberries.scraper.asyncio.sleep", new=AsyncMock()
        ) as sleep:
            payload = await scraper._fetch_search_page("test", 1)
        self.assertEqual(payload, {"products": []})
        sleep.assert_awaited_once_with(1)

    async def test_blocked_response_refreshes_session_then_retries(self) -> None:
        scraper = WildberriesScraper()
        scraper.device_id = "site_test"
        scraper.page = type("Page", (), {"evaluate": AsyncMock(side_effect=[
            {"status": 498, "body": ""}, {"status": 200, "body": '{"products": []}'},
        ])})()
        scraper._goto_homepage = AsyncMock()
        scraper._wait_for_token = AsyncMock(return_value="token")
        with patch.dict(REQUEST_CONFIG, {"max_retries": 2}), patch(
            "api.platforms.wildberries.scraper.asyncio.sleep", new=AsyncMock()
        ):
            payload = await scraper._fetch_search_page("test", 1)
        self.assertEqual(payload, {"products": []})
        scraper._goto_homepage.assert_awaited_once()
        scraper._wait_for_token.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
