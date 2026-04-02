/**
 * Высокая нагрузка без паузы.
 */

import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8080';

export const options = {
  vus: 300,           // экстремальная нагрузка
  duration: '30s',
  thresholds: {
    // На пределе возможностей: p(95) может быть до 1s
    http_req_duration: ['p(95)<1500'],      // 1.5s - реалистичный порог
    http_req_failed: ['rate<0.5'],          // до 50% ошибок допустимо при экстремальной нагрузке
  },
};

export default function () {
  const res = http.get(`${BASE_URL}/`);
  check(res, {
    'status 200': (r) => r.status === 200,
  });
}
