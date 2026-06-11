"""Entry point: uv run python -m api (from the repo root)."""


def main() -> None:
    from api.backend_api import WebBackendApi

    WebBackendApi().start()


if __name__ == "__main__":
    main()
