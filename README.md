# EComSpider

EComSpider 是一个基于 FastAPI 的电商商品数据采集服务，支持 Ozon 和 Wildberries。服务将任务、商品、评论、运行事件和错误信息保存在本地 SQLite 数据库中。

## 功能

- 提交并查看 Ozon、Wildberries 商品采集任务。
- 通过带令牌认证的 API 查询任务、商品、评论、事件和错误。
- 将采集结果保存到 SQLite，并在本地生成轮转日志。
- Wildberries 搜索通过 Chrome DevTools Protocol（CDP）使用本机 Chrome 浏览器。

## 环境要求

- Python 3.10 或更高版本
- 采集 Wildberries 时，需要启动启用远程调试的 Chrome 浏览器

## 安装

在项目根目录打开 PowerShell，创建虚拟环境并安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## 配置

将 `.env.example` 复制为 `.env`，并在启动前为 `API_BOOTSTRAP_TOKEN` 设置足够长的随机令牌。`.env` 已加入 Git 忽略规则，请勿公开或提交该文件。

飞书通知为可选功能，可通过 `FEISHU_WEBHOOK_URL` 和 `FEISHU_SECRET` 配置。

采集 Wildberries 前，请启动 Chrome 并开启远程调试端口，默认端口为 `9510`。如需使用其他地址，可在 `.env` 中配置 `BROWSER_CDP_URL`；也可以通过 `WILDBERRIES_CDP_URL` 单独配置 Wildberries 浏览器地址。

## 启动服务

安装依赖后，在项目根目录执行：

```powershell
python run_api.py
```

Windows 用户也可以双击运行 `start_api.bat`，服务会在当前窗口前台运行。按 `Ctrl+C` 停止服务。

服务默认监听 `0.0.0.0:8000`，因此本机和局域网内其他设备均可访问。浏览器打开 `http://127.0.0.1:8000/docs` 查看 API 文档；局域网设备请将 `127.0.0.1` 换为运行服务电脑的局域网 IP。Windows 防火墙需要允许 TCP 8000 入站。可在 `.env` 中通过 `APP_HOST` 和 `APP_PORT` 修改监听地址和端口。

## API 使用

除 `/health` 外，`/api/*` 接口均需要提供有效的 API 令牌。以下示例提交一个 Wildberries 采集任务：

```powershell
$token = '<你的 API_BOOTSTRAP_TOKEN>'
$body = @{ keyword = '清洁剂'; max_products = 20 } | ConvertTo-Json
Invoke-RestMethod -Method Post `
  -Uri 'http://127.0.0.1:8000/api/tasks?platform=wildberries' `
  -ContentType 'application/json' `
  -Headers @{ Authorization = "Bearer $token" } `
  -Body $body
```

将 `platform=wildberries` 改为 `platform=ozon` 即可提交 Ozon 任务。常用数据接口如下：

| 接口 | 说明 |
| --- | --- |
| `/api/tasks`、`/api/tasks/all` | 查询任务状态和历史记录 |
| `/api/products` | 查询任务采集到的商品 |
| `/api/reviews` | 查询任务采集到的评论 |
| `/api/events` | 查询任务进度事件 |
| `/api/errors` | 查询采集错误 |

数据接口需要提供 `platform` 和 `task_id` 查询参数。完整请求和响应格式请查看 `/docs`。

## 本地数据与隐私

SQLite 数据库和运行日志分别保存在 `data/`、`logs/` 目录中。这些目录、虚拟环境、Python 缓存和 `.env` 文件均已从 Git 中排除，避免将本地数据或配置推送到公开仓库。

请遵守目标平台的条款和适用法律，仅采集公开数据，并尊重访问频率限制；不要采集或公开个人数据。
