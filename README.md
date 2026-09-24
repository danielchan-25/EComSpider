# EComSpider

EComSpider provides a FastAPI service for collecting public product data from Ozon and Wildberries. It stores tasks, products, reviews, events, and errors in a local SQLite database.

## Features

- Submit and monitor collection tasks for Ozon and Wildberries.
- Query collected products, reviews, task events, and errors through an authenticated API.
- Persist results in SQLite and write rotating application logs locally.
- Use a local Chrome instance through the Chrome DevTools Protocol (CDP) for Wildberries search.

## Requirements

- Python 3.10 or newer
- A Chrome instance with remote debugging enabled for Wildberries collection

Install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and set a long, random `API_BOOTSTRAP_TOKEN` before starting the service. Keep `.env` private; it is ignored by Git. Optional Feishu notification settings can be configured with `FEISHU_WEBHOOK_URL` and `FEISHU_SECRET`.

Start Chrome with a remote debugging port (default `9510`) before collecting Wildberries data. Configure `BROWSER_CDP_URL` and, if needed, `WILDBERRIES_CDP_URL` in `.env` to match your local setup.

## Run

```powershell
python run_api.py
```

The API listens on all network interfaces (`0.0.0.0:8000`) by default so other devices on the LAN can connect. Open `http://<server-lan-ip>:8000/docs` from a LAN device. Set `APP_HOST` and `APP_PORT` in `.env` to change the bind address and port. Windows Firewall must allow inbound TCP connections on the selected port.

## API usage

All `/api/*` routes require a token. The health check at `/health` does not. The example below submits a Wildberries collection task:

```powershell
$token = '<your API_BOOTSTRAP_TOKEN>'
$body = @{ keyword = 'cleaner'; max_products = 20 } | ConvertTo-Json
Invoke-RestMethod -Method Post `
  -Uri 'http://127.0.0.1:8000/api/tasks?platform=wildberries' `
  -ContentType 'application/json' `
  -Headers @{ Authorization = "Bearer $token" } `
  -Body $body
```

Use `platform=ozon` for Ozon. Available data endpoints include:

| Endpoint | Purpose |
| --- | --- |
| `/api/tasks` and `/api/tasks/all` | Current task status and task history |
| `/api/products` | Products collected by a task |
| `/api/reviews` | Reviews collected by a task |
| `/api/events` | Task progress events |
| `/api/errors` | Collection errors |

Data endpoints accept a `platform` and `task_id` query parameter. See `/docs` for request and response schemas.

## Data and privacy

The SQLite database and logs are created under `data/` and `logs/`. They are excluded from Git because they may contain collected data or operational details. Local virtual environments, bytecode, and `.env` files are also excluded.

Only collect public data in accordance with the applicable platform terms and laws. Respect rate limits and avoid collecting or publishing personal data.
