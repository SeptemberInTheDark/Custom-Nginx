# Custom-Nginx

Асинхронный mini-nginx на `asyncio`: HTTP/1.1 reverse proxy, который принимает
TCP-соединения от клиентов, проксирует запросы к одному или нескольким upstream
сервисам и выдерживает нагрузку за счёт streaming, keep-alive, backpressure,
timeouts и лимитов соединений.

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![Asyncio](https://img.shields.io/badge/asyncio-IO--bound-2F7D32?style=flat-square)
![HTTP](https://img.shields.io/badge/HTTP-1.1-0B7285?style=flat-square)
![k6](https://img.shields.io/badge/k6-load%20tested-7D64FF?style=flat-square&logo=k6&logoColor=white)

## Что умеет

| Возможность | Статус |
| --- | --- |
| TCP-сервер на `asyncio.start_server` | готово |
| Минимальный HTTP/1.1 parser: start-line + headers | готово |
| Reverse proxy к одному или нескольким upstream | готово |
| Round-robin балансировка | готово |
| Streaming request/response body без полного буфера в памяти | готово |
| Backpressure через `await writer.drain()` | готово |
| Keep-alive для client и upstream соединений | готово |
| Таймауты `connect/read/write/total` | готово |
| Лимиты client/upstream соединений | готово |
| Простое логирование slow/error запросов | готово |

## Быстрый старт

Установить зависимости:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Запустить два локальных upstream сервиса:

```bash
venv/bin/python -m uvicorn tests.echo_app:app --host 127.0.0.1 --port 9001 --workers 1
venv/bin/python -m uvicorn tests.echo_app:app --host 127.0.0.1 --port 9002 --workers 1
```

Запустить прокси. Если `8080` занят, используй порт `18080`:

```bash
venv/bin/python -m proxy.main --port 18080 --log-level warning
```

Проверить запрос:

```bash
curl -v http://127.0.0.1:18080/
curl -v -X POST http://127.0.0.1:18080/echo -d 'hello world'
```

## Нагрузочное тестирование

Базовый k6 сценарий:

```bash
BASE_URL=http://127.0.0.1:18080 k6 run tests/load/k6_basic.js
```

Дополнительные сценарии:

```bash
BASE_URL=http://127.0.0.1:18080 k6 run tests/load/k6_ramp.js
BASE_URL=http://127.0.0.1:18080 k6 run tests/load/k6_scenarios.js
BASE_URL=http://127.0.0.1:18080 SCENARIO=stress k6 run tests/load/k6_scenarios.js
```

Альтернативные инструменты:

```bash
wrk -t4 -c128 -d30s http://127.0.0.1:18080/
ab -n 5000 -c 200 http://127.0.0.1:18080/
vegeta attack -duration=30s -rate=500 | vegeta report
```

Пример успешного результата на `500 VU / 30s`:

```text
http_req_duration: p(95)=69.75ms
http_req_failed:   rate=0.51%
http_reqs:         ~8000 req/s
```

## Конфигурация

Конфиг можно передать через `--config`:

```bash
venv/bin/python -m proxy.main --config config.yaml
```

Пример YAML:

```yaml
listen: "127.0.0.1:18080"

upstreams:
  - host: "127.0.0.1"
    port: 9001
  - host: "127.0.0.1"
    port: 9002

timeouts:
  connect_ms: 1000
  read_ms: 15000
  write_ms: 15000
  total_ms: 30000

limits:
  max_client_conns: 1000
  max_conns_per_upstream: 250
  backlog: 32768
  limit: 1048576

logging:
  level: "info"
```

## Архитектура

```text
client
  |
  v
ProxyServer
  |
  v
ClientConnectionHandler
  |
  +--> Http parser
  +--> TimeoutPolicy
  +--> UpstreamPool -- round-robin --> upstream:9001
  |                                  --> upstream:9002
  |
  v
stream response back to client
```

Основные модули:

| Файл | Ответственность |
| --- | --- |
| `proxy/main.py` | CLI entrypoint, загрузка конфига, graceful shutdown |
| `proxy/proxy_server.py` | TCP-сервер, лимит client connections |
| `proxy/client_handler.py` | HTTP keep-alive цикл, streaming request/response |
| `proxy/upstream_pool.py` | round-robin, лимиты и reuse upstream-соединений |
| `proxy/timeouts.py` | обёртки для `asyncio.wait_for` и `writer.drain()` |
| `proxy/utils/http.py` | минимальный HTTP parser |
| `proxy/logger.py` | trace-id и формат логов |
| `proxy/metrics.py` | базовые метрики |

## Стратегия стриминга

Прокси не буферизует тело запроса или ответа целиком:

1. читает chunk у клиента через `await client_reader.read(n)`;
2. сразу пишет chunk в upstream через `up_writer.write(chunk)`;
3. ждёт `await up_writer.drain()`, чтобы учитывать backpressure;
4. аналогично стримит ответ upstream обратно клиенту.

Такой подход важен для больших тел, медленных клиентов и стабильной памяти под
нагрузкой.

## Цели практической работы

Проект закрепляет темы из блока async:

- итераторы, генераторы, корутины и event loop;
- задачи, планирование и отмена задач;
- различия CPU-bound и IO-bound нагрузки;
- `asyncio.start_server`, `asyncio.open_connection`, `StreamReader`,
  `StreamWriter`, `wait_for`, `Task`;
- сетевые паттерны: keep-alive, streaming, backpressure, timeouts, pooling.

## Критерии приёмки

- `curl -v http://127.0.0.1:18080/anything` возвращает ответ upstream с
  корректным статусом и заголовками.
- Под нагрузкой сервер не падает, ограничивает одновременные соединения и не
  показывает заметной утечки памяти.
- Таймауты срабатывают предсказуемо: зависший upstream не держит клиента вечно.
- k6 thresholds проходят: `http_req_duration p(95)<100`,
  `http_req_failed rate<0.2`.

## Структура проекта

```text
proxy/
  main.py
  config.py
  proxy_server.py
  client_handler.py
  upstream_pool.py
  timeouts.py
  logger.py
  metrics.py
  utils/http.py

tests/
  echo_app.py
  load/
    k6_basic.js
    k6_ramp.js
    k6_scenarios.js
    k6_stress.js
    k6_max.js

config.yaml
config.example.yaml
README.md
```

## Что можно улучшить дальше

- Health-checks upstream сервисов и исключение недоступных из балансировки.
- Retry policy для безопасных методов при connect/read timeout.
- Circuit breaker для проблемного upstream.
- Rate limiting через token bucket.
- TLS termination на входе или TLS к upstream.
- Горячая перезагрузка конфигурации через `SIGHUP`.
- Заголовки `X-Forwarded-For`, `Via`, `X-Request-Id`.
- Отдельная `/metrics` ручка или мини-панель статистики.

## Вопросы для самопроверки

- Где и зачем применять `await writer.drain()`?
- Чем отличается отмена задачи от таймаута?
- Что происходит с event loop, когда upstream долго отвечает?
- Почему сеть считается IO-bound нагрузкой?
- Как избежать утечек при исключениях во время streaming?
- Почему важно ограничивать число одновременных соединений?
