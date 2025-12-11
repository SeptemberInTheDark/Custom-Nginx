import asyncio
from typing import TypeVar, Coroutine, Any

T = TypeVar("T")


async def with_timeout(
    coro: Coroutine[Any, Any, T],
    timeout: float,
    operation: str = ""
) -> T:
    """
    Обёртка над asyncio.wait_for с понятным сообщением об ошибке.
    
    Args:
        coro: Корутина для выполнения
        timeout: Таймаут в секундах
        operation: Описание операции для сообщения об ошибке
    
    Raises:
        TimeoutError: Если операция не завершилась за отведённое время
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(f"Timeout during {operation} after {timeout}s")


class TimeoutScope:
    """
    Контекст для общего таймаута на всю операцию.
    
    Позволяет отслеживать оставшееся время для нескольких
    последовательных операций.
    
    Пример:
        scope = TimeoutScope(30.0)  # 30 секунд на всё
        
        await with_timeout(op1(), scope.remaining, "op1")
        await with_timeout(op2(), scope.remaining, "op2")
        
        if scope.expired:
            raise TimeoutError("Total timeout exceeded")
    """

    def __init__(self, total_timeout: float):
        self._start_time = asyncio.get_event_loop().time()
        self._total_timeout = total_timeout

    @property
    def elapsed(self) -> float:
        """Сколько времени прошло с начала."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        return loop.time() - self._start_time

    @property
    def remaining(self) -> float:
        """Сколько времени осталось."""
        return max(0, self._total_timeout - self.elapsed)

    @property
    def expired(self) -> bool:
        """Истёк ли таймаут."""
        return self.remaining <= 0
