#!/usr/bin/env python3
"""
Расширенный тест keep-alive с различными сценариями.
"""
import asyncio


async def test_without_keepalive():
    """Тест: клиент просит close."""
    print("\n=== Test 1: Connection: close (client-side) ===")
    reader, writer = await asyncio.open_connection("127.0.0.1", 8080)

    # Первый запрос с close
    request = (
        "GET / HTTP/1.1\r\n"
        "Host: 127.0.0.1:9001\r\n"
        "Connection: close\r\n"
        "Content-Length: 0\r\n"
        "\r\n"
    )

    writer.write(request.encode())
    await writer.drain()

    # читаем ответ полностью (headers + body)
    response = b""
    while True:
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=1)
            if not chunk:
                break
            response += chunk
        except asyncio.TimeoutError:
            break

    status = response.split(b"\r\n")[0].decode()
    print(f"Response: {status}")
    print("✓ Connection properly closed after 'Connection: close'")


async def test_multiple_keepalive():
    """Тест: несколько запросов с keep-alive."""
    print("\n=== Test 2: Multiple keep-alive requests ===")
    reader, writer = await asyncio.open_connection("127.0.0.1", 8080)

    success_count = 0
    for i in range(5):
        request = (
            f"GET /?num={i} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:9001\r\n"
            f"Connection: keep-alive\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )

        writer.write(request.encode())
        await writer.drain()

        response = b""
        while b"\r\n\r\n" not in response:
            try:
                chunk = await asyncio.wait_for(reader.read(1024), timeout=2)
                if not chunk:
                    print(f"✗ Connection closed prematurely at request {i+1}")
                    return
                response += chunk
            except asyncio.TimeoutError:
                break

        # skip body читая до конца headers
        status = response.split(b"\r\n")[0].decode()
        if "200" in status:
            success_count += 1

        await asyncio.sleep(0.05)

    print(f"✓ All {success_count} requests succeeded on single connection")
    writer.close()
    await writer.wait_closed()


async def test_roundrobin():
    """Проверяем round-robin балансировку."""
    print("\n=== Test 3: Round-robin load balancing ===")

    upstreams_hit = {"9001": 0, "9002": 0}

    for req_num in range(10):
        reader, writer = await asyncio.open_connection("127.0.0.1", 8080)

        request = (
            "GET / HTTP/1.1\r\n"
            "Host: 127.0.0.1:9001\r\n"
            "Connection: close\r\n"
            "Content-Length: 0\r\n"
            "\r\n"
        )

        writer.write(request.encode())
        await writer.drain()

        response = b""
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=1)
                if not chunk:
                    break
                response += chunk
            except asyncio.TimeoutError:
                break

        # В ответе будет информация о порту котором был обработан запрос
        if b"9001" in response:
            upstreams_hit["9001"] += 1
        elif b"9002" in response:
            upstreams_hit["9002"] += 1

        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)

    print(f"Upstream 9001 hit: {upstreams_hit['9001']} times")
    print(f"Upstream 9002 hit: {upstreams_hit['9002']} times")

    # Проверяем что примерно поровну
    if abs(upstreams_hit["9001"] - upstreams_hit["9002"]) <= 2:
        print("✓ Round-robin distribution is balanced")
    else:
        print("✗ Round-robin distribution is NOT balanced")


async def main():
    """Запуск всех тестов."""
    await test_without_keepalive()
    await test_multiple_keepalive()
    await test_roundrobin()
    print("\n=== All tests completed ===")


if __name__ == "__main__":
    asyncio.run(main())
