import { chromium } from 'playwright';

const URL = 'https://trader.renacode.com';
const PASS = process.env.TRADER_PASS;

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 950 }, deviceScaleFactor: 2 });
const page = await ctx.newPage();

const errors = [];
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));

await page.goto(URL, { waitUntil: 'networkidle' });
await page.screenshot({ path: '/tmp/shots/01-login.png' });
console.log('01 login  | tytul:', await page.title(), '| pola:', await page.locator('input').count());

await page.fill('input[autocomplete="username"]', 'admin');
await page.fill('input[autocomplete="current-password"]', PASS);
await page.click('button[type="submit"]');
await page.waitForTimeout(3500);
await page.screenshot({ path: '/tmp/shots/02-stan.png', fullPage: true });
console.log('02 stan   | zakladki:', await page.locator('.nav-tab').count(), '| wykresy svg:', await page.locator('svg').count());

for (const [i, label] of [['03','Stopień nauki'], ['04','Ruchy'], ['05','Źródła'], ['06','Ryzyko']]) {
  await page.click(`.nav-tab:has-text("${label}")`);
  await page.waitForTimeout(2500);
  await page.screenshot({ path: `/tmp/shots/${i}-${label.split(' ')[0].toLowerCase()}.png`, fullPage: true });
  console.log(`${i} ${label.padEnd(14)}| svg:`, await page.locator('svg').count(), '| puste sekcje:', await page.locator('.empty').count());
}

// Druga zakladka silnika
await page.click('.engine-tab:has-text("Giełda")');
await page.waitForTimeout(2500);
await page.screenshot({ path: '/tmp/shots/07-equity.png', fullPage: true });
console.log('07 equity | ok');

console.log('\nBLEDY KONSOLI:', errors.length ? errors.slice(0, 8).join('\n  ') : 'brak');
await browser.close();
