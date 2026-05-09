# proxy_threads_select — потоки + selectors

Reverse-proxy без `asyncio`. Внутри:
- 1 поток-**acceptor** делает `selectors.select()` на listening-сокете и
  раздаёт принятые соединения по reactor’ам round-robin;
- N потоков-**reactor**, каждый со своим `selectors.DefaultSelector()`;
  у каждого reactor’а свой набор сессий;
- общий thread-safe **UpstreamPool**: round-robin + idle keep-alive.

Каждая HTTP-сессия — конечный автомат в [conn.py](conn.py),
который двигают IO-события селектора. Это ровно то, что `asyncio` делает
под капотом, только написано вручную.

## Запуск

```bash
# 1) активируем venv из корня проекта
source ../venv/bin/activate

# 2) поднимаем upstreams (как в основном README)
python -m uvicorn tests.echo_app:app --host 127.0.0.1 --port 9001 &
python -m uvicorn tests.echo_app:app --host 127.0.0.1 --port 9002 &

# 3) запускаем прокси
python -m proxy_threads_select.main --config ../config.yaml --workers 4

# или с дефолтным конфигом (upstreams 9001/9002)
python -m proxy_threads_select.main -p 8080 --workers 4

# 4) проверка
curl -v http://127.0.0.1:8080/
curl -v -X POST http://127.0.0.1:8080/echo -d 'hello'

# 5) k6 (из корня проекта)
BASE_URL=http://127.0.0.1:8080 k6 run ../tests/load/k6_basic.js
```

## Файлы

| Файл              | Что делает                                           |
| ----------------- | ---------------------------------------------------- |
| `main.py`         | argparse, SIGINT/SIGTERM, запуск acceptor + reactors |
| `config.py`       | YAML-конфиг, dataclass’ы (как в proxy/config.py)     |
| `logger.py`       | формат с именем потока                               |
| `acceptor.py`     | accept-loop в одном потоке                           |
| `reactor.py`      | event-loop потока: select + dispatch + timeouts      |
| `conn.py`         | state machine HTTP-сессии                            |
| `http_parser.py`  | инкрементальные парсеры request/response             |
| `upstream_pool.py`| thread-safe пул upstream-серверов                    |

## Как это всё стыкуется

```
listening sock ── accept() ──┐
                             │
              ┌──────────────┴──────────────┐
              │           Acceptor          │
              │  (selectors на 1 сокете)    │
              └────┬───────┬────────┬───────┘
                   │ rr    │ rr     │ rr      push_client(sock, addr)
                   ▼       ▼        ▼
           ┌─────────┐ ┌─────────┐ ┌─────────┐
           │Reactor 0│ │Reactor 1│ │Reactor N│   each: own selector,
           │ Session │ │ Session │ │ Session │   own sessions list
           │ Session │ │ Session │ │ Session │
           └────┬────┘ └────┬────┘ └────┬────┘
                │            │           │       acquire_slot / take_idle
                └────────────┴──────┬────┘
                                    ▼
                          ┌──────────────────┐
                          │  UpstreamPool    │   shared, thread-safe
                          │  (RR + idle)     │
                          └────────┬─────────┘
                                   │
                                   ▼
                            upstream sockets
```

## Особенности по сравнению с asyncio-версией

- Нет `await` — вместо этого state machine в `Session._advance()`;
- Дедлайны — поле `Session.deadline`, проверяется reactor’ом раз за итерацию;
- `writer.drain()` не нужен: send/recv non-blocking, неотправленный
  хвост остаётся в `cli_out`/`up_out` и пишется на следующем
  `EVENT_WRITE`;
- `asyncio.Semaphore` → `threading.BoundedSemaphore` в пуле upstream;
- ContextVar для trace_id заменили на поле `Session.trace_id` +
  `[%(threadName)s]` в формате логов.

## Известные упрощения

- Глобальный лимит клиентов (`max_client_conns`) не реализован —
  ограничение даётся через OS backlog и количество reactor’ов;
- Для chunked-кодирования игнорируются trailers (большинство клиентов
  их не шлют);
- `take_idle()` не делает `peek` для проверки живости сокета — если
  upstream его уже закрыл, мы это обнаружим на ближайшем `send()`.
