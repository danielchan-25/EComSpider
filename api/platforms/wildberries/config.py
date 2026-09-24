"""Wildberries.ru 采集配置。

采集路线:
  1. 通过 CDP 复用真实 Chrome(带真实指纹与 JS 环境);
  2. 首页加载后 JS 自动生成 x_wbaas_token cookie(WaaS 反爬令牌);
  3. 在页面上下文内 fetch __internal/u-search 搜索 API,
     携带 deviceid / x-requested-with / x-spa-version / x-queryid 请求头。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env", override=True)

# 复用 Ozon 同一套 CDP 浏览器;可用 WILDBERRIES_CDP_URL 单独覆盖。
BROWSER_CONFIG = {
    "cdp_url": os.getenv("WILDBERRIES_CDP_URL", os.getenv("BROWSER_CDP_URL", "http://127.0.0.1:9510")),
    "homepage": "https://www.wildberries.ru/",
}

# 静态请求参数(与站点 SPA 自身请求一致,2026-09 实测有效)。
SEARCH_CONFIG = {
    "base": "https://www.wildberries.ru/__internal/u-search/exactmatch/ru/common/v18/search",
    "app_type": 1,
    "curr": "rub",
    "dest": 1259570991,          # 地域 ID,站点按访问 IP 下发
    "lang": "ru",
    "locale": "ru",
    "page_size": 100,            # 每页商品数
    "max_pages": 10,             # 最大翻页数上限
    "sort": "popular",           # popular / rate / priceup / pricedown / newly
}

# SPA 版本号;站点前端资源随版本更新,搜索引擎校验其一致性。
SPA_VERSION = os.getenv("WILDBERRIES_SPA_VERSION", "14.24.3")

# 请求节奏(转速限制,429 后请调大)
REQUEST_CONFIG = {
    "page_gap_min": 2.5,         # 分页间最小间隔(秒)
    "page_gap_max": 4.0,         # 分页间最大间隔(秒)
    "token_wait_seconds": 20,    # 等待 x_wbaas_token 最长秒数
    "page_load_timeout_ms": 45000,
    "max_retries": 3,            # 403/429 后重试次数
    "retry_backoff_seconds": 15,
    "detail_concurrency": 10,
    "card_timeout_seconds": 10,
    "shard_probe_workers": 12,
}

# 商品 card.json 位于公开静态 CDN，不需要浏览器会话。分片由 vol 稳定决定，
# 运行期间会被采集器缓存，避免同一 vol 的商品重复探测。
CARD_CONFIG = {
    "cdn_template": "https://basket-{shard:02d}.wbbasket.ru",
    "shard_start": 1,
    "shard_end": 50,
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
    ),
}

# 中文检索词 → 俄语等价词(WB 索引以俄语为主)
SEARCH_QUERY_ALIASES = {
    "手机": "телефон",
    "清洁剂": "средство для чистки",
    "耳机": "наушники",
    "连衣裙": "платье",
    "运动鞋": "кроссовки",
    "电脑": "компьютер",
}
