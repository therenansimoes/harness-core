const { spawn } = require('child_process');
const path = require('path');
const http = require('http');

const PORT = 4174;
const BASE = `http://127.0.0.1:${PORT}`;

function get(pathAndQuery) {
  return new Promise((resolve, reject) => {
    http
      .get(BASE + pathAndQuery, (res) => {
        let body = '';
        res.on('data', (chunk) => (body += chunk));
        res.on('end', () => {
          try {
            resolve({ status: res.statusCode, json: JSON.parse(body) });
          } catch (e) {
            reject(new Error(`bad json for ${pathAndQuery}: ${body}`));
          }
        });
      })
      .on('error', reject);
  });
}

function waitForHealth(retries) {
  return new Promise((resolve, reject) => {
    const attempt = (n) => {
      http
        .get(`${BASE}/health`, (res) => {
          if (res.statusCode === 200) resolve();
          else retry(n);
        })
        .on('error', () => retry(n));
    };
    const retry = (n) => {
      if (n <= 0) return reject(new Error('server did not become healthy'));
      setTimeout(() => attempt(n - 1), 200);
    };
    attempt(retries);
  });
}

async function main() {
  const server = spawn(process.execPath, [path.join(__dirname, '..', 'server.js')], {
    env: { ...process.env, PORT: String(PORT) },
    stdio: 'ignore',
  });

  const failures = [];

  try {
    await waitForHealth(25);

    // Criterion 1: filtering by category must return only matching, non-empty results.
    const electronics = await get('/api/products?category=electronics');
    if (electronics.json.count === 0) {
      failures.push('FAIL category filter: /api/products?category=electronics returned 0 items (expected 4)');
    } else {
      const wrongCategory = electronics.json.items.filter((p) => p.category !== 'electronics');
      if (wrongCategory.length > 0) {
        failures.push(`FAIL category filter: returned items outside category: ${JSON.stringify(wrongCategory)}`);
      }
      if (electronics.json.count !== 4) {
        failures.push(`FAIL category filter: expected 4 electronics items, got ${electronics.json.count}`);
      }
    }

    // Criterion 2: sort=price_asc must be non-decreasing.
    const sorted = await get('/api/products?sort=price_asc');
    const prices = sorted.json.items.map((p) => p.price);
    for (let i = 1; i < prices.length; i++) {
      if (prices[i] < prices[i - 1]) {
        failures.push(`FAIL sort: price_asc not sorted at index ${i}: ${prices[i - 1]} > ${prices[i]}`);
        break;
      }
    }

    // Criterion 3: price range filter sanity check (should already pass; guards regressions).
    const ranged = await get('/api/products?minPrice=20&maxPrice=50');
    const outOfRange = ranged.json.items.filter((p) => p.price < 20 || p.price > 50);
    if (outOfRange.length > 0) {
      failures.push(`FAIL price range: items out of [20,50]: ${JSON.stringify(outOfRange)}`);
    }
  } catch (e) {
    failures.push(`FAIL exception: ${e.message}`);
  } finally {
    server.kill();
  }

  if (failures.length > 0) {
    console.log(failures.join('\n'));
    console.log(`\n${failures.length} check(s) failed.`);
    process.exit(1);
  }

  console.log('All checks passed: category filter, sort, price range.');
  process.exit(0);
}

main();
