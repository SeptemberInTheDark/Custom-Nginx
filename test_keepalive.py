#!/usr/bin/env python3
"""
Тест keep-alive поддержки.
"""
import asyncio
import socket


async def test_keepalive():
    """Отправляем 3 HTTP-запроса на одном TCP-соединении."""
    reader, writer = await asyncio.open_connection("127.0.0.1", 8080)

    for i in range(3):
        print(f"\n=== Request {i+1} ===")

        # отправляем HTTP-запрос
        request = (
            f"GET / HTTP/1.1\r\n"
            f"Host: 127.0.0.1:9001\r\n"
            f"Connection: keep-alive\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )

        writer.write(request.encode())
        await writer.drain()

        # читаем ответ (только headers для простоты)
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = await reader.read(1024)
            if not chunk:
                print("Connection closed by server")
                return
            response += chunk

        # парсим статус
        status_line = response.split(b"\r\n")[0].decode()
        print(f"Response: {status_line}")

        # пропускаем тело
        await asyncio.sleep(0.1)

    print("\n✓ Keep-alive работает! Все 3 запроса отправлены на одном соединении")
    writer.close()
    await writer.wait_closed()


if __name__ == "__main__":
    asyncio.run(test_keepalive())
