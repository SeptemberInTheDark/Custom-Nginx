# Сценарии тестирования

## Установка зависимостей

```bash
pip install -r requirements.txt
```

## Запуск upstream-серверов

```bash
# Терминал 1
uvicorn tests.echo_app:app --host 127.0.0.1 --port 9001

# Терминал 2
uvicorn tests.echo_app:app --host 127.0.0.1 --port 9002
```

## Запуск прокси

```bash
# С дефолтными настройками
python -m proxy.main

# С debug логами
python -m proxy.main --log-level debug

# С конфигом
python -m proxy.main --config config.example.yaml

# С кастомным портом
python -m proxy.main --port 8888
```

## Тестирование curl

```bash
# Простой GET
curl -v http://127.0.0.1:8080/

# POST с телом
curl -X POST http://127.0.0.1:8080/echo -d "hello"

# Проверка таймаутов (зависнет на 10 сек)
curl http://127.0.0.1:8080/slow?delay=10

# Разные статусы
curl -v http://127.0.0.1:8080/status?code=404

# Большой ответ (1MB)
curl http://127.0.0.1:8080/large?size=1048576 > /dev/null
```

## Нагрузочное тестирование

```bash
# wrk
wrk -t4 -c128 -d30s http://127.0.0.1:8080/

# ab (Apache Benchmark)
ab -n 5000 -c 200 http://127.0.0.1:8080/

# vegeta
echo "GET http://127.0.0.1:8080/" | vegeta attack -duration=30s -rate=500 | vegeta report
```
