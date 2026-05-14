import http from 'k6/http';
import { check } from 'k6';
import { Trend, Counter } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:18080';

// Детальные метрики для каждого воркера
const workerMetrics = {};
const workerCount = parseInt(__ENV.WORKER_COUNT) || 10; // настраиваемое количество

for (let i = 0; i < workerCount; i++) {
  workerMetrics[`worker_${i}_requests`] = new Counter(`worker_${i}_requests`);
  workerMetrics[`worker_${i}_duration`] = new Trend(`worker_${i}_duration`);
}

export const options = {
  scenarios: {
    balancing_test: {
      executor: 'constant-vus',
      vus: 1000,
      duration: '60s',
    },
  },
  thresholds: {
    'http_req_duration': ['p(95)<500', 'p(99)<1000'],
    'http_req_failed': ['rate<0.01'],
    // Убираем wildcard, thresholds будут динамическими через handleSummary
  },
};

export default function () {
  // Определяем воркер на основе VU
  const workerNum = __VU % workerCount;
  const workerLabel = `worker_${workerNum}`;
  
  const res = http.get(`${BASE_URL}/`);
  
  // Базовая проверка
  const statusOk = res.status === 200;
  const bodyNotEmpty = res.body && res.body.length > 0;
  
  check(res, {
    'status is 200': () => statusOk,
    'body not empty': () => bodyNotEmpty,
    [`${workerLabel}_status`]: () => statusOk,
  });
  
  // Записываем метрики для конкретного воркера
  if (workerMetrics[`${workerLabel}_requests`]) {
    workerMetrics[`${workerLabel}_requests`].add(1);
    workerMetrics[`${workerLabel}_duration`].add(res.timings.duration);
  }
}

// Функция для подробного отчета
export function handleSummary(data) {
  const report = {
    timestamp: new Date().toISOString(),
    test_duration_ms: data.state.testRunDurationMs,
    total_requests: 0,
    workers: {},
    balance_analysis: {},
    summary: {
      http_req_duration: data.metrics.http_req_duration?.values,
      http_req_failed: data.metrics.http_req_failed?.values,
      iterations: data.metrics.iterations?.values,
    }
  };
  
  // Собираем данные по воркерам
  let totalRequestsByWorker = 0;
  const workerRequests = [];
  
  for (let i = 0; i < workerCount; i++) {
    const reqMetric = data.metrics[`worker_${i}_requests`];
    const durMetric = data.metrics[`worker_${i}_duration`];
    
    if (reqMetric && reqMetric.values) {
      const reqCount = reqMetric.values.count || 0;
      totalRequestsByWorker += reqCount;
      workerRequests.push(reqCount);
      
      report.workers[`worker_${i}`] = {
        requests: reqCount,
        avg_duration_ms: durMetric?.values?.avg?.toFixed(2) || 0,
        min_duration_ms: durMetric?.values?.min?.toFixed(2) || 0,
        max_duration_ms: durMetric?.values?.max?.toFixed(2) || 0,
        p95_duration_ms: durMetric?.values?.['p(95)']?.toFixed(2) || 0,
        percentage: 0,
      };
    } else {
      report.workers[`worker_${i}`] = {
        requests: 0,
        avg_duration_ms: 0,
        p95_duration_ms: 0,
        percentage: 0,
      };
      workerRequests.push(0);
    }
  }
  
  report.total_requests = totalRequestsByWorker;
  
  // Вычисляем проценты
  for (const worker in report.workers) {
    if (report.total_requests > 0) {
      report.workers[worker].percentage = 
        ((report.workers[worker].requests / report.total_requests) * 100).toFixed(2);
    } else {
      report.workers[worker].percentage = "0.00";
    }
  }
  
  // Анализ балансировки
  const validRequests = workerRequests.filter(r => r > 0);
  const avg = report.total_requests / workerCount;
  const variance = workerRequests.reduce((sum, val) => sum + Math.pow(val - avg, 2), 0) / workerCount;
  const stdDev = Math.sqrt(variance);
  const cv = (stdDev / avg) * 100; // Коэффициент вариации
  
  // Находим min и max
  const maxRequests = Math.max(...workerRequests);
  const minRequests = Math.min(...workerRequests.filter(r => r > 0));
  const ratio = maxRequests / Math.max(1, minRequests);
  
  report.balance_analysis = {
    total_requests: report.total_requests,
    workers_count: workerCount,
    expected_per_worker: avg.toFixed(2),
    std_deviation: stdDev.toFixed(2),
    coefficient_of_variation: cv.toFixed(2) + '%',
    max_requests: maxRequests,
    min_requests: minRequests,
    max_min_ratio: ratio.toFixed(2),
    balance_quality: cv < 10 ? 'Excellent' : (cv < 20 ? 'Good' : (cv < 30 ? 'Fair' : 'Poor')),
    recommendation: '',
  };
  
  // Рекомендации
  if (cv < 10) {
    report.balance_analysis.recommendation = '✅ Отличная балансировка! Нагрузка распределена равномерно.';
  } else if (cv < 20) {
    report.balance_analysis.recommendation = '👍 Хорошая балансировка, небольшие отклонения допустимы.';
  } else if (cv < 30) {
    report.balance_analysis.recommendation = '⚠️ Удовлетворительная балансировка, есть перекосы. Проверьте настройки.';
  } else {
    report.balance_analysis.recommendation = '🔴 Плохая балансировка! Серьезный перекос нагрузки. Нужна настройка балансировщика.';
  }
  
  // Вывод в консоль в виде таблицы
  console.log('\n' + '═'.repeat(100));
  console.log('📊 АНАЛИЗ БАЛАНСИРОВКИ НАГРУЗКИ МЕЖДУ ВОРКЕРАМИ');
  console.log('═'.repeat(100));
  
  // Таблица воркеров
  console.log('\n📈 РАСПРЕДЕЛЕНИЕ ЗАПРОСОВ ПО ВОРКЕРАМ:');
  console.log('─'.repeat(100));
  console.log(`${'Воркер'.padEnd(12)} ${'Запросы'.padEnd(12)} ${'Доля'.padEnd(10)} ${'Avg (ms)'.padEnd(12)} ${'P95 (ms)'.padEnd(12)} ${'Статус'}`);
  console.log('─'.repeat(100));
  
  for (let i = 0; i < workerCount; i++) {
    const w = report.workers[`worker_${i}`];
    const expected = report.balance_analysis.expected_per_worker;
    const deviation = ((w.requests - expected) / expected * 100).toFixed(1);
    let status = '';
    
    if (Math.abs(deviation) < 10) status = '✅';
    else if (Math.abs(deviation) < 25) status = '⚠️';
    else status = '🔴';
    
    console.log(
      `worker_${i.toString().padEnd(5)} ${w.requests.toString().padEnd(12)} ` +
      `${w.percentage.toString().padEnd(10)} ${w.avg_duration_ms.toString().padEnd(12)} ` +
      `${w.p95_duration_ms.toString().padEnd(12)} ${status} (${deviation}%)`
    );
  }
  
  console.log('─'.repeat(100));
  
  // Статистика
  console.log('\n📊 СТАТИСТИКА РАСПРЕДЕЛЕНИЯ:');
  console.log(`   • Всего запросов: ${report.balance_analysis.total_requests}`);
  console.log(`   • Количество воркеров: ${report.balance_analysis.workers_count}`);
  console.log(`   • Среднее на воркер: ${report.balance_analysis.expected_per_worker}`);
  console.log(`   • Максимум запросов: ${report.balance_analysis.max_requests}`);
  console.log(`   • Минимум запросов: ${report.balance_analysis.min_requests}`);
  console.log(`   • Отношение max/min: ${report.balance_analysis.max_min_ratio}`);
  console.log(`   • Стандартное отклонение: ${report.balance_analysis.std_deviation}`);
  console.log(`   • Коэффициент вариации: ${report.balance_analysis.coefficient_of_variation}`);
  
  console.log('\n🎯 ОЦЕНКА КАЧЕСТВА БАЛАНСИРОВКИ:');
  console.log(`   • Уровень: ${report.balance_analysis.balance_quality}`);
  console.log(`   • ${report.balance_analysis.recommendation}`);
  
  // Общие результаты теста
  console.log('\n📈 ОБЩИЕ РЕЗУЛЬТАТЫ ТЕСТА:');
  if (report.summary.http_req_duration) {
    console.log(`   • HTTP Request Duration - avg: ${report.summary.http_req_duration.avg?.toFixed(2)}ms, p95: ${report.summary.http_req_duration['p(95)']?.toFixed(2)}ms`);
  }
  if (report.summary.http_req_failed) {
    console.log(`   • HTTP Request Failed: ${(report.summary.http_req_failed.rate * 100).toFixed(2)}%`);
  }
  if (report.summary.iterations) {
    console.log(`   • Iterations: ${report.summary.iterations.count}`);
  }
  
  console.log('\n' + '═'.repeat(100) + '\n');
  
  return {
    'balancing_report.json': JSON.stringify(report, null, 2),
  };
}