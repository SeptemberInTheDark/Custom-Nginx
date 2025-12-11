"""
Тестовый echo-сервер.

Запускается как upstream для проверки прокси:
    uvicorn tests.echo_app:app --host 127.0.0.1 --port 9001
    uvicorn tests.echo_app:app --host 127.0.0.1 --port 9002
"""
import asyncio

from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.requests import Request


async def homepage(request: Request) -> JSONResponse:
    """
    GET / — возвращает информацию о запросе.
    
    Удобно для проверки что прокси пробрасывает заголовки.
    """
    return JSONResponse({
        "message": "Hello from echo server",
        "path": str(request.url.path),
        "method": request.method,
        "headers": dict(request.headers),
    })


async def echo(request: Request) -> PlainTextResponse:
    """
    POST /echo — возвращает тело запроса.
    
    curl -X POST http://localhost:8080/echo -d "hello"
    """
    body = await request.body()
    return PlainTextResponse(body)


async def slow(request: Request) -> JSONResponse:
    """
    GET /slow?delay=5 — отвечает с задержкой.
    
    Для тестирования таймаутов.
    """
    delay = float(request.query_params.get("delay", 5))
    await asyncio.sleep(delay)
    return JSONResponse({"delayed": delay})


async def status(request: Request) -> JSONResponse:
    """
    GET /status?code=404 — возвращает указанный HTTP-код.
    
    Для тестирования обработки разных статусов.
    """
    code = int(request.query_params.get("code", 200))
    return JSONResponse({"status": code}, status_code=code)


async def large(request: Request) -> PlainTextResponse:
    """
    GET /large?size=1048576 — большой ответ.
    
    Для тестирования стриминга. По умолчанию 1MB.
    """
    size = int(request.query_params.get("size", 1024 * 1024))
    return PlainTextResponse("x" * size)


# маршруты
app = Starlette(
    routes=[
        Route("/", homepage),
        Route("/echo", echo, methods=["POST"]),
        Route("/slow", slow),
        Route("/status", status),
        Route("/large", large),
    ]
)
