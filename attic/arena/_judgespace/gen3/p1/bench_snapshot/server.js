const http = require('http');
const fs = require('fs');
const path = require('path');
const url = require('url');

const products = JSON.parse(fs.readFileSync(path.join(__dirname, 'data.json'), 'utf8'));

function filterProducts(items, query) {
  let result = items;

  if (query.category) {
    result = result.filter((p) => p.tags && p.tags.includes(query.category));
  }

  if (query.minPrice) {
    result = result.filter((p) => p.price >= parseFloat(query.minPrice));
  }

  if (query.maxPrice) {
    result = result.filter((p) => p.price <= parseFloat(query.maxPrice));
  }

  return result;
}

function sortProducts(items, sort) {
  const sorted = [...items];
  if (sort === 'price_asc') {
    sorted.sort((a, b) => a.price - b.price);
  } else if (sort === 'price_desc') {
    sorted.sort((a, b) => b.price - a.price);
  }
  return sorted;
}

const server = http.createServer((req, res) => {
  const parsed = url.parse(req.url, true);

  if (parsed.pathname === '/api/products') {
    let items = filterProducts(products, parsed.query);
    items = sortProducts(items, parsed.query.sort);

    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ count: items.length, items }));
    return;
  }

  if (parsed.pathname === '/health') {
    res.writeHead(200, { 'Content-Type': 'text/plain' });
    res.end('ok');
    return;
  }

  res.writeHead(404, { 'Content-Type': 'text/plain' });
  res.end('not found');
});

if (require.main === module) {
  const PORT = process.env.PORT || 4173;
  server.listen(PORT, () => {
    console.log(`catalog server listening on ${PORT}`);
  });
}

module.exports = { server, filterProducts, sortProducts };
