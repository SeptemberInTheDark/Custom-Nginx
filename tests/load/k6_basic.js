/**
 * Базовый нагрузочный тест прокси через k6.
 *
 * Запуск (прокси на 127.0.0.1:8080, upstreams на 9001/9002):
 *   k6 run tests/load/k6_basic.js
 *
 * С кастомным базовым URL:
 *   k6 run -e BASE_URL=http://localhost:8888 tests/load/k6_basic.js
 *
 * Установка k6: https://k6.io/docs/getting-started/installation/
 *   macOS: brew install k6
 */

import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8080';

export const options = {
  vus: 200,           // оптимально для одного proxy
  duration: '30s',    // длительность теста
  thresholds: {
    http_req_duration: ['p(95)<100'],  // 95% запросов быстрее 100ms
    http_req_failed: ['rate<0.2'],     // <20% ошибок
  },
};

export default function () {
  const res = http.get(`${BASE_URL}/`);
  check(res, {
    'status 200': (r) => r.status === 200,
  });
  // Без паузы - максимальная пропускная способность
}
