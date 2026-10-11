/* Stend interfejsu z PODSTAWIONYM API: jak pulpit zachowuje sie, gdy odczyt padnie.
 *
 * URUCHOMIENIE (z katalogu glownego repozytorium):
 *
 *     cd web && npm run build && cd ..
 *     node tests/stend_ui.spec.mjs
 *
 * Nie potrzebuje ani bazy, ani dzialajacego API, ani konta - cala wartosc tego
 * narzedzia polega na tym, ze odpowiedzi podstawia samo. Sprawdza zbudowany
 * `web/dist`; inny katalog wskazuje sie zmienna UI_DIST (tak robi sie dowod
 * czerwony-zielony na kopii drzewa):
 *
 *     UI_DIST=/tmp/kopia/web/dist node tests/stend_ui.spec.mjs
 *
 * ZALEZNOSC: `playwright`. NIE jest zadeklarowana nigdzie w repozytorium -
 * nie ma korzeniowego package.json, a `tests/package.json` nie ma zaleznosci.
 * Lezy w `node_modules/` obok, zainstalowana recznie, tak samo jak dla
 * `smoke.spec.mjs`. Gdy jej brakuje:
 *
 *     npm install --no-save playwright && npx playwright install chromium
 *
 * ------------------------------------------------------------------------
 * DLACZEGO ISTNIEJE OBOK smoke.spec.mjs
 *
 * `smoke.spec.mjs` sprawdza aplikacje ZYWA: loguje sie prawdziwym haslem do
 * prawdziwego API i przechodzi zakladki. Odpowiada na pytanie "czy to w ogole
 * wstaje". Nie odpowiada - i nie moze - na pytanie "co widzi wlasciciel, kiedy
 * odczyt padnie", bo nie umie sprawic, zeby padl.
 *
 * Ma przy tym wade, ktora warto znac, zanim ktos uzna jego zielony wynik za
 * dowod czegokolwiek. `smoke.spec.mjs:66-82` zbiera wykresy do tablicy
 * (`document.querySelectorAll('.chart-wrap svg')`) i dopiero POTEM, w petli po
 * tej tablicy, wola `check()`. Przy pustej bazie zaden wykres sie nie renderuje,
 * tablica jest pusta, petla nie wykonuje ANI JEDNEJ asercji, licznik bledow
 * zostaje na zerze i skrypt drukuje "TEST DYMNY: PRZESZEDL". Straznik, ktorego
 * asercje siedza w petli po danych mogacych byc pustymi, nie twierdzi nic -
 * a wyglada, jakby twierdzil wszystko.
 *
 * ------------------------------------------------------------------------
 * CZEGO TEN STEND PILNUJE
 *
 * Jednej rzeczy, ktora 2026-09-22 wrocila naraz w czterech plikach frontu:
 * NIEUDANY ODCZYT NIE MOZE BYC RENDEROWANY JAKO TWIERDZENIE O STANIE SYSTEMU.
 * "Brak zapisanych kluczy", "Brak aktywnych sesji", "Weryfikacja dwuetapowa:
 * Wylaczona", "Kill-switch nigdy sie nie uruchomil" - kazde z tych zdan
 * pojawialo sie WYLACZNIE dlatego, ze zapytanie padlo, i kazde brzmialo jak
 * pomiar. Brak danych ma dawac "-" i ton neutralny; awaria ma dawac jawny
 * komunikat o awarii; nigdy zielone zero w miejscu braku pomiaru.
 *
 * Scenariusz 1 sprawdza to na KAZDEJ zakladce, dwoma twierdzeniami, ktore nie
 * zalezą od sformulowan:
 *   - zaden pusty stan nie zaczyna sie od slowa "Brak" (to jest wlasnie ksztalt
 *     falszywego twierdzenia),
 *   - tresc bledu, ktora odeslal serwer, JEST widoczna na ekranie - czyli
 *     wyjatek nie zostal polkniety po drodze.
 * Do tego zero wyjatkow JavaScript: nieobslugiwany `await` w komponencie konczy
 * sie nieobsluzonym odrzuceniem promisy i to tu wychodzi.
 *
 * Scenariusze 2-4 pilnuja wad szczegolowych z tego samego audytu: limitu bez
 * pomiaru rysowanego na zielono, jednostki zgadywanej we froncie, pulsu dozorcy
 * branego z pierwszego wiersza zamiast z najstarszego, godzin ciszy zapisywanych
 * przy kazdym wcisnietym znaku i - najgrozniejsze - zapisu ciszy przy
 * NIEWCZYTANYCH ustawieniach, ktory kazal serwerowi dopelnic reszte pol
 * wartosciami domyslnymi i kasowal realna konfiguracje.
 *
 * CZEGO NIE PILNUJE
 *
 *   - KONTRAKTU API. Odpowiedzi sa podstawione (`stend_ui_dane.mjs`); gdy router
 *     zmieni ksztalt, stend nadal bedzie zielony na danych, ktorych API juz nie
 *     wysyla. Kontraktu pilnuja testy pytest czytajace routery.
 *   - wygladu, ukladu, szerokosci wykresow - od tego jest smoke.
 *   - logowania, sesji, uprawnien, bazy.
 *   - sciezki 401: warstwa HTTP traktuje ja jako wygasla sesje i wyrzuca na
 *     ekran logowania, wiec awarie udaje sie tu kodem 500.
 */
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { BAZA, BRZEGI, TERAZ } from './stend_ui_dane.mjs';

let chromium;
try {
  ({ chromium } = await import('playwright'));
} catch {
  console.error('Brak zaleznosci `playwright`. Zainstaluj w katalogu glownym repo:\n'
    + '    npm install --no-save playwright && npx playwright install chromium');
  process.exit(2);
}

const KORZEN = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const DIST = process.env.UI_DIST || path.join(KORZEN, 'web/dist');
const PORT = Number(process.env.UI_PORT || 5199);
const TABS = ['Stan', 'Stopień nauki', 'Ruchy', 'Źródła', 'Ryzyko', 'Ustawienia', 'Profil'];

/* Tresc bledu odsylana przy kazdej podstawionej awarii. Musi byc na tyle
   osobliwa, zeby jej obecnosc na ekranie nie mogla byc przypadkiem: to jest
   dowod, ze komponent PRZEPUSCIL blad do uzytkownika, a nie go polknal. */
const SYGNATURA = 'awaria testowa 8f3c';

if (!fs.existsSync(path.join(DIST, 'index.html'))) {
  console.error(`Nie ma zbudowanego interfejsu w ${DIST}\n`
    + '    zbuduj:  cd web && npm run build');
  process.exit(2);
}

const TYP = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
              '.svg': 'image/svg+xml', '.json': 'application/json' };

let TRASY = {};
const srv = http.createServer((req, res) => {
  const sciezka = new URL(req.url, 'http://x').pathname;
  if (sciezka.startsWith('/api/')) {
    const klucz = Object.keys(TRASY).find((k) => sciezka === k)
               ?? Object.keys(BAZA).find((k) => sciezka === k);
    const dane = klucz === undefined ? undefined : (TRASY[klucz] ?? BAZA[klucz]);
    if (dane === undefined) {
      res.writeHead(500, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ detail: SYGNATURA }));
    }
    res.writeHead(200, { 'content-type': 'application/json' });
    return res.end(JSON.stringify(dane));
  }
  const plik = path.join(DIST, sciezka === '/' ? '/index.html' : sciezka);
  if (!plik.startsWith(DIST) || !fs.existsSync(plik)) { res.writeHead(404); return res.end(); }
  res.writeHead(200, { 'content-type': TYP[path.extname(plik)] || 'application/octet-stream' });
  res.end(fs.readFileSync(plik));
});
await new Promise((r) => srv.listen(PORT, r));

let bledy = 0;
const ok = (warunek, opis, detal = '') => {
  console.log(`${warunek ? '  ok  ' : '  BLAD'} ${opis}${detal ? ' — ' + detal : ''}`);
  if (!warunek) bledy++;
};
const jest = (txt, s, opis) => ok(txt.includes(s), opis, txt.includes(s) ? '' : `brak „${s}"`);
const niema = (txt, s, opis) => ok(!txt.includes(s), opis, txt.includes(s) ? `jest „${s}"` : '');

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 1200 } });
const page = await ctx.newPage();
const jsErr = [];
page.on('pageerror', (e) => jsErr.push(e.message));

const zakladka = async (nazwa) => {
  await page.click(`.nav-tab:has-text("${nazwa}")`);
  await page.waitForTimeout(700);
  return page.locator('.app-container').innerText();
};
const wejdz = async () => {
  await page.goto(`http://localhost:${PORT}/`, { waitUntil: 'networkidle' });
  await page.waitForSelector('.nav-tab', { timeout: 20000 });
};

try {

// =============================================== 1: kazdy odczyt danych pada
console.log('\n--- SCENARIUSZ 1: kazdy odczyt danych pada (500) ---');
TRASY = {};
await wejdz();

for (const tab of TABS) {
  const txt = await zakladka(tab);

  // (a) Zaden pusty stan nie moze byc TWIERDZENIEM. "Brak czegos" po nieudanym
  //     odczycie to zdanie o systemie postawione na podstawie awarii.
  const puste = await page.locator('.empty').allInnerTexts();
  const twierdzace = puste.filter((s) => /^\s*Brak\b/i.test(s));
  ok(twierdzace.length === 0, `"${tab}": zaden pusty stan nie twierdzi "Brak…"`,
     twierdzace.join(' | ').slice(0, 120));

  // (b) Blad MUSI dojsc do uzytkownika. Sygnatura przyszla w tresci odpowiedzi
  //     500 - jesli jej nie widac, komponent polknal wyjatek.
  jest(txt, SYGNATURA, `"${tab}": tresc bledu z serwera jest widoczna`);
}

// Nieobsluzony `await` w komponencie konczy sie odrzuceniem promisy bez catcha.
ok(jsErr.length === 0, 'brak nieobsluzonych wyjatkow JS przy awarii API',
   jsErr.slice(0, 3).join(' | '));

// Zdania, ktore w tym scenariuszu nie maja prawa paesc - kazde bylo realna wada.
const ustawienia = await zakladka('Ustawienia');
niema(ustawienia, 'nie ma dostępu do salda',
      'Ustawienia: brak zapewnienia o stanie systemu po awarii');
const profil = await zakladka('Profil');
niema(profil, 'Wyłączona', 'Profil: nieodczytane 2FA nie jest "Wyłączona"');
const ryzyko = await zakladka('Ryzyko');
niema(ryzyko, 'brak dozorcy', 'Ryzyko: awaria odczytu pulsu to nie "brak dozorcy"');
// Audyt 2026-10-09, W-1: hamulec nie moze zalezec od udanego odczytu historii.
jest(ryzyko, 'Zatrzymaj handel', 'Ryzyko: reczny kill-switch dostepny takze po awarii odczytu');
// Audyt 2026-10-10, S2-3: nieodczytane ustawienie likwidacji to NIE "otwarte zostaja".
jest(ryzyko, 'nie wiadomo, czy zamknie otwarte',
     'Ryzyko: awaria odczytu skutku kill-switcha nie udaje lagodnej wersji');
niema(ryzyko, 'otwarte zostają', 'Ryzyko: brak pulsu nie obiecuje, ze pozycje zostana');
const zrodla = await zakladka('Źródła');
niema(zrodla, 'rusza po wpisaniu klucza',
      'Zrodla: po awarii nie kaze wpisywac klucza, ktory moze byc wpisany');

// ===================================================== 2: dane brzegowe (200)
console.log('\n--- SCENARIUSZ 2: dane poprawne, ale brzegowe ---');
TRASY = BRZEGI;
await page.reload({ waitUntil: 'networkidle' });
await page.waitForSelector('.nav-tab', { timeout: 20000 });

let t = await zakladka('Ryzyko');
jest(t, 'brak pomiaru', 'Ryzyko: limit bez `as_of` to brak pomiaru, nie zero');
jest(t, 'wyłączony', 'Ryzyko: limit `enabled: false` jest oznaczony');
jest(t, 'w walucie rozliczenia', 'Ryzyko: jednostka `quote_ccy` nie udaje USD');
niema(t, '15 000,00 USD', 'Ryzyko: front nie zgaduje waluty');
jest(t, 'najstarszy puls 170s temu', 'Ryzyko: puls liczony z NAJSTARSZEGO dozorcy');
niema(t, 'puls 5s temu', 'Ryzyko: nie bierze pierwszego po ORDER BY nazwa');
jest(t, 'Historia kill-switcha została ucięta',
     'Ryzyko: ucieta historia KS jest zglaszana, a nie przemilczana');
const paski = await page.locator('.bar-fill').count();
ok(paski === 1, 'Ryzyko: pasek tylko dla limitu zmierzonego i wlaczonego', `${paski} z 3`);

// AUTOMAT TRZYMA, RECZNEGO NIE MA (audyt 2026-10-09, W-1). Do tej poprawki
// przycisk znikal przy kazdym aktywnym zdarzeniu, a `max_daily_loss` z regula
// `po_ustaniu` po karencji sam wznawial handel.
TRASY = {
  ...BRZEGI,
  '/api/engines/crypto/risk/limits': BRZEGI['/api/engines/crypto/risk/limits'].map(
    (l) => (l.name === 'max_daily_loss' ? { ...l, enabled: true, wznowienie: 'po_ustaniu' } : l)),
  '/api/engines/crypto/risk/killswitch': [{
    id: 900, triggered_at: TERAZ(), reason: 'Strata dzienna 3,4% przekroczyła 3%',
    limit_name: 'max_daily_loss', released_at: null, released_by: null,
    release_note: null, released_auto: false, triggered_by: null, active: true,
  }],
  // Silnik z `--likwiduj-po-killswitchu` (audyt 2026-10-10, S2-3).
  '/api/engines/crypto/risk/killswitch/skutek': {
    likwiduje: true, mode: 'demo', as_of: TERAZ(),
    opis: 'Silnik przestanie otwierać pozycje i ZAMKNIE WSZYSTKIE OTWARTE POZYCJE '
      + 'ZLECENIEM RYNKOWYM (stawka taker + niezmierzony poślizg).',
  },
};
await page.reload({ waitUntil: 'networkidle' });
await page.waitForSelector('.nav-tab', { timeout: 20000 });
t = await zakladka('Ryzyko');
jest(t, 'Zatrzymaj handel', 'Ryzyko: reczny kill-switch widoczny, gdy trzyma automat');
jest(t, 'zwolni się sam', 'Ryzyko: automat z regula wznowienia nie udaje recznego');
niema(t, 'Odblokowanie wymaga ręcznego potwierdzenia',
      'Ryzyko: przy automacie nie ma zapewnienia, ze handel stoi do recznego zwolnienia');
jest(t, 'ZAMYKA wszystkie pozycje rynkowo',
     'Ryzyko: przy wlaczonej likwidacji karta mowi o sprzedazy, nie o wstrzymaniu');
// Audyt 2026-10-10, N2-2: klasa `.btn` nie istniala - surowy przycisk przegladarki.
const tloKs = await page.getByRole('button', { name: 'Zatrzymaj handel…' })
  .evaluate((b) => getComputedStyle(b).backgroundColor);
ok(tloKs !== 'rgba(0, 0, 0, 0)' && !/239, 239, 239|240, 240, 240/.test(tloKs),
   'Ryzyko: przycisk kill-switcha ma styl panelu, nie domyslny przegladarki', tloKs);
await page.getByRole('button', { name: 'Zatrzymaj handel…' }).click();
t = await page.locator('.app-container').innerText();
jest(t, 'ZLECENIEM RYNKOWYM', 'Ryzyko: formularz zatrzymania mowi o zamknieciu pozycji');
jest(t, 'Włącz kill-switch i zamknij pozycje',
     'Ryzyko: przycisk nazywa sprzedaz, ktora uruchomi');
TRASY = BRZEGI;
await page.reload({ waitUntil: 'networkidle' });
await page.waitForSelector('.nav-tab', { timeout: 20000 });

t = await zakladka('Źródła');
jest(t, 'Klucz jest wpisany, ale dziś nie było ani jednego wywołania',
     'Zrodla: zero wywolan przy wpisanym kluczu nazwane poprawnie');
niema(t, 'rusza po wpisaniu klucza', 'Zrodla: nie kaze wpisywac wpisanego klucza');
jest(t, 'Limit dobowy', 'Zrodla: karta kosztu nie znika przy zerze wywolan');
jest(t, '3.21 USD przez 30 dni', 'Zrodla: koszt 30-dniowy jest widoczny');
jest(t, 'żaden limit dobowy nie jest znany', 'Zrodla: brak wykorzystania nie udaje 0%');
jest(t, 'wyłączony', 'Zrodla: wylaczony kanal RSS jest oznaczony');
const ocena = await page.locator('.row-item:has-text("Artykul bez oceny")').innerText();
ok(!/pewność\s*\n?\s*0%/.test(ocena), 'Zrodla: brak oceny NIE renderuje sie jako 0%',
   ocena.replace(/\n/g, ' ').slice(0, 90));

t = await zakladka('Profil');
jest(t, 'Włączona', 'Profil: wlaczone 2FA jest pokazane jako wlaczone');
jest(t, 'nie częściej niż co 20 s', 'Profil: odstep 20 s nie zaokragla sie do 0 min');
niema(t, 'co 0 min', 'Profil: nie ma odstepu "0 min"');
const czasy = await page.locator('input[type="time"]')
  .evaluateAll((xs) => xs.map((x) => `${x.value}:${x.disabled}`));
ok(czasy.join(',') === '22:00:false,07:00:false,08:30:false',
   'Profil: godziny ciszy pochodza z serwera, pola odblokowane', czasy.join(','));

t = await zakladka('Stan');
ok(/24 h\s*—/.test(t), 'Kapital: brak zmiany dobowej to "—"', (t.match(/24 h[^\n]*/) || [])[0]);
niema(t, '24 h +—', 'Kapital: brak pomiaru nie jest zielonym zyskiem');

// ============================== 3: godziny ciszy - kiedy leci zapis i co po bledzie
console.log('\n--- SCENARIUSZ 3: zapis godzin ciszy ---');
const puty = [];
await page.route('**/api/notifications/quiet', async (route) => {
  if (route.request().method() !== 'PUT') return route.continue();
  puty.push(JSON.parse(route.request().postData()));
  return route.fulfill({ status: 500, contentType: 'application/json',
                         body: JSON.stringify({ detail: 'baza nie odpowiada' }) });
});
await zakladka('Profil');
const pole = page.locator('input[type="time"]').first();
await pole.click();
await page.keyboard.type('21');            // godzina zmieniona, minuty jeszcze nie
await page.waitForTimeout(300);
ok(puty.length === 0, 'Cisza: samo wpisywanie NIE wysyla zapisu',
   `${puty.length} PUT-ow, pole=${await pole.inputValue()}`);
await page.keyboard.type('30');
await page.locator('input[type="time"]').nth(1).click();   // wyjscie z pola
await page.waitForTimeout(600);
ok(puty.length === 1, 'Cisza: zapis idzie RAZ, po wyjsciu z pola', `${puty.length} PUT-ow`);
ok(puty[0] && puty[0].quiet_to === '07:00' && puty[0].digest_at === '08:30',
   'Cisza: PUT niesie pelny komplet pol', JSON.stringify(puty[0]));
ok((await pole.inputValue()) === '22:00',
   'Cisza: nieudany zapis cofa wartosc na ekranie', await pole.inputValue());
jest(await page.locator('.app-container').innerText(), 'baza nie odpowiada',
     'Cisza: blad zapisu jest widoczny');

// ============== 4: NAJGROZNIEJSZE - zapis ciszy przy nieodczytanych ustawieniach
console.log('\n--- SCENARIUSZ 4: nieodczytana cisza a zapis (wada niszczaca dane) ---');
const puty4 = [];
await page.unroute('**/api/notifications/quiet');
await page.route('**/api/notifications/quiet', async (route) => {
  if (route.request().method() === 'PUT') {
    puty4.push(JSON.parse(route.request().postData()));
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' });
  }
  // Odczyt pada: `quiet` zostaje nieznane.
  return route.fulfill({ status: 500, contentType: 'application/json',
                         body: JSON.stringify({ detail: 'awaria odczytu ciszy' }) });
});
await zakladka('Stan');
await zakladka('Profil');
const czasy4 = page.locator('input[type="time"]');
ok(await czasy4.evaluateAll((xs) => xs.every((x) => x.disabled)),
   'Cisza bez odczytu: pola sa zablokowane');
try {
  await czasy4.first().click({ timeout: 1500 });
  await page.keyboard.type('0330');
  await czasy4.nth(2).click({ timeout: 1500 });
} catch { /* pole zablokowane - o to wlasnie chodzi */ }
await page.waitForTimeout(500);
ok(puty4.length === 0, 'Cisza bez odczytu: zaden zapis nie idzie do serwera',
   puty4.length ? JSON.stringify(puty4[0]) : '');
// Nawet gdyby kiedys poszedl: niepelne ciało kaze serwerowi (QuietBody) dopelnic
// reszte pol DOMYSLNYMI i kasuje realna konfiguracje ciszy.
const niepelny = puty4.find((p) => !('quiet_to' in p) || !('digest_at' in p));
ok(!niepelny, 'Cisza bez odczytu: zaden PUT nie jest niepelny',
   niepelny ? JSON.stringify(niepelny) : '');

/* ------------------------------------------------------------------------
 * MIEJSCE NA KOLEJNE SCENARIUSZE
 *
 * 5: /learning/walk-forward - okna posortowane po `test_from` i pochodzace
 *    z JEDNEGO przebiegu treningowego (czeka na poprawke po stronie API).
 *    Podstawic odpowiedz w `stend_ui_dane.mjs`, wejsc na "Stopień nauki".
 * ------------------------------------------------------------------------ */

} catch (e) {
  ok(false, 'stend przerwany wyjatkiem', e.message);
} finally {
  await browser.close();
  srv.close();
}

console.log(bledy === 0 ? '\nSTEND UI: PRZESZEDL' : `\nSTEND UI: ${bledy} BLEDOW`);
process.exit(bledy === 0 ? 0 : 1);
