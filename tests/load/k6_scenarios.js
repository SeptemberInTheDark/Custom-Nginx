/**
 * Сценарии нагрузочного тестирования: дымовой, нагрузка, стресс.
 *
 * Запуск:
 *   k6 run tests/load/k6_scenarios.js
 *
 * Отдельный сценарий:
 *   k6 run --env SCENARIO=smoke tests/load/k6_scenarios.js
 *   k6 run --env SCENARIO=load tests/load/k6_scenarios.js
 *   k6 run --env SCENARIO=stress tests/load/k6_scenarios.js
 */

import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8080';
const SCENARIO = __ENV.SCENARIO || 'load';

const scenarios = {
  // Быстрая проверка: 5 VU, 10 сек
  smoke: {
    executor: 'constant-vus',
    vus: 5,
    duration: '10s',
    startTime: '0s',
  },
  // Нагрузка: 50 VU, 1 мин
  load: {
    executor: 'constant-vus',
    vus: 50,
    duration: '1m',
    startTime: '0s',
  },
  // Стресс: разгон до 200 VU за 2 мин
  stress: {
    executor: 'ramping-vus',
    startVUs: 0,
    stages: [
      { duration: '30s', target: 50 },
      { duration: '1m', target: 100 },
      { duration: '30s', target: 200 },
    ],
    startTime: '0s',
  },
};

export const options = {
  scenarios: {
    default: scenarios[SCENARIO] || scenarios.load,
  },
  thresholds: {
    http_req_duration: ['p(95)<2000', 'p(99)<5000'],
    http_req_failed: ['rate<0.02'],
  },
};

export default function () {
  // GET /
  const r1 = http.get(`${BASE_URL}/`);
  check(r1, { 'GET / ok': (r) => r.status === 200 });

  sleep(0.2);

  // POST /echo
  const r2 = http.post(
    `${BASE_URL}/echo`,
    'hello from k6',
    { headers: { 'Content-Type': 'text/plain' } }
  );
  check(r2, {
    'POST /echo ok': (r) => r.status === 200,
    'echo body': (r) => r.body === 'hello from k6',
  });

  sleep(0.3);

  // GET /status?code=200
  const r3 = http.get(`${BASE_URL}/status?code=200`);
  check(r3, { 'GET /status ok': (r) => r.status === 200 });
}
