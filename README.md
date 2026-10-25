# GridWorks Web Back-End

This repo contains the backend services for the [GridWorks Web Front-End](https://github.com/thegridelectric/gridworks-web-frontend), which include:
- A REST API for serving the web frontend (login, data visualization, etc.), running on port 8000 and reading data from PostgreSQL databases
- A Realtime WebSocket gateway for distributing SCADA telemetry to the web frontend's real time dashboards, running on port 8100 and reading the fleet stream from the analytics tap broker over AMQPS

## Setup

```bash
cd ~/gridworks-web-backend
uv sync
cp template.env .env   
chmod 600 .env
nano .env
```

### Fill in credentials

Both services read `BACKEND_*` variables from `.env`. Use `KEY=value` lines (no spaces around `=`).

| Variable | Used by | Meaning |
|----------|---------|---------|
| `BACKEND_JOURNAL_DB_PASSWORD` | API | `journaldb` user password |
| `BACKEND_BACKOFFICE_DB_PASSWORD` | API | `backofficedb` user password |
| `BACKEND_TSDB_URL` | API, Gateway | Read-only `postgresql+asyncpg://` URL of journaldb; the gateway reads the layouts table through it once at start |
| `BACKEND_ACCESS_TOKEN_SECRET` | API | JWT signing key for login tokens (input any string) |
| `BACKEND_RABBIT_USER` | Gateway | Login on the analytics tap broker (`web.gateway`) |
| `BACKEND_RABBIT_PASSWORD` | Gateway | That login's password |
| `BACKEND_RABBIT_HOST` | Gateway | Tap broker host (`hw1-tap.electricity.works`); the connection is AMQPS |
| `BACKEND_RABBIT_PORT` | Gateway | Tap broker AMQPS port (`5671`) |
| `BACKEND_RABBIT_VHOST` | Gateway | Broker vhost (`hw1__1`) |
| `BACKEND_RUNNING_LOCALLY` | API | `true` for local dev |
| `BACKEND_ALERT_MANAGER_URL` | API | Base URL of alert-manager (e.g. `http://localhost:8000`) |
| `BACKEND_ALERT_MANAGER_TOKEN` | API | Bearer token for alert-manager `/alerts-history` |
| `BACKEND_PRICE_FORECAST_URL` | API | The stopgap price service's latest-forecast route, forwarded by `/api/v2/price-forecast/latest` |

### Running as services (best for production on EC2):

```bash
chmod +x deploy/install-services.sh
./deploy/install-services.sh
sudo systemctl start gridworks-api gridworks-gateway
```

**Logs**
```bash
journalctl -u gridworks-api -f
journalctl -u gridworks-gateway -f
```

**Updates**
```bash
cd ~/gridworks-web-backend
git pull
uv sync
sudo systemctl restart gridworks-api gridworks-gateway
```

### Running as standalone processes (best for local development):

```bash
uv run python -m api       # REST API on port 8000
uv run python -m gateway   # gateway server on port 8100
```

## Tests

```bash
uv sync --group dev
uv run pytest
```

The suite stands up a throwaway TimescaleDB in Docker (testcontainers) and
self-skips when it cannot; `WEB_BACKEND_TEST_PG_URL` points it at a running
database instead.

## nginx

On the EC2 instance, configure nginx to proxy WebSocket traffic to the gateway:

```bash
sudo nano /etc/nginx/sites-available/default
```

Add the following to the `location` block for the API:

```nginx
location ~ ^/realtime/(?<alias>[a-z0-9]+)$ {
    proxy_pass http://127.0.0.1:8100;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 86400;
    proxy_send_timeout 86400;
    proxy_buffering off;
}
```

```bash
sudo systemctl restart nginx
```


