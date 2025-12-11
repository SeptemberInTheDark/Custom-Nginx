"""
Тестовый echo-сервер для проверки proxy.

Запуск:
    uvicorn tests.echo_app:app --host 127.0.0.1 --port 9001 --workers 1
    uvicorn tests.echo_app:app --host 127.0.0.1 --port 9002 --workers 1
"""
import asyncio

from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.requests import Request


async def homepage(request: Request) -> JSONResponse:
    """Главная страница с информацией о запросе."""
    return JSONResponse({
        "message": "Hello from echo server",
        "path": str(request.url.path),
        "method": request.method,
        "headers": dict(request.headers),
    })


async def echo(request: Request) -> PlainTextResponse:
    """Эхо-эндпоинт — возвращает тело запроса."""
    body = await request.body()
    return PlainTextResponse(body)


async def slow(request: Request) -> JSONResponse:
    """Медленный эндпоинт для тестирования таймаутов."""
    delay = float(request.query_params.get("delay", 5))
    await asyncio.sleep(delay)
    return JSONResponse({"delayed": delay})


async def status(request: Request) -> JSONResponse:
    """Возвращает указанный HTTP статус."""
    code = int(request.query_params.get("code", 200))
    return JSONResponse({"status": code}, status_code=code)


async def large(request: Request) -> PlainTextResponse:
    """Возвращает большой ответ для тестирования стриминга."""
    size = int(request.query_params.get("size", 1024 * 1024))  # 1MB default
    return PlainTextResponse("x" * size)


app = Starlette(
    routes=[
        Route("/", homepage),
        Route("/echo", echo, methods=["POST"]),
        Route("/slow", slow),
        Route("/status", status),
        Route("/large", large),
    ]
)
