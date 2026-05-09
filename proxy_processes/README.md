# proxy_processes — pre-fork worker model

Reverse-proxy в стиле nginx: один master + N worker-процессов.
Внутри каждого worker’а — синхронный accept-loop + ThreadPoolExecutor,
по одному потоку на клиента (блокирующие сокеты, `socket.settimeout()`
в роли таймаутов).

## Архитектура

```
                ┌─────────────────────────┐
                │  master process         │
                │  - bind listening sock  │
                │  - fork N workers       │
                │  - respawn on death     │
                │  - propagate SIGTERM    │
                └──────────┬──────────────┘
                           │ fork()
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
  ┌──────────┐       ┌──────────┐       ┌──────────┐
  │ worker 0 │       │ worker 1 │       │ worker N │
  │ accept() │       │ accept() │       │ accept() │
  │ tpool    │       │ tpool    │       │ tpool    │
  │ pool     │       │ pool     │       │ pool     │   per-worker
  └──────────┘       └──────────┘       └──────────┘   upstream pool
```

Все воркеры делают `accept()` на одном и том же inherited fd —
ядро (на Linux/macOS) распределяет соединения между ними
(грубо равномерно; с `SO_REUSEPORT` ещё лучше). GIL не общий —
полная утилизация ядер.

## Запуск

```bash
# из корня проекта
source venv/bin/activate

# upstreams
python -m uvicorn tests.echo_app:app --host 127.0.0.1 --port 9001 &
python -m uvicorn tests.echo_app:app --host 127.0.0.1 --port 9002 &

# запуск (4 worker-процесса)
python -m proxy_processes.main --config config.yaml --workers 4

# или с дефолтным конфигом
python -m proxy_processes.main -p 8080 --workers 4

# проверка
curl -v http://127.0.0.1:8080/
curl -v -X POST http://127.0.0.1:8080/echo -d 'hello'

# k6
BASE_URL=http://127.0.0.1:8080 k6 run tests/load/k6_basic.js
```

## Файлы

| Файл              | Что делает                                      |
| ----------------- | ----------------------------------------------- |
| `main.py`         | master: bind, fork, респавн, graceful shutdown  |
| `worker.py`       | дочерний процесс: accept + thread pool          |
| `handler.py`      | синхронный handle_client с keep-alive           |
| `http_parser.py`  | parse_request / parse_response_status_line      |
| `upstream_pool.py`| thread-safe пул внутри одного процесса          |
| `config.py`       | YAML-конфиг (то же, что в proxy/config.py)      |
| `logger.py`       | формат с ролью (master/worker-N) и pid          |

## Особенности по сравнению с asyncio-версией

- **Запуск тяжелее**: каждый worker — отдельный Python-процесс.
  Зато работают параллельно на разных ядрах.
- **idle keep-alive не шарится между воркерами.** У каждого процесса
  свой `UpstreamPool`. В сумме коннектов к upstream получается
  `workers * max_conns_per_upstream` — учитывайте при настройке.
- **Лимит клиентов — на воркер.** Глобальный считать сложно, поэтому
  каждый worker держит свой `BoundedSemaphore(max_client_conns)`.
- **Респавн**: упавший воркер мастер поднимает обратно. Отключить —
  `--no-respawn`.
- **Нет ContextVar для trace_id**: каждый поток внутри worker’а пишет
  свой trace_id явно в логи (через имя потока + uuid).
- **Сигналы**: master ловит SIGINT/SIGTERM, шлёт SIGTERM воркерам,
  ждёт 10с на graceful drain, иначе SIGKILL.

## Внутри одного worker’а

```
   listening fd (inherited from master)
              │
              ▼  blocking accept()
        ┌──────────┐
        │  main    │  acquire client semaphore
        │  thread  │  executor.submit(handle_client, sock)
        └────┬─────┘
             │
   ┌─────────┴─────────┐
   ▼                   ▼
 thread #1          thread #N    blocking sockets,
 handle_client      handle_client settimeout() даёт таймауты
   │                   │
   ▼                   ▼
 UpstreamPool.acquire (round-robin + idle)
                       │
                       ▼
                upstream socket
```

## Известные упрощения

- Один worker = много потоков. Под 1000 клиентов на воркер — 1000
  потоков, ~8 MB stack каждый. На 4 воркера это 32 ГБ виртуальной
  памяти (RSS меньше). Если ожидаете тысячи одновременных коннектов —
  смотрите вариант `proxy_threads_select/`.
- `take_idle()` не делает `peek` для проверки живости — сломанный
  keep-alive сокет даст ошибку при первом write, мы это обработаем
  как 502.
- `multiprocessing.set_start_method('fork')` вызывается в master’е
  принудительно: на macOS дефолт `spawn` не позволил бы инхеритить fd
  без явной передачи через socket.share() / sendfd.
