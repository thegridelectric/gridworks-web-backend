# gridworks-web-backend

REST API, realtime WebSocket gateway, and side-quest analysis scripts (WIP).

## Setup (uv)

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12. The editable `gridworks-flo` dependency lives at `../gridworks-innovations/gridworks-flo`.

```bash
cd ~/gridworks-web-backend
uv sync
cp template.env .env   # fill in credentials
```

## Running

```bash
uv run python -m api                         # REST API (or ./start_api.sh)
uv run python -m gateway                     # realtime gateway (or ./start_gateway.sh)
./start_all.sh                               # both in tmux
```

Both services read `BACKEND_*` variables from `.env` in the repo root.

If you use a SCADA `gw` shell alias that activates another venv, run `deactivate` first or prefix commands with `unset VIRTUAL_ENV` so `uv` uses this repo's `.venv` (not `gridworks-scada/.../venv`).
