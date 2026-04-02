#!/usr/bin/env python3
import asyncio


async def test():
    results = []
    for i in range(4):
        r, w = await asyncio.open_connection("127.0.0.1", 8080)
        w.write(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        await w.drain()
        resp = await r.read(4096)
        if b"9001" in resp:
            results.append("9001")
        else:
            results.append("9002")
        w.close()
        await w.wait_closed()
    print("Results:", results)
    print("Expected: ['9001', '9002', '9001', '9002']")


asyncio.run(test())
