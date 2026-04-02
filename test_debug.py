#!/usr/bin/env python3
import asyncio
import json


async def test():
    for i in range(4):
        r, w = await asyncio.open_connection("127.0.0.1", 8080)
        w.write(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        await w.drain()

        # Read full response
        resp_data = b""
        while True:
            chunk = await r.read(4096)
            if not chunk:
                break
            resp_data += chunk

        # Parse HTTP response to get body
        parts = resp_data.split(b"\r\n\r\n", 1)
        if len(parts) == 2:
            body = parts[1].decode()
            # Echo app returns JSON with request info
            try:
                data = json.loads(body)
                method = data.get("method", "?")
                print(f"Request {i+1}: {method}")
                print(f"  Response body: {body[:100]}...")
            except:
                print(f"Request {i+1}: Could not parse JSON")
                print(f"  Body: {body[:100]}...")
        else:
            print(f"Request {i+1}: No body in response")

        w.close()
        await w.wait_closed()


asyncio.run(test())
