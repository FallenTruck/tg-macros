"""Minimal FastAPI/Mangum API surface for the Mini App foundation."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mangum import Mangum

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/api")
@app.get("/api/health")
async def health(request: Request) -> JSONResponse:
    return JSONResponse(
        content={
            "ok": True,
            "service": "tg-macros-api-foundation",
            "path": request.url.path,
            "telegram_init_data_present": bool(request.headers.get("x-telegram-init-data")),
        },
        headers={"cache-control": "no-store"},
    )


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def foundation_route(path: str) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"error": "foundation_route_not_implemented", "path": f"/api/{path}"},
        headers={"cache-control": "no-store"},
    )


handler = Mangum(app, lifespan="off")
