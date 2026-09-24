#!/usr/bin/env python3
"""
Wildberries.ru 采集器。

采集路线(2026-09 实测通过):
  1. 通过 CDP 连接真实 Chrome(保留真实指纹与 JS 执行环境);
  2. 加载首页,等待 JS 生成的 x_wbaas_token cookie(get 型 WaaS 反爬令牌);
  3. 在页面上下文内 fetch 搜索 API(/__internal/u-search/.../v18/search),
     携带 deviceid / x-requested-with / x-spa-version / x-queryid / x-userid 请求头,
     按 page 参数分页采集,返回原始商品 JSON。

说明:直连 curl 会被 498(缺 WaaS token)或 429(搜索域限流)拦截,
必须借助浏览器会话获取令牌后,用页面同源请求采集。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any
from urllib.parse import quote

import requests

logger = logging.getLogger("ozon_api.wildberries.scraper")

from playwright.async_api import async_playwright

try:
    from .config import (
        BROWSER_CONFIG, CARD_CONFIG, SEARCH_CONFIG, SPA_VERSION,
        REQUEST_CONFIG, SEARCH_QUERY_ALIASES,
    )
except ImportError:
    from config import (
        BROWSER_CONFIG, CARD_CONFIG, SEARCH_CONFIG, SPA_VERSION,
        REQUEST_CONFIG, SEARCH_QUERY_ALIASES,
    )


class WildberriesScraper:
    """Wildberries.ru 采集器,复用真实 Chrome 会话。"""

    def __init__(self) -> None:
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.uses_cdp = bool(BROWSER_CONFIG.get("cdp_url"))
        self.device_id: str | None = None
        self.spa_version: str = SPA_VERSION
        self.last_error: str | None = None
        self._seen_ids: set[int] = set()
        self._shard_cache: dict[str, int] = {}
        self._shard_lock = threading.Lock()

    # ---------- 浏览器初始化 ----------

    async def init_browser(self) -> None:
        cdp_url = BROWSER_CONFIG["cdp_url"]
        logger.info("wb_browser_init cdp_url=%s", cdp_url)
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.connect_over_cdp(cdp_url)
        if not self.browser.contexts:
            raise RuntimeError(f"CDP 浏览器没有可用上下文: {cdp_url}")
        self.context = self.browser.contexts[0]
        await self._ensure_page()
        await self._ensure_session()
        logger.info("wb_browser_ready page_url=%s device_id=%s", self.page.url, self.device_id)

    async def _ensure_page(self):
        """复用 Wildberries 或空白页；兜底新开页且不在结束时关闭。"""
        existing = next(
            (p for p in reversed(self.context.pages) if "wildberries.ru" in p.url),
            None,
        )
        if existing is not None:
            self.page = existing
            return
        blank = next(
            (p for p in self.context.pages if p.url in {"about:blank", "chrome://newtab/", "edge://newtab/"}),
            None,
        )
        if blank is not None:
            self.page = blank
            return
        self.page = await self.context.new_page()
        logger.info("wb_browser_page_created page_url=%s", self.page.url)

    async def close(self) -> None:
        """只断开 CDP 连接；不关闭或刷新任何浏览器标签页。"""
        if self.playwright:
            await self.playwright.stop()

    # ---------- 会话与令牌 ----------

    async def _ensure_session(self) -> None:
        """确保页面位于首页且拥有 x_wbaas_token cookie 与 deviceid。"""
        if "wildberries.ru" not in (self.page.url or ""):
            await self._goto_homepage()
        token = await self._wait_for_token()
        if not token:
            await self._goto_homepage()   # 令牌缺失时重新加载
            token = await self._wait_for_token()
        if not token:
            self.last_error = "未能获取 Wildberries 反爬令牌 x_wbaas_token"
            raise RuntimeError(self.last_error)
        await self._load_or_create_device_id()

    async def _goto_homepage(self) -> None:
        logger.info("wb_goto_homepage url=%s", BROWSER_CONFIG["homepage"])
        await self.page.goto(
            BROWSER_CONFIG["homepage"],
            wait_until="domcontentloaded",
            timeout=REQUEST_CONFIG["page_load_timeout_ms"],
        )

    async def _wait_for_token(self) -> str | None:
        deadline = asyncio.get_event_loop().time() + REQUEST_CONFIG["token_wait_seconds"]
        while asyncio.get_event_loop().time() < deadline:
            cookies = await self.context.cookies(BROWSER_CONFIG["homepage"])
            for cookie in cookies:
                if cookie.get("name") == "x_wbaas_token" and cookie.get("value"):
                    return cookie["value"]
            await asyncio.sleep(1.5)
        return None

    async def _load_or_create_device_id(self) -> None:
        value = await self.page.evaluate(
            "() => { const v = localStorage.getItem('deviceid'); return v || ''; }"
        )
        if value:
            self.device_id = value
            return
        created = await self.page.evaluate(
            """() => {
                const id = 'site_' + crypto.randomUUID().replace(/-/g, '');
                localStorage.setItem('deviceid', id);
                return id;
            }"""
        )
        self.device_id = created
        logger.info("wb_device_id_created id=%s", created)

    def _build_search_url(self, keyword: str, page: int) -> str:
        params = {
            "ab_testing": "false",
            "appType": str(SEARCH_CONFIG["app_type"]),
            "autoselectFilters": "false",
            "curr": SEARCH_CONFIG["curr"],
            "dest": str(SEARCH_CONFIG["dest"]),
            "hide_vflags": "4294967296",
            "inheritFilters": "true",
            "lang": SEARCH_CONFIG["lang"],
            "locale": SEARCH_CONFIG["locale"],
            "query": keyword,
            "resultset": "catalog",
            "sort": SEARCH_CONFIG["sort"],
            "sppFixGeo": "4",
            "suppressSpellcheck": "false",
            "page": str(page),
        }
        querystring = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
        return f"{SEARCH_CONFIG['base']}?{querystring}"

    def _build_query_id(self) -> str:
        now = datetime.now()
        ts = now.strftime("%Y%m%d%H%M%S")
        return f"qid0{ts}"   # x-userid=0(未登录)

    # ---------- 搜索 ----------

    async def search(self, keyword: str, max_products: int | None = None) -> list[dict[str, Any]]:
        """按关键词分页搜索,返回原始商品 JSON 列表。"""
        query = SEARCH_QUERY_ALIASES.get(keyword, keyword)
        logger.info("wb_search_started keyword=%s query=%s max_products=%s", keyword, query, max_products)
        self._seen_ids.clear()
        collected: list[dict[str, Any]] = []
        max_pages = SEARCH_CONFIG["max_pages"]
        page = 1
        while page <= max_pages:
            if max_products is not None and len(collected) >= max_products:
                break
            payload = await self._fetch_search_page(query, page)
            if payload is None:
                break
            products = payload.get("products") or []
            total = payload.get("total")
            logger.info("wb_search_page page=%s products=%s total=%s", page, len(products), total)
            if not products:
                break
            for product in products:
                product_id = product.get("id")
                if product_id is None or product_id in self._seen_ids:
                    continue
                self._seen_ids.add(product_id)
                collected.append(product)
            if len(products) < SEARCH_CONFIG["page_size"]:
                break
            page += 1
            await asyncio.sleep(random.uniform(
                REQUEST_CONFIG["page_gap_min"], REQUEST_CONFIG["page_gap_max"],
            ))
        if max_products is not None:
            collected = collected[:max_products]
        logger.info("wb_search_finished keyword=%s collected=%s", keyword, len(collected))
        return collected

    async def _fetch_search_page(self, query: str, page: int) -> dict[str, Any] | None:
        url = self._build_search_url(query, page)
        for attempt in range(1, REQUEST_CONFIG["max_retries"] + 1):
            res = await self.page.evaluate(
                """(args) => (async () => {
                    const r = await fetch(args.url, {
                        headers: {
                            'accept': 'application/json',
                            'deviceid': args.deviceId,
                            'x-requested-with': 'XMLHttpRequest',
                            'x-spa-version': args.spaVersion,
                            'x-queryid': args.qid,
                            'x-userid': '0',
                        }
                    });
                    return { status: r.status, body: await r.text() };
                })()""",
                {
                    "url": url,
                    "deviceId": self.device_id,
                    "spaVersion": self.spa_version,
                    "qid": self._build_query_id(),
                },
            )
            status = res.get("status")
            if status == 200:
                try:
                    return json.loads(res.get("body") or "{}")
                except json.JSONDecodeError:
                    logger.warning("wb_search_bad_json page=%s", page)
                    return None
            if status in (403, 498):
                logger.warning("wb_search_blocked status=%s attempt=%s page=%s", status, attempt, page)
                await self._goto_homepage()
                await self._wait_for_token()
                await asyncio.sleep(5 * attempt)
                continue
            if status == 429:
                backoff = REQUEST_CONFIG["retry_backoff_seconds"] * attempt
                logger.warning("wb_search_rate_limited page=%s backoff=%s", page, backoff)
                await asyncio.sleep(backoff)
                continue
            logger.warning("wb_search_unexpected_status status=%s page=%s", status, page)
            self.last_error = f"Wildberries 搜索接口返回 HTTP {status}"
            return None
        self.last_error = f"Wildberries 搜索接口多次失败(关键词 {query}, 第 {page} 页)"
        return None

    # ---------- 商品详情（公开 CDN，无需浏览器令牌） ----------

    @staticmethod
    def card_parts(product_id: int) -> tuple[str, str]:
        """返回 WB 静态资源路径使用的 vol / part。"""
        text = str(product_id)
        if len(text) <= 5:
            raise ValueError(f"无效 Wildberries 商品 ID: {product_id}")
        return text[:-5], text[:-3]

    @classmethod
    def card_path(cls, product_id: int) -> str:
        vol, part = cls.card_parts(product_id)
        return f"/vol{vol}/part{part}/{product_id}/info/ru/card.json"

    @classmethod
    def image_urls(cls, product_id: int, shard: int, photo_count: int | None) -> list[str]:
        try:
            count = int(photo_count or 0)
        except (TypeError, ValueError):
            return []
        if count < 1:
            return []
        vol, part = cls.card_parts(product_id)
        base = CARD_CONFIG["cdn_template"].format(shard=shard)
        return [f"{base}/vol{vol}/part{part}/{product_id}/images/big/{index}.webp" for index in range(1, count + 1)]

    def _card_url(self, product_id: int, shard: int) -> str:
        return CARD_CONFIG["cdn_template"].format(shard=shard) + self.card_path(product_id)

    @staticmethod
    def _vol(product_id: int) -> str:
        return WildberriesScraper.card_parts(product_id)[0]

    def _resolve_shard(self, product_id: int) -> int | None:
        """探测一个 vol 的 CDN 分片；成功结果在本任务内缓存。"""
        vol = self._vol(product_id)
        with self._shard_lock:
            cached = self._shard_cache.get(vol)
        if cached is not None:
            return cached

        shards = list(range(40, CARD_CONFIG["shard_end"] + 1)) + list(range(CARD_CONFIG["shard_start"], 40))
        headers = {"User-Agent": CARD_CONFIG["user_agent"]}

        def probe(shard: int) -> int | None:
            try:
                reply = requests.get(self._card_url(product_id, shard), headers=headers,
                                     timeout=REQUEST_CONFIG["card_timeout_seconds"])
                return shard if reply.status_code == 200 else None
            except requests.RequestException:
                return None

        # Do not wait for an unavailable shard before trying the next one.
        # The CDN mapping is stable per vol, so the first 200 result is cached
        # and reused by every product from that volume.
        with ThreadPoolExecutor(max_workers=REQUEST_CONFIG["shard_probe_workers"]) as executor:
            futures = [executor.submit(probe, shard) for shard in shards]
            for future in as_completed(futures):
                shard = future.result()
                if shard is not None:
                    for pending in futures:
                        pending.cancel()
                    with self._shard_lock:
                        self._shard_cache[vol] = shard
                    return shard
        logger.warning("wb_card_shard_not_found product_id=%s vol=%s", product_id, vol)
        return None

    def _fetch_card(self, product_id: int, shard: int) -> dict[str, Any] | None:
        try:
            reply = requests.get(
                self._card_url(product_id, shard),
                headers={"User-Agent": CARD_CONFIG["user_agent"]},
                timeout=REQUEST_CONFIG["card_timeout_seconds"],
            )
            reply.raise_for_status()
            return reply.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("wb_card_fetch_failed product_id=%s error=%s", product_id, exc)
            return None

    async def fetch_details(self, raw_items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        """批量下载 card.json；单项失败返回空，绝不丢弃搜索列表项。"""
        ids = [item.get("id") for item in raw_items if isinstance(item.get("id"), int)]
        if not ids:
            return {}

        representatives: dict[str, int] = {}
        for product_id in ids:
            representatives.setdefault(self._vol(product_id), product_id)

        def collect() -> dict[int, dict[str, Any]]:
            with ThreadPoolExecutor(max_workers=REQUEST_CONFIG["shard_probe_workers"]) as executor:
                shards_by_vol = dict(zip(representatives, executor.map(self._resolve_shard, representatives.values())))
            product_shards = {product_id: shards_by_vol[self._vol(product_id)] for product_id in ids if shards_by_vol.get(self._vol(product_id))}
            with ThreadPoolExecutor(max_workers=REQUEST_CONFIG["detail_concurrency"]) as executor:
                cards = executor.map(lambda item: self._fetch_card(*item), product_shards.items())
                details: dict[int, dict[str, Any]] = {}
                for product_id, card in zip(product_shards, cards):
                    if isinstance(card, dict):
                        # Keep the resolved CDN shard with the transient detail
                        # record so the adapter can derive public image URLs.
                        card["_shard"] = product_shards[product_id]
                        details[product_id] = card
                return details

        details = await asyncio.to_thread(collect)
        logger.info("wb_card_details_finished succeeded=%s requested=%s", len(details), len(ids))
        return details
