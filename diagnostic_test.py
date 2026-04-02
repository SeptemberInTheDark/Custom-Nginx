# diagnostic_test.py
"""
Полная диагностика прокси и upstream
"""
import asyncio
import aiohttp
import time
from datetime import datetime

async def test_direct_upstream():
    """Тест прямого подключения к upstream"""
    print("\n=== Testing Direct Upstream ===")
    success = 0
    errors = 0
    
    async with aiohttp.ClientSession() as session:
        for i in range(100):
            try:
                async with session.get('http://127.0.0.1:9001/', timeout=5) as resp:
                    if resp.status == 200:
                        success += 1
                    else:
                        errors += 1
            except Exception as e:
                errors += 1
                print(f"Error: {e}")
            
            if i % 20 == 0:
                print(f"Progress: {i}/100")
    
    print(f"Direct upstream: {success}% success, {errors}% errors")
    return success, errors

async def test_through_proxy():
    """Тест через прокси"""
    print("\n=== Testing Through Proxy ===")
    success = 0
    errors = 0
    
    async with aiohttp.ClientSession() as session:
        for i in range(100):
            try:
                async with session.get('http://127.0.0.1:8080/', timeout=5) as resp:
                    if resp.status == 200:
                        success += 1
                    else:
                        errors += 1
            except Exception as e:
                errors += 1
                print(f"Error: {e}")
            
            if i % 20 == 0:
                print(f"Progress: {i}/100")
    
    print(f"Through proxy: {success}% success, {errors}% errors")
    return success, errors

async def test_connection_rate():
    """Тест скорости установления соединений"""
    print("\n=== Testing Connection Rate ===")
    
    async def connect_test(port, duration=10):
        start = time.time()
        connections = 0
        errors = 0
        
        while time.time() - start < duration:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection('127.0.0.1', port),
                    timeout=1
                )
                writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                connections += 1
                writer.close()
                await writer.wait_closed()
            except:
                errors += 1
        
        return connections, errors
    
    for port in [9001, 8080]:
        conn, err = await connect_test(port)
        print(f"Port {port}: {conn} connections, {err} errors in 10s")

async def main():
    print(f"Test started at {datetime.now()}")
    
    # Тест 1: Прямой upstream
    up_success, up_errors = await test_direct_upstream()
    
    # Тест 2: Через прокси
    proxy_success, proxy_errors = await test_through_proxy()
    
    # Тест 3: Скорость соединений
    await test_connection_rate()
    
    print(f"\n=== SUMMARY ===")
    print(f"Direct upstream success rate: {up_success}%")
    print(f"Through proxy success rate: {proxy_success}%")
    
    if up_success < 90:
        print("❌ PROBLEM: Upstream server is failing")
    elif proxy_success < 90:
        print("❌ PROBLEM: Proxy is failing (upstream works)")
    else:
        print("✅ Both work! Issue is with high concurrency")

if __name__ == "__main__":
    asyncio.run(main())