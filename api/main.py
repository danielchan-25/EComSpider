from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import base64
import hashlib
import hmac
import time
import urllib.request
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from api.platforms.ozon.adapter import OzonAdapter
from api.platforms.wildberries.adapter import WildberriesAdapter


ROOT = Path(__file__).resolve().parent.parent
# The service configuration belongs to the workspace root.  Override an
# accidentally exported empty variable so a configured bootstrap token is
# reliably initialized on first startup.
load_dotenv(ROOT / ".env", override=True)
DATABASE_PATH = ROOT / "data" / "ozon.sqlite3"
LOG_PATH = ROOT / "logs" / "ozon.log"
WILDBERRIES_LOG_PATH = ROOT / "logs" / "wildberries.log"
QUEUE_MAX_SIZE = int(os.getenv("TASK_QUEUE_MAX_SIZE", "20"))
SUBMIT_INTERVAL = int(os.getenv("TASK_SUBMIT_INTERVAL_SECONDS", "60"))
TASK_TIMEOUT = int(os.getenv("TASK_TIMEOUT_SECONDS", "1800"))
BOOTSTRAP_TOKEN = os.getenv("API_BOOTSTRAP_TOKEN", "")
FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")
FEISHU_SECRET = os.getenv("FEISHU_SECRET", "")

logger = logging.getLogger("ozon_api")
wildberries_logger = logging.getLogger("ozon_api.wildberries")


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, platform TEXT NOT NULL, keyword TEXT NOT NULL, status TEXT NOT NULL,
 queue_position INTEGER, max_products INTEGER, detail_concurrency INTEGER,
 wait_min_seconds REAL, wait_max_seconds REAL, include_reviews INTEGER,
 include_recommendations INTEGER, progress_total INTEGER DEFAULT 0,
 progress_done INTEGER DEFAULT 0, success_count INTEGER DEFAULT 0, failed_count INTEGER DEFAULT 0,
 error_code TEXT, error_message TEXT, created_at TEXT NOT NULL, queued_at TEXT,
 started_at TEXT, finished_at TEXT, elapsed_seconds REAL, config_json TEXT
);
CREATE TABLE IF NOT EXISTS products (
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, platform TEXT NOT NULL,
 platform_product_id TEXT, keyword TEXT, title TEXT, brand TEXT, price REAL,
 original_price REAL, currency TEXT, discount TEXT, rating REAL, review_count INTEGER,
 sales_count INTEGER, seller_id TEXT, seller_name TEXT, seller_rating REAL,
 product_url TEXT, main_image_url TEXT, description TEXT, category_path TEXT,
 specs_json TEXT, images_json TEXT, raw_jsonld TEXT, raw_data_json TEXT, crawled_at TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, platform TEXT NOT NULL,
 platform_product_id TEXT, platform_review_id TEXT, user_name TEXT, user_id TEXT,
 rating REAL, content TEXT, review_time TEXT, like_count INTEGER, dislike_count INTEGER,
 variant_info TEXT, images_json TEXT, raw_data_json TEXT, crawled_at TEXT
);
CREATE TABLE IF NOT EXISTS task_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, platform TEXT NOT NULL,
 event_type TEXT NOT NULL, message TEXT, payload_json TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crawl_errors (
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, platform TEXT NOT NULL,
 platform_product_id TEXT, stage TEXT, error_code TEXT, error_message TEXT,
 traceback TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_limits (
 client_key TEXT NOT NULL, platform TEXT NOT NULL, last_submit_at TEXT NOT NULL,
 PRIMARY KEY(client_key, platform)
);
CREATE TABLE IF NOT EXISTS api_tokens (
 token TEXT PRIMARY KEY, name TEXT, is_active INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_platform_status ON tasks(platform, status);
CREATE INDEX IF NOT EXISTS idx_products_task_platform ON products(task_id, platform);
CREATE INDEX IF NOT EXISTS idx_reviews_task_platform ON reviews(task_id, platform);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def configure_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    def configure_file_logger(target: logging.Logger, path: Path) -> None:
        if target.handlers:
            return
        target.setLevel(logging.INFO)
        handler = RotatingFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        target.addHandler(handler)

    configure_file_logger(logger, LOG_PATH)
    configure_file_logger(wildberries_logger, WILDBERRIES_LOG_PATH)
    # Wildberries 采集器写入独立日志，避免与 Ozon/API 通用日志重复。
    wildberries_logger.propagate = False


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_database() -> None:
    with connect() as connection:
        connection.executescript(SCHEMA)
        if BOOTSTRAP_TOKEN:
            connection.execute(
                "INSERT OR IGNORE INTO api_tokens(token, name, created_at) VALUES (?, ?, ?)",
                (BOOTSTRAP_TOKEN, "bootstrap", now()),
            )


def record_event(task_id: str, kind: str, message: str, payload: Any | None = None, platform: str = "ozon") -> None:
    with connect() as connection:
        connection.execute(
            "INSERT INTO task_events(task_id, platform, event_type, message, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, platform, kind, message, json.dumps(payload, ensure_ascii=False) if payload is not None else None, now()),
        )


def send_feishu_text(text: str) -> tuple[bool, str]:
    if not FEISHU_WEBHOOK_URL:
        return False, "FEISHU_WEBHOOK_URL is not configured"
    payload: dict[str, Any] = {"msg_type": "text", "content": {"text": text}}
    if FEISHU_SECRET:
        timestamp = str(int(time.time()))
        signature = base64.b64encode(
            hmac.new(
                f"{timestamp}\n{FEISHU_SECRET}".encode("utf-8"), b"", hashlib.sha256
            ).digest()
        ).decode("utf-8")
        payload.update({"timestamp": timestamp, "sign": signature})
    request = urllib.request.Request(
        FEISHU_WEBHOOK_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            return True, reply.read().decode("utf-8", errors="replace")
    except OSError as exc:
        logger.warning("飞书失败通知发送失败: %s", exc)
        return False, str(exc)


class TaskPayload(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"keyword": "清洁剂", "max_products": 20},
                {"keyword": "телефон", "max_products": 10},
            ]
        },
    )
    keyword: str = Field(min_length=1, max_length=200, description="搜索关键词，长度 1–200。")
    max_products: int = Field(default=20, ge=1, le=100, description="最多采集的商品数量，范围 1–100。")


def response(*, success: bool, message: str, platform: str | None = None, task_id: str | None = None,
             status: str | None = None, data: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "success": success, "platform": platform, "task_id": task_id, "status": status,
        "message": message, "data": data, "error": error,
        "meta": {"request_id": f"req_{uuid4().hex}", "timestamp": now()},
    }


def error(message: str, code: str, *, task_id: str | None = None, platform: str = "ozon") -> dict[str, Any]:
    return response(success=False, message=message, platform=platform, task_id=task_id,
                    error={"code": code, "message": message, "detail": None})


def require_token(authorization: str | None = Header(default=None), x_api_token: str | None = Header(default=None)) -> None:
    token = x_api_token or (authorization.partition(" ")[2].strip() if authorization and authorization.lower().startswith("bearer ") else "")
    with connect() as connection:
        row = connection.execute("SELECT token FROM api_tokens WHERE token = ? AND is_active = 1", (token,)).fetchone()
        if row:
            connection.execute("UPDATE api_tokens SET last_used_at = ? WHERE token = ?", (now(), token))
    if not token or not row:
        raise HTTPException(status_code=401, detail=error("API token 验证失败", "UNAUTHORIZED"))


class TaskManager:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
        self.worker: asyncio.Task | None = None
        self.active_task_id: str | None = None

    def start(self) -> None:
        if not self.worker or self.worker.done():
            with connect() as connection:
                stale_rows = connection.execute("SELECT id, platform FROM tasks WHERE status IN ('running_search', 'running_detail')").fetchall()
                stamp = now()
                for row in stale_rows:
                    platform = row["platform"]
                    connection.execute("UPDATE tasks SET status='failed', error_code='TASK_INTERRUPTED', error_message='服务停止或重启导致任务中断', finished_at=? WHERE id=?", (stamp, row["id"]))
                    connection.execute("INSERT INTO crawl_errors(task_id, platform, stage, error_code, error_message, created_at) VALUES (?, ?, 'lifecycle', 'TASK_INTERRUPTED', '服务停止或重启导致任务中断', ?)", (row["id"], platform, stamp))
                    connection.execute("INSERT INTO task_events(task_id, platform, event_type, message, payload_json, created_at) VALUES (?, ?, 'task_interrupted', '服务启动时清理遗留运行任务', NULL, ?)", (row["id"], platform, stamp))
                rows = connection.execute("SELECT id FROM tasks WHERE status = 'queued' ORDER BY queued_at LIMIT ?", (QUEUE_MAX_SIZE,)).fetchall()
            if stale_rows:
                logger.warning("task_recovery_interrupted_count=%s", len(stale_rows))
            for row in rows:
                self.queue.put_nowait(row["id"])
            self.worker = asyncio.create_task(self._work())

    async def stop(self) -> None:
        active_task_id = self.active_task_id
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
        if active_task_id:
            await self._mark_interrupted(active_task_id, "服务正常停止导致任务中断")

    async def submit(self, payload: TaskPayload, client_key: str, platform: str = "ozon") -> tuple[str, int]:
        with connect() as connection:
            last = connection.execute("SELECT last_submit_at FROM rate_limits WHERE client_key = ? AND platform = ?", (client_key, platform)).fetchone()
            if last and (datetime.now(timezone.utc) - datetime.fromisoformat(last["last_submit_at"])).total_seconds() < SUBMIT_INTERVAL:
                raise ValueError("RATE_LIMITED")
            if self.queue.full():
                raise ValueError("QUEUE_FULL")
            prefix = "ozon" if platform == "ozon" else "wb"
            task_id = f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"
            position = self.queue.qsize() + 1
            stamp = now()
            connection.execute("""INSERT INTO tasks(id, platform, keyword, status, queue_position, max_products, detail_concurrency, wait_min_seconds, wait_max_seconds, include_reviews, include_recommendations, created_at, queued_at, config_json) VALUES (?, ?, ?, 'queued', ?, ?, 1, 2, 5, 1, 0, ?, ?, ?)""", (task_id, platform, payload.keyword, position, payload.max_products, stamp, stamp, payload.model_dump_json()))
            connection.execute("INSERT INTO rate_limits(client_key, platform, last_submit_at) VALUES (?, ?, ?) ON CONFLICT(client_key, platform) DO UPDATE SET last_submit_at=excluded.last_submit_at", (client_key, platform, stamp))
        record_event(task_id, "task_queued", "任务已进入队列", {"queue_position": position}, platform)
        await self.queue.put(task_id)
        return task_id, position

    async def _work(self) -> None:
        while True:
            task_id = await self.queue.get()
            self.active_task_id = task_id
            try:
                await asyncio.wait_for(self._run(task_id), timeout=TASK_TIMEOUT)
            except Exception as exc:
                logger.exception("任务失败: %s", task_id)
                await self._fail(task_id, "TASK_FAILED", str(exc))
            finally:
                self.active_task_id = None
                self.queue.task_done()

    async def _mark_interrupted(self, task_id: str, message: str) -> None:
        with connect() as connection:
            task = connection.execute("SELECT platform FROM tasks WHERE id = ?", (task_id,)).fetchone()
            platform = task["platform"] if task else "ozon"
            updated = connection.execute("UPDATE tasks SET status='failed', error_code='TASK_INTERRUPTED', error_message=?, finished_at=? WHERE id=? AND status IN ('running_search', 'running_detail')", (message, now(), task_id)).rowcount
            if updated:
                connection.execute("INSERT INTO crawl_errors(task_id, platform, stage, error_code, error_message, created_at) VALUES (?, ?, 'lifecycle', 'TASK_INTERRUPTED', ?, ?)", (task_id, platform, message, now()))
        if updated:
            record_event(task_id, "task_interrupted", message, {"code": "TASK_INTERRUPTED"}, platform)
            logger.warning("task_interrupted task_id=%s reason=%s", task_id, message)

    async def _run(self, task_id: str) -> None:
        started_at = now()
        with connect() as connection:
            task = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not task:
                return
            connection.execute("UPDATE tasks SET status='running_search', started_at=? WHERE id=?", (started_at, task_id))
        platform = task["platform"]
        record_event(task_id, "search_started", f"开始使用 CDP 采集 {platform}", {"keyword": task["keyword"]}, platform)
        adapters = {"ozon": OzonAdapter, "wildberries": WildberriesAdapter}
        products, reviews = await adapters[platform]().collect(task["keyword"], task["max_products"])
        with connect() as connection:
            connection.execute("UPDATE tasks SET status='running_detail', progress_total=? WHERE id=?", (len(products), task_id))
            for index, product in enumerate(products, start=1):
                fields = ["task_id", "platform", *product.keys()]
                values = [task_id, platform, *product.values()]
                connection.execute(f"INSERT INTO products({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", values)
                connection.execute("UPDATE tasks SET progress_done=?, success_count=? WHERE id=?", (index, index, task_id))
            for review in reviews:
                fields = ["task_id", "platform", *review.keys()]
                connection.execute(f"INSERT INTO reviews({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", [task_id, platform, *review.values()])
            finished = now()
            elapsed = (datetime.fromisoformat(finished) - datetime.fromisoformat(started_at)).total_seconds()
            connection.execute("UPDATE tasks SET status='success', finished_at=?, elapsed_seconds=? WHERE id=?", (finished, elapsed, task_id))
        record_event(task_id, "task_success", "任务执行完成", {"products": len(products), "reviews": len(reviews)}, platform)

    async def _fail(self, task_id: str, code: str, message: str) -> None:
        with connect() as connection:
            task = connection.execute("SELECT keyword, platform FROM tasks WHERE id=?", (task_id,)).fetchone()
            platform = task["platform"] if task else "ozon"
            connection.execute("UPDATE tasks SET status='failed', error_code=?, error_message=?, finished_at=? WHERE id=?", (code, message, now(), task_id))
            connection.execute("INSERT INTO crawl_errors(task_id, platform, stage, error_code, error_message, created_at) VALUES (?, ?, 'task', ?, ?, ?)", (task_id, platform, code, message, now()))
        record_event(task_id, "task_failed", "任务失败", {"code": code, "message": message}, platform)
        text = (
            f"{platform.upper()} 采集任务失败\n"
            f"任务ID: {task_id}\n关键词: {task['keyword'] if task else '未知'}\n"
            f"错误码: {code}\n错误信息: {message[:2000]}"
        )
        sent, result = await asyncio.to_thread(send_feishu_text, text)
        record_event(task_id, "task_failure_notification", "任务失败通知已处理", {"sent": sent, "result": result}, platform)


tasks = TaskManager()


def task_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["include_reviews"] = bool(data["include_reviews"])
    data["include_recommendations"] = bool(data["include_recommendations"])
    data["config"] = json.loads(data.pop("config_json")) if data.get("config_json") else None
    return data


def client_key(request: Request) -> str:
    return request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown").split(",")[0].strip()


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    init_database()
    tasks.start()
    yield
    await tasks.stop()


app = FastAPI(
    title="多平台商品采集 API",
    version="0.2.0",
    docs_url="/docs",
    lifespan=lifespan,
    description="""
通过异步任务采集电商商品数据的本地 API。

## 当前支持的平台

| `platform` | 平台 | 任务 ID 前缀 |
| --- | --- | --- |
| `ozon` | Ozon | `ozon_` |
| `wildberries` | Wildberries | `wb_` |

所有 `/api/*` 接口均需在请求头提供活跃 Token，支持两种写法：

```http
X-API-Token: <token>
```

```http
Authorization: Bearer <token>
```

## 常用流程

1. `POST /api/tasks?platform=wildberries` 创建任务。
2. 轮询 `GET /api/tasks?platform=wildberries&task_id=<任务 ID>`，直到状态为 `success` 或 `failed`。
3. 通过 products、reviews、events、errors 接口读取结果和执行轨迹。

除 `/health` 外，业务响应均包含 `success`、`platform`、`task_id`、`status`、`message`、`data`、`error` 和 `meta`。
""",
    openapi_tags=[
        {"name": "系统", "description": "服务健康状态。"},
        {"name": "任务", "description": "创建、查询和轮询异步采集任务。"},
        {"name": "数据", "description": "读取已完成任务的商品、评论、事件和错误。"},
    ],
)


@app.middleware("http")
async def request_log(request: Request, call_next):
    started = time.perf_counter()
    logger.info("api_request method=%s path=%s query=%s client=%s", request.method, request.url.path, request.url.query, client_key(request))
    try:
        reply = await call_next(request)
    except Exception:
        logger.exception("api_request_failed method=%s path=%s", request.method, request.url.path)
        raise
    logger.info("api_response method=%s path=%s status=%s elapsed_seconds=%.3f", request.method, request.url.path, reply.status_code, time.perf_counter() - started)
    return reply


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.detail if isinstance(exc.detail, dict) else error(str(exc.detail), "HTTP_ERROR"))


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    message = "请求参数或请求体校验失败"
    return JSONResponse(
        status_code=422,
        content=response(
            success=False,
            message=message,
            platform="ozon",
            error={"code": "VALIDATION_ERROR", "message": message, "detail": jsonable_encoder(exc.errors())},
        ),
    )

@app.get("/health", tags=["系统"], summary="健康检查", description="无需 Token。返回 `status=ok` 表示 API 服务可用。")
async def health() -> dict[str, str]:
    return {"status": "ok"}


SUPPORTED_PLATFORMS = {"ozon", "wildberries"}


def ensure_platform(platform: str | None) -> None:
    if platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=400, detail=error("仅支持 platform=ozon / wildberries", "UNSUPPORTED_PLATFORM", platform=platform or ""))


@app.post(
    "/api/tasks",
    dependencies=[Depends(require_token)],
    tags=["任务"],
    summary="创建采集任务",
    description="创建后立即返回 `task_id` 和 `queued` 状态；请使用任务查询接口轮询执行结果。",
)
async def create_task(
    request: Request,
    payload: TaskPayload = Body(description="采集任务参数。"),
    platform: str = Query(..., description="目标平台：`ozon` 或 `wildberries`。", examples=["wildberries", "ozon"]),
) -> dict[str, Any]:
    ensure_platform(platform)
    try:
        task_id, position = await tasks.submit(payload, client_key(request), platform)
    except ValueError as exc:
        code = str(exc)
        return error("任务队列已满，请稍后再试" if code == "QUEUE_FULL" else "任务提交过于频繁，请稍后再试", code, platform=platform)
    return response(success=True, message="任务已提交，正在排队", platform=platform, task_id=task_id, status="queued", data={"queue_position": position, "queue_size": tasks.queue.qsize(), "keyword": payload.keyword})


@app.get("/api/tasks", dependencies=[Depends(require_token)], tags=["任务"], summary="查询任务或任务列表")
async def get_tasks(
    platform: str = Query(..., description="目标平台：`ozon` 或 `wildberries`。", examples=["wildberries", "ozon"]),
    task_id: str | None = Query(None, description="任务 ID；提供时返回单个任务的状态与进度。", examples=["wb_20260911093025_238ba5d0"]),
    limit: int = Query(50, ge=1, le=200, description="列表最多返回的任务数。"),
) -> dict[str, Any]:
    ensure_platform(platform)
    with connect() as connection:
        if task_id:
            row = connection.execute("SELECT * FROM tasks WHERE id=? AND platform=?", (task_id, platform)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=error("任务不存在", "TASK_NOT_FOUND", task_id=task_id, platform=platform))
            task = task_row(row)
            total, done = task["progress_total"] or 0, task["progress_done"] or 0
            data = {"task": task, "progress": {"total": total, "done": done, "success": task["success_count"], "failed": task["failed_count"], "percent": round(done / total * 100, 2) if total else 0}, "poll_after_seconds": 10 if task["status"] not in {"success", "failed"} else None}
            return response(success=task["status"] != "failed", message="任务完成" if task["status"] == "success" else "任务状态", platform=platform, task_id=task_id, status=task["status"], data=data, error={"code": task["error_code"], "message": task["error_message"], "detail": None} if task["status"] == "failed" else None)
        rows = connection.execute("SELECT * FROM tasks WHERE platform=? ORDER BY created_at DESC LIMIT ?", (platform, limit)).fetchall()
    return response(success=True, message="任务列表", platform=platform, data={"tasks": [task_row(row) for row in rows], "count": len(rows)})


@app.get("/api/tasks/all", dependencies=[Depends(require_token)], tags=["任务"], summary="查询任务历史")
async def get_all_tasks(
    platform: str = Query(..., description="目标平台：`ozon` 或 `wildberries`。", examples=["wildberries", "ozon"]),
    limit: int = Query(200, ge=1, le=1000, description="最多返回的历史任务数。"),
) -> dict[str, Any]:
    return await get_tasks(platform, None, limit)


def task_data(table: str, platform: str, task_id: str, json_columns: tuple[str, ...]) -> dict[str, Any]:
    ensure_platform(platform)
    with connect() as connection:
        exists = connection.execute("SELECT 1 FROM tasks WHERE id=? AND platform=?", (task_id, platform)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail=error("任务不存在", "TASK_NOT_FOUND", task_id=task_id, platform=platform))
        rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table} WHERE task_id=? AND platform=? ORDER BY id", (task_id, platform)).fetchall()]
    for row in rows:
        for column in json_columns:
            row[column.removesuffix("_json")] = json.loads(row.pop(column)) if row.get(column) else None
    label = {"products": "商品列表", "reviews": "评论列表", "task_events": "任务事件", "crawl_errors": "任务错误"}[table]
    key = {"products": "products", "reviews": "reviews", "task_events": "events", "crawl_errors": "errors"}[table]
    return response(success=True, message=label, platform=platform, task_id=task_id, data={key: rows, "count": len(rows)})


@app.get("/api/products", dependencies=[Depends(require_token)], tags=["数据"], summary="查询任务商品")
async def products(
    platform: str = Query(..., description="任务所属平台。", examples=["wildberries", "ozon"]),
    task_id: str = Query(..., description="已完成或执行中的任务 ID。", examples=["wb_20260911093025_238ba5d0"]),
) -> dict[str, Any]:
    return task_data("products", platform, task_id, ("specs_json", "images_json", "raw_jsonld", "raw_data_json"))


@app.get("/api/reviews", dependencies=[Depends(require_token)], tags=["数据"], summary="查询任务评论")
async def reviews(
    platform: str = Query(..., description="任务所属平台。", examples=["wildberries", "ozon"]),
    task_id: str = Query(..., description="任务 ID。", examples=["wb_20260911093025_238ba5d0"]),
) -> dict[str, Any]:
    return task_data("reviews", platform, task_id, ("images_json", "raw_data_json"))


@app.get("/api/events", dependencies=[Depends(require_token)], tags=["数据"], summary="查询任务事件")
async def events(
    platform: str = Query(..., description="任务所属平台。", examples=["wildberries", "ozon"]),
    task_id: str = Query(..., description="任务 ID。", examples=["wb_20260911093025_238ba5d0"]),
) -> dict[str, Any]:
    return task_data("task_events", platform, task_id, ("payload_json",))


@app.get("/api/errors", dependencies=[Depends(require_token)], tags=["数据"], summary="查询任务错误")
async def errors(
    platform: str = Query(..., description="任务所属平台。", examples=["wildberries", "ozon"]),
    task_id: str = Query(..., description="任务 ID。", examples=["wb_20260911093025_238ba5d0"]),
) -> dict[str, Any]:
    return task_data("crawl_errors", platform, task_id, ())
