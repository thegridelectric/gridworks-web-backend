"""Both services import with the vendored snapshots they ship with.

The routers name the sema classes they read at import time, so a snapshot
regenerated without one of those words breaks the service at start, not
in any request test. Importing the two entry modules here fails the suite
in that case.
"""

import importlib


def test_api_imports() -> None:
    importlib.import_module("api.backend_api")


def test_gateway_imports() -> None:
    importlib.import_module("gateway.server")
