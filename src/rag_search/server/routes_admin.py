"""Root redirect."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import RedirectResponse


async def _root(request: Request) -> RedirectResponse:
    return RedirectResponse("/dashboard")


def register(app) -> None:
    app.add_route("/", _root, methods=["GET"])
