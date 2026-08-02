import { test, expect } from '@playwright/test';

// Checks de UI AUTOMÁTICOS. O que dá para verificar por máquina fica aqui e não
// vai para a fila do Renan. A rubrica subjetiva (gosto, hierarquia) só é acionada
// quando algo aqui falha de forma ambígua, ou quando review_subjective está ligado.

test('home responde 200 e tem estrutura mínima', async ({ page }) => {
  const resp = await page.goto('/index.html');
  expect(resp?.status(), 'index.html deveria responder 200').toBe(200);
  await expect(page.locator('h1')).toHaveCount(1);
  await expect(page).toHaveTitle(/.+/);
});

test('CSS carregou de fato (não só o link no HTML)', async ({ page }) => {
  // O check estático de regression só confirma que o <link> existe. Aqui a
  // pergunta é outra: o estilo chegou a ser aplicado no browser?
  await page.goto('/index.html');
  const body = page.locator('body');
  const bg = await body.evaluate((el) => getComputedStyle(el).backgroundColor);
  const font = await body.evaluate((el) => getComputedStyle(el).fontFamily);
  expect(bg, 'body sem background computado — CSS não aplicou').toBeTruthy();
  expect(font, 'body sem font-family — CSS não aplicou').toBeTruthy();
});

test('nenhum link interno quebrado', async ({ page, request }) => {
  await page.goto('/index.html');
  const hrefs = await page.locator('a[href]').evaluateAll((as) =>
    as.map((a) => a.getAttribute('href')).filter(
      (h) => h && !h.startsWith('http') && !h.startsWith('#') && !h.startsWith('mailto:'),
    ),
  );
  for (const href of hrefs) {
    const r = await request.get(href.startsWith('/') ? href : `/${href}`);
    expect(r.status(), `link interno quebrado: ${href}`).toBeLessThan(400);
  }
});

test('sem erro de console na home', async ({ page }) => {
  const erros = [];
  page.on('console', (m) => m.type() === 'error' && erros.push(m.text()));
  page.on('pageerror', (e) => erros.push(String(e)));
  await page.goto('/index.html');
  expect(erros, `erros de console: ${erros.join(' | ')}`).toHaveLength(0);
});

test('layout não regrediu (screenshot vs baseline)', async ({ page }) => {
  await page.goto('/index.html');
  await expect(page).toHaveScreenshot('home-desktop.png', { fullPage: true });
});

test('usável em tela estreita (375px)', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 800 });
  await page.goto('/index.html');
  // Overflow horizontal é o sintoma clássico de layout que não responde.
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth + 1,
  );
  expect(overflow, 'a página rola na horizontal em 375px').toBe(false);
  await expect(page).toHaveScreenshot('home-mobile.png', { fullPage: true });
});
