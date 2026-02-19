/**
 * Ramp-up: плавный разгон нагрузки.
 *
 * Запуск:
 *   k6 run tests/load/k6_ramp.js
 *
 * Полезно смотреть как ведёт себя прокси при росте числа соединений.
 */

import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8080';

export const options = {
  stages: [
    { duration: '30s', target: 20 },   // разгон до 20 VU
    { duration: '1m', target: 20 },     // держим 20 VU
    { duration: '30s', target: 50 },    // разгон до 50 VU
    { duration: '1m', target: 50 },     // держим 50 VU
    { duration: '30s', target: 0 },    // сброс
  ],
  thresholds: {
    http_req_duration: ['p(95)<1000'],
    http_req_failed: ['rate<0.05'],
  },
};

export default function () {
  const res = http.get(`${BASE_URL}/`);
  check(res, { 'status 200': (r) => r.status === 200 });
  sleep(0.5 + Math.random() * 0.5);
}
