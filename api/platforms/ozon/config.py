"""配置文件 - Ozon.ru 爬虫设置。"""

import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env", override=True)

# 代理设置
PROXY_CONFIG = {
    # 示例代理配置（需要替换为实际的代理）
    # "server": "http://proxy.example.com:8080",
    # "username": "proxy_user",
    # "password": "proxy_password",

    # 或者使用SOCKS5代理
    # "server": "socks5://proxy.example.com:1080",
    # "username": "proxy_user",
    # "password": "proxy_password",
}

# 连接已经启动、且已通过 Ozon 访问验证的本地 Chrome。
# 留空则由 Playwright 启动独立浏览器；当前环境使用 CDP 9510，不使用代理。
BROWSER_CONFIG = {
    "cdp_url": os.getenv("BROWSER_CDP_URL", "http://127.0.0.1:9510"),
}

# Ozon 的商品标题与索引主要使用俄语。为中文检索词保留可见的等价词，
# 以获得完整的站内商品结果。
SEARCH_QUERY_ALIASES = {
    "清洁剂": "средство для чистки",
}

# 请求设置
REQUEST_CONFIG = {
    "min_delay": 2.0,      # 最小延迟（秒）
    "max_delay": 5.0,      # 最大延迟（秒）
    "page_load_timeout": 30000,  # 页面加载超时（毫秒）
    "max_retries": 3,      # 最大重试次数
}

# 搜索设置
SEARCH_CONFIG = {
    "max_pages": 5,        # 最大翻页数
    "products_per_page": 36,  # 每页产品数（Ozon默认）
}

# 输出设置
OUTPUT_CONFIG = {
    "search_results_file": "search_results.json",
    "product_details_file": "product_details.json",
    "encoding": "utf-8",
}

# 用户代理列表
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

# 反检测设置
STEALTH_CONFIG = {
    "disable_web_security": True,
    "disable_dev_shm_usage": True,
    "no_sandbox": True,
    "disable_setuid_sandbox": True,
    "disable_infobars": True,
    "window_size": (1920, 1080),
}

# 日志设置
LOG_CONFIG = {
    "enable_logging": True,
    "log_file": "scraper.log",
    "log_level": "INFO",
}
