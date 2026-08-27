/* Test dymny calej aplikacji: logowanie -> kazda zakladka -> brak bledow JS.
 *
 * DLACZEGO istnieje: trzy regresje z 27.08 (bialy ekran po zalogowaniu, wykresy
 * renderowane na 1/3 szerokosci, rozciagniety tekst osi) przeszly przez build
 * bez jednego ostrzezenia. `npm run build` sprawdza, czy kod sie kompiluje -
 * nie czy cokolwiek widac. Ten plik sprawdza to drugie.
 *
 * Uruchomienie:  BASE_URL=... TEST_LOGIN=... TEST_PASSWORD=... node tests/smoke.spec.mjs
 */
import { chromium } from 'playwright';

const BASE = process.env.BASE_URL || 'http://localhost:5174';
const LOGIN = process.env.TEST_LOGIN || 'admin';
const PASSWORD = process.env.TEST_PASSWORD;

const TABS = ['Stan', 'Stopień nauki', 'Ruchy', 'Źródła', 'Ryzyko'];

let failures = 0;
const check = (ok, label, detail = '') => {
  console.log(`${ok ? '  ok  ' : '  BLAD'} ${label}${detail ? ' — ' + detail : ''}`);
  if (!ok) failures++;
};

if (!PASSWORD) {
  console.error('TEST_PASSWORD nie jest ustawiony');
  process.exit(2);
}

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();

const jsErrors = [];
page.on('pageerror', (e) => jsErrors.push(e.message));

try {
  // --- ekran logowania -----------------------------------------------------
  await page.goto(BASE, { waitUntil: 'networkidle' });
  check(await page.locator('input[autocomplete="username"]').count() === 1, 'formularz logowania widoczny');

  // Dane nie moga byc dostepne bez sesji.
  const anon = await page.request.get(`${BASE}/api/engines`);
  check(anon.status() === 401, 'API odmawia bez sesji', `status ${anon.status()}`);

  // --- logowanie -----------------------------------------------------------
  await page.fill('input[autocomplete="username"]', LOGIN);
  await page.fill('input[autocomplete="current-password"]', PASSWORD);
  await page.click('button[type="submit"]');
  await page.waitForSelector('.nav-tab', { timeout: 20000 });
  check(true, 'logowanie i wejscie do dashboardu');

  // Bialy ekran: root pusty mimo zalogowania.
  const rootLen = (await page.locator('#root').innerHTML()).length;
  check(rootLen > 1000, 'dashboard sie wyrenderowal', `${rootLen} znakow w #root`);

  // --- zakladki ------------------------------------------------------------
  for (const tab of TABS) {
    await page.click(`.nav-tab:has-text("${tab}")`);
    await page.waitForTimeout(2000);

    const cards = await page.locator('.glass-card').count();
    check(cards > 0, `zakladka "${tab}" ma tresc`, `${cards} kart`);

    // Kazdy wykres musi wypelniac kontener. Rozjazd viewBox <-> szerokosc
    // renderu oznacza albo zly pomiar, albo deformacje tekstu.
    const charts = await page.evaluate(() => {
      const out = [];
      document.querySelectorAll('.chart-wrap svg').forEach((svg) => {
        const vb = (svg.getAttribute('viewBox') || '0 0 0 0').split(' ');
        out.push({
          viewBoxW: parseFloat(vb[2]),
          renderW: Math.round(svg.getBoundingClientRect().width),
        });
      });
      return out;
    });
    for (const [i, c] of charts.entries()) {
      const skala = c.renderW / c.viewBoxW;
      check(Math.abs(skala - 1) < 0.02,
            `zakladka "${tab}", wykres #${i} w skali 1:1`,
            `viewBox=${c.viewBoxW} render=${c.renderW} skala=${skala.toFixed(2)}`);
    }
  }

  // --- drugi silnik --------------------------------------------------------
  const engines = await page.locator('.engine-tab').count();
  check(engines === 2, 'dwie zakladki silnikow', `${engines}`);
  await page.locator('.engine-tab').nth(1).click();
  await page.waitForTimeout(2500);
  check((await page.locator('.glass-card').count()) > 0, 'drugi silnik sie wyrenderowal');

  // --- wylogowanie ---------------------------------------------------------
  await page.click('button:has-text("Wyloguj")');
  await page.waitForSelector('input[autocomplete="username"]', { timeout: 10000 });
  check(true, 'wylogowanie wraca na ekran logowania');

  check(jsErrors.length === 0, 'brak wyjatkow JavaScript',
        jsErrors.slice(0, 3).join(' | ') || 'brak');
} catch (e) {
  check(false, 'test przerwany wyjatkiem', e.message);
} finally {
  await browser.close();
}

console.log(failures === 0 ? '\nTEST DYMNY: PRZESZEDL' : `\nTEST DYMNY: ${failures} BLEDOW`);
process.exit(failures === 0 ? 0 : 1);
