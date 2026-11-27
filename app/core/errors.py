import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse


logger = logging.getLogger("app.errors")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        request_id = getattr(request.state, "request_id", "")
        logger.warning(
            "http_exception request_id=%s path=%s status=%s detail=%s",
            request_id,
            request.url.path,
            exc.status_code,
            exc.detail,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "ok": False,
                "error": {
                    "code": "http_error",
                    "message": str(exc.detail),
                },
                "request_id": request_id,
            },
            headers=getattr(exc, "headers", None),  # 保留 Retry-After 等头部
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", "")
        logger.exception(
            "unhandled_exception request_id=%s path=%s",
            request_id,
            request.url.path,
        )
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": {
                    "code": "internal_error",
                    "message": "服务器内部错误，请稍后重试",
                },
                "request_id": request_id,
            },
        )

