"""
Утилиты для работы с таймаутами.

asyncio.wait_for() кидает asyncio.TimeoutError без деталей,
тут мы оборачиваем его с нормальным сообщением.
"""
import asyncio
from typing import TypeVar, Coroutine, Any

T = TypeVar("T")


async def with_timeout(
    coro: Coroutine[Any, Any, T],
    timeout: float,
    operation: str = ""
) -> T:
    """
    Обёртка над wait_for с понятной ошибкой.
    
    Вместо голого TimeoutError получаем:
    "Timeout during reading response after 15.0s"
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(f"Timeout during {operation} after {timeout}s")


class TimeoutScope:
    """
    Общий таймаут на несколько операций.
    
    Пример:
        scope = TimeoutScope(30.0)  # 30 сек на всё
        
        await with_timeout(op1(), scope.remaining, "op1")  # съело 5 сек
        await with_timeout(op2(), scope.remaining, "op2")  # осталось 25 сек
        
        if scope.expired:
            raise TimeoutError("Total timeout")
    
    Пока не используется, но пригодится для total_timeout.
    """

    def __init__(self, total_timeout: float):
        # time() возвращает монотонное время loop'а
        self._start_time = asyncio.get_event_loop().time()
        self._total_timeout = total_timeout

    @property
    def elapsed(self) -> float:
        """Сколько прошло с начала."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        return loop.time() - self._start_time

    @property
    def remaining(self) -> float:
        """Сколько осталось. Не меньше 0."""
        return max(0, self._total_timeout - self.elapsed)

    @property
    def expired(self) -> bool:
        return self.remaining <= 0
