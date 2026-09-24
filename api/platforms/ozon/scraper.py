#!/usr/bin/env python3
"""
Ozon.ru 高级爬虫 - 包含代理轮换和反检测
"""

import asyncio
import json
import logging
import random
import time
import requests
from typing import Dict, List, Optional
from urllib.parse import quote, unquote_plus, urlsplit
from dataclasses import dataclass, asdict
from datetime import datetime

logger = logging.getLogger("ozon_api.scraper")

from playwright.async_api import async_playwright, Page, BrowserContext
from playwright_stealth import Stealth

try:
    from .config import (
        PROXY_CONFIG, REQUEST_CONFIG, SEARCH_CONFIG,
        OUTPUT_CONFIG, USER_AGENTS, STEALTH_CONFIG, BROWSER_CONFIG,
        SEARCH_QUERY_ALIASES,
    )
except ImportError:
    from config import (
        PROXY_CONFIG, REQUEST_CONFIG, SEARCH_CONFIG,
        OUTPUT_CONFIG, USER_AGENTS, STEALTH_CONFIG, BROWSER_CONFIG,
        SEARCH_QUERY_ALIASES,
    )


@dataclass
class Product:
    """产品数据类"""
    url: str
    title: str
    price: Optional[str] = None
    old_price: Optional[str] = None
    discount: Optional[str] = None
    rating: Optional[str] = None
    reviews_count: Optional[str] = None
    seller_name: Optional[str] = None
    seller_rating: Optional[str] = None
    images: List[str] = None
    characteristics: List[Dict] = None
    description: Optional[str] = None
    product_id: Optional[str] = None
    scraped_at: Optional[str] = None

    def __post_init__(self):
        if self.images is None:
            self.images = []
        if self.characteristics is None:
            self.characteristics = []
        if self.scraped_at is None:
            self.scraped_at = datetime.now().isoformat()


class ProxyRotator:
    """代理轮换器"""

    def __init__(self, proxies: List[Dict]):
        self.proxies = proxies
        self.current_index = 0
        self.request_count = 0
        self.max_requests_per_proxy = 100  # 每个代理最大请求数

    def get_next_proxy(self) -> Optional[Dict]:
        """获取下一个代理"""
        if not self.proxies:
            return None

        self.request_count += 1

        # 如果达到最大请求数，切换到下一个代理
        if self.request_count >= self.max_requests_per_proxy:
            self.current_index = (self.current_index + 1) % len(self.proxies)
            self.request_count = 0
            print(f"切换到代理: {self.current_index + 1}/{len(self.proxies)}")

        return self.proxies[self.current_index]


class AdvancedOzonScraper:
    """高级Ozon.ru爬虫"""

    def __init__(self, headless: bool = True, use_proxy: bool = False):
        self.headless = headless
        self.use_proxy = use_proxy
        self.browser = None
        self.context = None
        self.page = None
        self.last_error = None
        self.owns_browser = False
        self.uses_cdp = False
        self.proxy_rotator = None
        self.scraped_urls = set()
        # Only CDP targets opened by this scraper may be closed on cleanup.
        # Existing user tabs in the shared Chrome session are never touched.
        self.created_cdp_target_ids = set()

        # 初始化代理轮换器
        if use_proxy and PROXY_CONFIG.get("server"):
            self.proxy_rotator = ProxyRotator([PROXY_CONFIG])

    async def init_browser(self):
        """初始化浏览器"""
        logger.info("browser_init cdp_url=%s use_proxy=%s", BROWSER_CONFIG.get("cdp_url"), self.use_proxy)
        self.playwright = await async_playwright().start()

        cdp_url = BROWSER_CONFIG.get("cdp_url")
        if cdp_url:
            # Reuse the user's Chrome profile and its Ozon session.  Do not
            # create a proxy or alter the profile's browser fingerprint.
            self.browser = await self.playwright.chromium.connect_over_cdp(cdp_url)
            self.uses_cdp = True
            if not self.browser.contexts:
                raise RuntimeError(f"CDP 浏览器没有可用上下文: {cdp_url}")
            self.context = self.browser.contexts[0]
            self.page = next(
                (page for page in reversed(self.context.pages) if "ozon.ru" in page.url),
                None,
            )
            if self.page is None:
                self.page = await self.context.new_page()
            logger.info("browser_connected contexts=%s initial_url=%s", len(self.browser.contexts), self.page.url)
            return

        # 获取代理配置
        proxy_config = None
        if self.use_proxy and self.proxy_rotator:
            proxy_config = self.proxy_rotator.get_next_proxy()

        # 浏览器启动参数
        launch_args = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-web-security",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-infobars",
                "--window-size=1920,1080",
                "--disable-extensions",
                "--disable-gpu",
                "--disable-software-rasterizer",
            ]
        }

        if proxy_config:
            launch_args["proxy"] = proxy_config

        self.browser = await self.playwright.chromium.launch(**launch_args)
        self.owns_browser = True

        # 创建浏览器上下文
        context_args = {
            "viewport": {"width": 1920, "height": 1080},
            "user_agent": random.choice(USER_AGENTS),
            "locale": "ru-RU",
            "timezone_id": "Europe/Moscow",
            "geolocation": {"latitude": 55.7558, "longitude": 37.6173},
            "permissions": ["geolocation"],
            "ignore_https_errors": True,
        }

        self.context = await self.browser.new_context(**context_args)
        self.page = await self.context.new_page()

        # 应用stealth插件
        stealth = Stealth()
        await stealth.apply_stealth_async(self.page)

        # 设置额外的HTTP头
        await self.page.set_extra_http_headers({
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Cache-Control": "max-age=0",
            "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        })

    async def close(self):
        """关闭浏览器"""
        logger.info("browser_close created_targets=%s", len(self.created_cdp_target_ids))
        if self.uses_cdp:
            cdp_url = BROWSER_CONFIG.get("cdp_url")
            for target_id in self.created_cdp_target_ids:
                try:
                    requests.get(f"{cdp_url}/json/close/{target_id}", timeout=5)
                    logger.info("cdp_target_closed target_id=%s", target_id)
                except requests.RequestException:
                    pass
            self.created_cdp_target_ids.clear()
        if self.owns_browser and self.context:
            await self.context.close()
        if self.owns_browser and self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def random_delay(self, min_seconds: float = None, max_seconds: float = None):
        """随机延迟"""
        if min_seconds is None:
            min_seconds = REQUEST_CONFIG["min_delay"]
        if max_seconds is None:
            max_seconds = REQUEST_CONFIG["max_delay"]

        delay = random.uniform(min_seconds, max_seconds)
        logger.info("rate_limit_delay seconds=%.3f", delay)
        await asyncio.sleep(delay)

    async def open_cdp_page(self, url: str) -> bool:
        """Open a URL through CDP, reloading one blank page after 30 seconds."""
        cdp_url = BROWSER_CONFIG.get("cdp_url")
        if not cdp_url:
            return False

        try:
            logger.info("cdp_open_started url=%s", url)
            # Ozon may redirect a search URL to a category URL. Remember the
            # newly created target instead of relying solely on its final URL.
            existing_pages = {
                page for context in self.browser.contexts for page in context.pages
            }
            target = requests.put(
                f"{cdp_url}/json/new?{quote(url, safe='')}", timeout=10
            ).json()
            target_id = target.get("id")
            if target_id:
                self.created_cdp_target_ids.add(target_id)
                requests.put(f"{cdp_url}/json/activate/{target_id}", timeout=10)
                logger.info("cdp_target_created target_id=%s", target_id)

            # Ozon appends tracking query parameters after navigation.  Match
            # the stable origin/path instead of the complete URL so the CDP
            # target can be recovered without waiting for the full timeout.
            requested = urlsplit(url)
            is_reviews_page = requested.path.rstrip("/").endswith("/reviews")
            is_search_page = requested.path.rstrip("/").endswith("/search")
            is_product_page = requested.path.rstrip("/").startswith("/product/") and not is_reviews_page

            def is_requested_page(page_url: str) -> bool:
                actual = urlsplit(page_url)
                return (
                    actual.scheme == requested.scheme
                    and actual.netloc == requested.netloc
                    and actual.path.rstrip("/") == requested.path.rstrip("/")
                )

            candidate = None
            # A newly created CDP target can take a while to become visible in
            # Playwright. Wait up to 30 seconds for the initial render.
            for _ in range(120):
                for context in self.browser.contexts:
                    for page in reversed(context.pages):
                        if page not in existing_pages or is_requested_page(page.url):
                            candidate = page
                            if await page.locator('.tile-root[data-index]').count():
                                self.context = context
                                self.page = page
                                logger.info("cdp_open_ready target_id=%s final_url=%s", target_id, page.url)
                                return True
                            # Review pages do not contain product tiles. Once
                            # a non-blank review document is ready, hand it to
                            # the review parser instead of waiting 30 seconds
                            # for a selector that can never appear.
                            if is_reviews_page:
                                state = await page.evaluate("""
                                    () => ({
                                        readyState: document.readyState,
                                        bodyTextLength: (document.body?.innerText || '').trim().length,
                                    })
                                """)
                                if state["readyState"] != "loading" and state["bodyTextLength"]:
                                    self.context = context
                                    self.page = page
                                    logger.info("cdp_review_page_ready target_id=%s final_url=%s body_length=%s", target_id, page.url, state["bodyTextLength"])
                                    return True
                            if is_search_page:
                                state = await page.evaluate("""
                                    () => ({
                                        readyState: document.readyState,
                                        bodyTextLength: (document.body?.innerText || '').trim().length,
                                    })
                                """)
                                if state["readyState"] != "loading" and state["bodyTextLength"]:
                                    self.context = context
                                    self.page = page
                                    logger.info("cdp_search_document_ready target_id=%s final_url=%s body_length=%s", target_id, page.url, state["bodyTextLength"])
                                    return True
                            if is_product_page:
                                state = await page.evaluate("""
                                    () => ({
                                        readyState: document.readyState,
                                        title: document.querySelector('h1')?.textContent?.trim() || '',
                                    })
                                """)
                                if state["readyState"] != "loading" and state["title"]:
                                    self.context = context
                                    self.page = page
                                    logger.info("cdp_product_page_ready target_id=%s final_url=%s title=%s", target_id, page.url, state["title"])
                                    return True
                # This is a local DOM readiness check, not another Ozon
                # request. A shorter interval removes up to 0.75 s of idle
                # time per item while keeping the same 30 s recovery limit.
                await asyncio.sleep(0.25)

            if candidate is not None:
                state = await candidate.evaluate("""
                    () => ({
                        readyState: document.readyState,
                        hasBody: Boolean(document.body),
                        bodyTextLength: (document.body?.innerText || '').trim().length,
                    })
                """)
                if state["readyState"] == "loading" and not state["hasBody"]:
                    print("Ozon 页面加载超过 30 秒且为空白，执行 reload 后重试")
                    try:
                        await candidate.reload(wait_until="commit", timeout=30000)
                    except Exception as error:
                        print(f"Ozon 空白页 reload 未立即完成: {error}")

                    for _ in range(120):
                        if await candidate.locator('.tile-root[data-index]').count():
                            self.context = next(
                                context for context in self.browser.contexts if candidate in context.pages
                            )
                            self.page = candidate
                            return True
                        await asyncio.sleep(0.25)
        except Exception as error:
            logger.exception("cdp_open_failed url=%s error=%s", url, error)
            print(f"通过 CDP 打开下一页失败: {error}")
        return False

    async def navigate_with_retry(self, url: str, max_retries: int = None) -> bool:
        """带重试的导航"""
        if max_retries is None:
            max_retries = REQUEST_CONFIG["max_retries"]

        # A CDP-managed Chrome may have no Ozon tab open yet. Page.goto() on
        # such a shared session can remain pending while Chrome opens the
        # target in another tab. Create and activate the target through CDP
        # instead, then attach to its rendered page.
        if self.uses_cdp:
            if await self.open_cdp_page(url):
                await self.random_delay(2, 4)
                return True
            self.last_error = f"CDP Chrome 未能加载 Ozon 页面: {url}"
            print(self.last_error)
            return False

        for attempt in range(max_retries):
            try:
                # 检查是否需要切换代理
                if self.proxy_rotator and attempt > 0:
                    new_proxy = self.proxy_rotator.get_next_proxy()
                    if new_proxy:
                        # 重新初始化浏览器上下文
                        await self.context.close()
                        context_args = {
                            "viewport": {"width": 1920, "height": 1080},
                            "user_agent": random.choice(USER_AGENTS),
                            "locale": "ru-RU",
                            "timezone_id": "Europe/Moscow",
                            "proxy": new_proxy,
                        }
                        self.context = await self.browser.new_context(**context_args)
                        self.page = await self.context.new_page()
                        stealth = Stealth()
                        await stealth.apply_stealth_async(self.page)

                response = await self.page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=REQUEST_CONFIG["page_load_timeout"]
                )

                # Ozon returns an HTTP 403 challenge page when the request is
                # blocked.  It contains none of the normal search/product DOM,
                # so treating it as a successful navigation only leads to a
                # misleading empty result set after a selector timeout.
                if response and response.status in (401, 403, 429):
                    antibot = response.headers.get("ozon-antibot")
                    if antibot or response.status != 401:
                        self.last_error = (
                            f"Ozon 拒绝了本次请求（HTTP {response.status}，"
                            "触发反爬保护）。未使用代理，停止本次抓取。"
                        )
                        print(self.last_error)
                        return False

                await self.random_delay(2, 4)
                return True

            except Exception as e:
                print(f"导航失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(5 * (attempt + 1))  # 指数退避
                    try:
                        await self.page.reload()
                    except:
                        pass
        return False

    async def extract_composer_data(self) -> Optional[Dict]:
        """从composer-api提取数据"""
        try:
            # 等待页面加载完成
            await self.page.wait_for_load_state("networkidle", timeout=10000)

            # 尝试从页面提取composer数据
            data = await self.page.evaluate(r"""
                () => {
                    // 尝试从window.__NUXT__获取数据
                    if (window.__NUXT__) {
                        return window.__NUXT__;
                    }

                    // 尝试从script标签获取数据
                    const scripts = document.querySelectorAll('script');
                    for (const script of scripts) {
                        const text = script.textContent;
                        if (text.includes('widgetStates')) {
                            try {
                                // 提取JSON数据
                                const match = text.match(/window\.__NUXT__\s*=\s*({.*?});/s);
                                if (match) {
                                    return JSON.parse(match[1]);
                                }
                            } catch (e) {
                                // 忽略解析错误
                            }
                        }
                    }

                    return null;
                }
            """)

            return data

        except Exception as e:
            print(f"提取composer数据失败: {e}")
            return None

    async def extract_search_results_from_composer(self) -> List[Product]:
        """从composer数据提取搜索结果"""
        products = []

        try:
            # Ozon 现行页面使用 tileGridDesktop / tile-root，而不是旧的
            # searchResultsV2 容器。
            await self.page.wait_for_selector('.tile-root[data-index]', timeout=15000)

            # 提取产品数据
            raw_products = await self.page.evaluate("""
                () => {
                    const items = [];

                    // The page's internal state is not a stable public API.
                    // Extract the rendered product tiles instead.
                    const productCards = document.querySelectorAll('.tile-root[data-index]');
                    productCards.forEach(card => {
                        const titleElement = card.querySelector('.tsBody500Medium');
                        const titleLink = titleElement?.closest('a[href*="/product/"]');
                        const href = titleLink?.getAttribute('href');
                        const title = titleElement?.textContent?.trim();
                        const textNodes = [...card.querySelectorAll('span, div')]
                            .filter(node => node.children.length === 0)
                            .map(node => node.textContent.trim())
                            .filter(Boolean);
                        const price = card.querySelector('.tsHeadline500Medium')?.textContent;
                        const oldPrice = textNodes.find(text => /[₽¥]/.test(text) && text !== price);
                        const discount = textNodes.find(text => /^[-−]\\s*\\d+%$/.test(text));
                        const rating = textNodes.find(text => /^\\d[.,]\\d$/.test(text));
                        const reviews = textNodes.find(text => /(отзыв|评论|review)/i.test(text));
                        const image = card.querySelector('img')?.src;

                        if (href && title) {
                            items.push({
                                url: href.startsWith('http') ? href : `https://www.ozon.ru${href}`,
                                title,
                                price: price?.trim(),
                                old_price: oldPrice?.trim(),
                                discount: discount?.trim(),
                                rating: rating?.trim(),
                                reviews_count: reviews?.trim(),
                                image
                            });
                        }
                    });

                    return items;
                }
            """)

            # 转换为Product对象
            for item in raw_products:
                product = Product(
                    url=item.get('url', ''),
                    title=item.get('title', ''),
                    price=item.get('price'),
                    old_price=item.get('old_price'),
                    discount=item.get('discount'),
                    rating=item.get('rating'),
                    reviews_count=item.get('reviews_count'),
                    images=[item.get('image')] if item.get('image') else []
                )
                products.append(product)

        except Exception as e:
            print(f"提取搜索结果失败: {e}")

        return products

    async def extract_product_details_from_composer(self, url: str) -> Optional[Product]:
        """从composer数据提取产品详情"""
        try:
            if not await self.navigate_with_retry(url):
                return None

            # navigate_with_retry already waits for the product grid and keeps
            # one 2–4 second randomized pause. Do not add a second pause here:
            # it would slow every item without reducing request frequency.

            # 提取产品数据
            product_data = await self.page.evaluate(r"""
                () => {
                    const data = {};

                    // 基本信息
                    data.title = document.querySelector('h1')?.textContent?.trim();

                    // 价格信息
                    const priceWidget = document.querySelector('[data-widget="webPrice"]');
                    if (priceWidget) {
                        data.price = priceWidget.querySelector('span[class*="price"]')?.textContent?.trim();
                        data.old_price = priceWidget.querySelector('span[class*="old"]')?.textContent?.trim();
                        data.discount = priceWidget.querySelector('span[class*="discount"]')?.textContent?.trim();
                    }
                    // The translated Ozon page no longer exposes webPrice.
                    // The first price before the purchase button is the item
                    // price; recommendations appear later in the document.
                    if (!data.price) {
                        const body = document.body?.innerText || '';
                        const purchaseEnd = body.indexOf('添加到购物车');
                        const purchaseArea = purchaseEnd >= 0 ? body.slice(0, purchaseEnd) : body;
                        const prices = purchaseArea.match(/\d+(?:[,.]\d+)?\s*¥/g);
                        data.price = prices?.[0]?.trim() || null;
                    }

                    // 评分和评论
                    const ratingWidget = document.querySelector('[data-widget="webReviewProductScore"]');
                    if (ratingWidget) {
                        data.rating = ratingWidget.querySelector('span[class*="rating"]')?.textContent?.trim();
                        data.reviews_count = ratingWidget.querySelector('span[class*="count"]')?.textContent?.trim();
                    }

                    // 产品特性
                    const characteristics = [];
                    const charWidget = document.querySelector('[data-widget="webCharacteristics"]');
                    if (charWidget) {
                        const charElements = charWidget.querySelectorAll('span');
                        charElements.forEach(el => {
                            const text = el.textContent.trim();
                            if (text.includes(':')) {
                                const [key, value] = text.split(':').map(s => s.trim());
                                characteristics.push({ key, value });
                            }
                        });
                    }
                    data.characteristics = characteristics;

                    // 图片
                    const images = [];
                    const galleryWidget = document.querySelector('[data-widget="webGallery"]');
                    if (galleryWidget) {
                        const imgElements = galleryWidget.querySelectorAll('img');
                        imgElements.forEach(img => {
                            if (img.src && !img.src.includes('data:')) {
                                images.push(img.src);
                            }
                        });
                    }
                    data.images = images;

                    // 卖家信息
                    const sellerWidget = document.querySelector('[data-widget="webSeller"]');
                    if (sellerWidget) {
                        data.seller_name = sellerWidget.querySelector('a')?.textContent?.trim();
                        data.seller_url = sellerWidget.querySelector('a')?.href;
                        data.seller_rating = sellerWidget.querySelector('[class*="rating"]')?.textContent?.trim();
                    }

                    // 产品描述
                    const descWidget = document.querySelector('[data-widget="webDescription"]');
                    data.description = descWidget?.textContent?.trim();

                    // 产品ID
                    const skuWidget = document.querySelector('[data-widget="webSKU"]');
                    if (skuWidget) {
                        const text = skuWidget.textContent;
                        const match = text.match(/\\d+/);
                        data.product_id = match ? match[0] : null;
                    }

                    return data;
                }
            """)

            # 创建Product对象
            product = Product(
                url=url,
                title=product_data.get('title', ''),
                price=product_data.get('price'),
                old_price=product_data.get('old_price'),
                discount=product_data.get('discount'),
                rating=product_data.get('rating'),
                reviews_count=product_data.get('reviews_count'),
                seller_name=product_data.get('seller_name'),
                seller_rating=product_data.get('seller_rating'),
                images=product_data.get('images', []),
                characteristics=product_data.get('characteristics', []),
                description=product_data.get('description'),
                product_id=product_data.get('product_id')
            )

            return product

        except Exception as e:
            print(f"提取产品详情失败: {e}")
            return None

    async def search_products(
        self,
        query: str,
        max_pages: int = None,
        max_products: int = None,
    ) -> List[Product]:
        """搜索产品"""
        if max_pages is None:
            max_pages = SEARCH_CONFIG["max_pages"]

        all_products = []

        search_term = SEARCH_QUERY_ALIASES.get(query, query)
        search_url = f"https://www.ozon.ru/search/?from_global=true&text={quote(search_term)}"

        print(f"开始搜索: {query}")
        print(f"搜索词: {search_term}")
        print(f"搜索URL: {search_url}")

        # Reuse a loaded CDP tab for the same query. Prefer the first result
        # page over a previously opened page-2 tab.
        loaded_page = None
        if self.uses_cdp:
            for context in self.browser.contexts:
                for candidate in reversed(context.pages):
                    decoded_url = unquote_plus(candidate.url)
                    if search_term in decoded_url and "page=" not in candidate.url:
                        if await candidate.locator('.tile-root[data-index]').count():
                            loaded_page = candidate
                            self.context = context
                            self.page = candidate
                            break
                if loaded_page:
                    break

        if loaded_page:
            print("复用 CDP Chrome 中已打开的搜索页")
        elif not await self.navigate_with_retry(search_url):
            print("无法访问搜索页面")
            return all_products

        seen_urls = set()

        async def collect_visible_products():
            """Collect the current virtualized tiles without duplicating URLs."""
            products = await self.extract_search_results_from_composer()
            added = 0
            for product in products:
                if product.url and product.url not in seen_urls:
                    seen_urls.add(product.url)
                    all_products.append(product)
                    added += 1
            return added

        added = await collect_visible_products()
        print(f"第1页当前视图新增 {added} 个产品，共 {len(all_products)} 个")

        # Ozon virtualizes its product tiles.  Scroll within each page so cards
        # outside the initial viewport are rendered and can be collected.
        page_num = 1
        no_new_scrolls = 0
        max_scrolls = max_pages * 12
        for _ in range(max_scrolls):
            if max_products is not None and len(all_products) >= max_products:
                break

            position = await self.page.evaluate("""
                () => ({
                    y: window.scrollY,
                    height: document.documentElement.scrollHeight,
                    viewport: window.innerHeight,
                })
            """)
            await self.page.mouse.wheel(0, max(position["viewport"] * 0.85, 600))
            await self.random_delay(1, 2)
            added = await collect_visible_products()
            no_new_scrolls = 0 if added else no_new_scrolls + 1

            at_bottom = position["y"] + position["viewport"] >= position["height"] - 20
            if at_bottom and page_num < max_pages:
                next_button = await self.page.query_selector('a[href*="page="]')
                if not next_button:
                    next_button = await self.page.query_selector('button[aria-label="Next"], .pagination-next')
                if next_button:
                    await next_button.click()
                    await self.random_delay(2, 4)
                    page_num += 1
                    added = await collect_visible_products()
                    no_new_scrolls = 0 if added else no_new_scrolls + 1
                    print(f"第{page_num}页已加载，共 {len(all_products)} 个产品")
                    continue
                if self.uses_cdp:
                    separator = '&' if '?' in self.page.url else '?'
                    next_url = f"{self.page.url}{separator}page={page_num + 1}"
                    if await self.open_cdp_page(next_url):
                        page_num += 1
                        added = await collect_visible_products()
                        no_new_scrolls = 0 if added else no_new_scrolls + 1
                        print(f"第{page_num}页已通过 CDP 加载，共 {len(all_products)} 个产品")
                        continue

            if no_new_scrolls >= 3:
                print("连续滚动未发现新商品，停止采集")
                break

        return all_products[:max_products] if max_products is not None else all_products

    async def scrape_product(self, url: str) -> Optional[Product]:
        """爬取单个产品"""
        if url in self.scraped_urls:
            print(f"跳过已爬取的产品: {url}")
            return None

        print(f"爬取产品: {url}")
        product = await self.extract_product_details_from_composer(url)

        if product:
            self.scraped_urls.add(url)

        return product

    async def scrape_multiple_products(self, urls: List[str], delay: float = None) -> List[Product]:
        """爬取多个产品"""
        if delay is None:
            delay = REQUEST_CONFIG["min_delay"]

        results = []

        for i, url in enumerate(urls):
            print(f"爬取产品 {i + 1}/{len(urls)}: {url}")

            product = await self.scrape_product(url)
            if product:
                results.append(product)

            # 随机延迟
            if i < len(urls) - 1:
                await self.random_delay(delay, delay + 2)

        return results


class OzonScraperCLI:
    """命令行界面"""

    def __init__(self):
        self.scraper = None

    async def run_search(
        self,
        query: str,
        max_pages: int = 3,
        max_products: int = None,
        headless: bool = True,
    ):
        """运行搜索"""
        self.scraper = AdvancedOzonScraper(headless=headless)

        try:
            await self.scraper.init_browser()

            # 搜索产品
            products = await self.scraper.search_products(query, max_pages, max_products)

            if self.scraper.last_error:
                print(f"抓取失败：{self.scraper.last_error}")
                print("未写入搜索结果文件，以免将已有数据覆盖为空列表。")
                return

            print(f"\n搜索结果: 找到 {len(products)} 个产品")

            # 保存搜索结果
            output_file = OUTPUT_CONFIG["search_results_file"]
            with open(output_file, "w", encoding=OUTPUT_CONFIG["encoding"]) as f:
                json.dump([asdict(p) for p in products], f, ensure_ascii=False, indent=2)

            print(f"搜索结果已保存到: {output_file}")

            # 询问是否爬取详情
            if products:
                try:
                    user_input = input(f"\n是否爬取前{min(5, len(products))}个产品的详情? (y/n): ")
                except EOFError:
                    user_input = "n"
                if user_input.lower() == 'y':
                    product_urls = [p.url for p in products[:5]]
                    details = await self.scraper.scrape_multiple_products(product_urls)

                    # 保存产品详情
                    details_file = OUTPUT_CONFIG["product_details_file"]
                    with open(details_file, "w", encoding=OUTPUT_CONFIG["encoding"]) as f:
                        json.dump([asdict(d) for d in details], f, ensure_ascii=False, indent=2)

                    print(f"产品详情已保存到: {details_file}")

        finally:
            await self.scraper.close()

    async def run_single_product(self, url: str, headless: bool = True):
        """爬取单个产品"""
        self.scraper = AdvancedOzonScraper(headless=headless)

        try:
            await self.scraper.init_browser()

            product = await self.scraper.scrape_product(url)

            if product:
                print(f"\n产品详情:")
                print(f"标题: {product.title}")
                print(f"价格: {product.price}")
                print(f"评分: {product.rating}")
                print(f"评论数: {product.reviews_count}")

                # 保存产品详情
                output_file = "single_product.json"
                with open(output_file, "w", encoding=OUTPUT_CONFIG["encoding"]) as f:
                    json.dump(asdict(product), f, ensure_ascii=False, indent=2)

                print(f"产品详情已保存到: {output_file}")
            else:
                print("无法提取产品详情")

        finally:
            await self.scraper.close()


async def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="Ozon.ru Scraper")
    parser.add_argument("mode", choices=["search", "product"], help="运行模式")
    parser.add_argument("query", help="搜索关键词或产品URL")
    parser.add_argument("--pages", type=int, default=3, help="搜索页数")
    parser.add_argument("--visible", action="store_true", help="显示浏览器窗口")

    args = parser.parse_args()

    cli = OzonScraperCLI()

    if args.mode == "search":
        await cli.run_search(args.query, args.pages, headless=not args.visible)
    elif args.mode == "product":
        await cli.run_single_product(args.query, headless=not args.visible)


if __name__ == "__main__":
    asyncio.run(main())
