# gridworks-web-backend

REST API and realtime WebSocket gateway for the GridWorks web frontend.

```
Browser ----HTTPS----> nginx (visualizer.electricity.works)
                          |
            +-------------+-------------+
            |                           |
       REST /api/*                  WSS /realtime/{alias}
            |                           |
     gridworks-api              gridworks-gateway
     (port 8000)                 (port 8100)
            |                           |
       PostgreSQL                   RabbitMQ (AMQP)
```

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12. The editable `gridworks-flo` dependency lives at `../gridworks-innovations/gridworks-flo`.

```bash
cd ~/gridworks-web-backend
uv sync
cp template.env .env   # fill in credentials; chmod 600 .env
```

Both services read `BACKEND_*` variables from `.env` in the repo root. For systemd, use `KEY=value` lines (no spaces around `=`).

If a SCADA shell venv is active, run `deactivate` or `unset VIRTUAL_ENV` before `uv run` so this repo's `.venv` is used.

## Configuration

| Variable | Used by | Meaning |
|----------|---------|---------|
| `BACKEND_JOURNAL_DB_PASSWORD` | API | Journal DB password (`journaldb@journaldb.electricity.works`) |
| `BACKEND_BACKOFFICE_DB_PASSWORD` | API | Backoffice DB password (`backofficedb@backofficedb.electricity.works`) |
| `BACKEND_ACCESS_TOKEN_SECRET` | API | JWT signing key for login tokens |
| `BACKEND_RUNNING_LOCALLY` | API | `true` for local dev |
| `BACKEND_GOOGLE_MAPS_API_KEY` | API | Google Maps API key (optional) |
| `BACKEND_RABBIT_PASSWORD` | Gateway | RabbitMQ password (`smqPublic@hw1-1.electricity.works/hw1__1`) |
| `BACKEND_GATEWAY_PORT` | Gateway | Gateway listen port (default `8100`) |

See `template.env` for placeholders. Full field definitions are in `api/config.py` and `gateway/config.py`.

## Realtime gateway

Consumes SCADA telemetry from RabbitMQ (`amq.topic`, binding `gw.#`) and fans it out to dashboard WebSocket clients by house short alias. **Read-only** — never publishes to the broker.

**WebSocket contract**

- Endpoint: `/realtime/{short_alias}` (e.g. `/realtime/oak`)
- Server → client: `{"type": "status", ...}` then `{"type": "mqtt_message", "message_type": "snapshot.spaceheat", "payload": {...}}`
- Client → server: `get_status` and `request_snapshot` are answered from cache; everything else is ignored

**Health:** `GET http://localhost:8100/gateway/health`

## Local development

```bash
uv run python -m api       # REST API on :8000
uv run python -m gateway   # gateway on :8100
```

## Production (EC2 systemd)

Install units that start on boot and restart on failure:

```bash
cd ~/gridworks-web-backend
uv sync
cp template.env .env   # if not already present
chmod 600 .env
./deploy/install-services.sh

sudo systemctl start gridworks-api gridworks-gateway
sudo systemctl status gridworks-api gridworks-gateway
```

**Logs**

```bash
journalctl -u gridworks-api -f
journalctl -u gridworks-gateway -f
```

**Deploy updates**

```bash
cd ~/gridworks-web-backend
git pull
uv sync
sudo systemctl restart gridworks-api gridworks-gateway
```

## nginx

Proxy WebSocket traffic to the gateway (API routing is unchanged):

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
