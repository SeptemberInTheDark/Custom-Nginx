# Нагрузочное тестирование (k6)

Перед запуском тестов должны быть подняты:

1. **Upstream-серверы** (например echo_app):
   ```bash
   uvicorn tests.echo_app:app --host 127.0.0.1 --port 9001
   uvicorn tests.echo_app:app --host 127.0.0.1 --port 9002
   ```

2. **Прокси**:
   ```bash
   python -m proxy.main
   ```

## Установка k6

- **macOS**: `brew install k6`
- **Linux**: см. [официальную документацию](https://k6.io/docs/getting-started/installation/)

## Запуск тестов

Базовый тест (10 VU, 30 сек):

```bash
k6 run tests/load/k6_basic.js
```

Другой порт прокси:

```bash
k6 run -e BASE_URL=http://127.0.0.1:8888 tests/load/k6_basic.js
```

Сценарии (smoke / load / stress):

```bash
k6 run tests/load/k6_scenarios.js
k6 run -e SCENARIO=smoke tests/load/k6_scenarios.js
k6 run -e SCENARIO=stress tests/load/k6_scenarios.js
```

Плавный разгон нагрузки:

```bash
k6 run tests/load/k6_ramp.js
```

## Вывод результатов

По умолчанию k6 выводит сводку в консоль. Для отчёта в JSON/HTML:

```bash
k6 run --summary-trend-stats="avg,p(95),p(99)" tests/load/k6_basic.js
k6 run --out json=results.json tests/load/k6_basic.js
```

Для облачных отчётов (k6 Cloud): `k6 login` и `k6 run --out cloud tests/load/k6_basic.js`.
