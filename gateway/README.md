# Realtime Gateway

Consumes SCADA telemetry from the GridWorks RabbitMQ broker over **AMQP** and
fans it out to dashboard WebSocket clients, keyed by house short alias.

```
SCADAs --MQTT plugin--> RabbitMQ (amq.topic)
                            |
                            | AMQP, binding key gw.#
                            v
                     realtime gateway ----wss /realtime/{alias}----> dashboards
```

**Read-only.** The gateway never publishes to the broker. SCADAs push
`snapshot.spaceheat` every ~30s and `layout.lite` on link activation; the
gateway caches the latest per house and pushes to clients on connect and on
arrival.

## WebSocket contract

- Endpoint: `/realtime/{short_alias}`, e.g. `/realtime/oak`
- Server sends `{"type": "status", ...}` then
  `{"type": "mqtt_message", "message_type": "snapshot.spaceheat", "payload": {...}}`
- Client messages `get_status` and `request_snapshot` are answered from the
  cache; everything else is ignored.

## Configuration

Read from the same `.env` as the visualizer API:

| Variable | Default | Meaning |
|----------|---------|---------|
| `VIS_RABBIT_URL` | `amqp://USERNAME:PASSWORD@HOST:5672/VHOST` | AMQP URL incl. vhost |
| `VIS_GATEWAY_PORT` | `8100` | HTTP/WebSocket listen port |

## Running

```bash
cd ~/gridworks-visualizer
python -m gateway          # or ./start_gateway.sh
```

Health: `GET http://localhost:8100/gateway/health`

## Deployment (visualizer EC2)

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
