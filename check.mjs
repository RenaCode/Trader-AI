import { chromium } from 'playwright';
const b = await chromium.launch();
const p = await (await b.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
await p.goto('https://trader.renacode.com', { waitUntil: 'networkidle' });
await p.fill('input[autocomplete="username"]', 'admin');
await p.fill('input[autocomplete="current-password"]', process.env.TRADER_PASS);
await p.click('button[type="submit"]');
await p.waitForTimeout(3000);
await p.click('.nav-tab:has-text("Stopień nauki")');
await p.waitForTimeout(2500);

console.log('Ile razy wystepuje "instrumentów":',
  await p.locator('text=instrumentów').count());

const info = await p.evaluate(() => {
  const out = [];
  document.querySelectorAll('.chart-wrap').forEach((el, i) => {
    const svg = el.querySelector('svg');
    if (!svg) return;
    out.push({
      i,
      kontener: el.clientWidth,
      viewBox: svg.getAttribute('viewBox'),
      svgRendered: Math.round(svg.getBoundingClientRect().width),
      par: svg.getAttribute('preserveAspectRatio'),
    });
  });
  return out;
});
console.log('\nWykresy:');
info.forEach(c => {
  const vbW = parseFloat(c.viewBox.split(' ')[2]);
  const skala = (c.svgRendered / vbW).toFixed(2);
  console.log(`  #${c.i} kontener=${c.kontener} viewBoxW=${vbW} render=${c.svgRendered} skalaX=${skala} par=${c.par}`);
});
await b.close();
