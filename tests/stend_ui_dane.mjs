/* Odpowiedzi API podstawiane stendowi z `stend_ui.spec.mjs`.
 *
 * Trzymane osobno, bo to SA dane testu, nie jego logika. Scenariusz ma sie
 * czytac jako lista twierdzen o interfejsie, a nie jako sciana JSON-a.
 *
 * Uwaga przy dopisywaniu: te ksztalty maja odpowiadac temu, co zwracaja
 * routery w `services/api/routers/`. Stend NIE sprawdza kontraktu - podstawia
 * go. Gdy router zmieni ksztalt odpowiedzi, stend tego nie zauwazy i bedzie
 * dalej swiecil na zielono na danych, ktorych API juz nie wysyla. Kontraktu
 * pilnuja testy pytest czytajace routery; tutaj trzeba go zaktualizowac RECZNIE.
 */

/* Odpowiedzi wspolne dla wszystkich scenariuszy: bez nich aplikacja nie
   wychodzi poza ekran logowania, wiec nie da sie obejrzec zadnej zakladki.
   401 celowo NIE jest uzywane jako awaria w scenariuszach - warstwa HTTP
   traktuje je jako wygasla sesje i wyrzuca na ekran logowania (api.js). */
export const BAZA = {
  '/api/auth/me': { login: 'admin', role: 'owner', totp_enabled: true },
  '/api/engines': [
    { engine: 'crypto', venue: 'bybit', instruments: 12, is_24_7: true },
  ],
  '/api/health': { migrations: 42 },
  '/api/notifications/count': { unread: 0, critical: 0 },
};

const TERAZ = () => new Date().toISOString();
const PRZED = (ms) => new Date(Date.now() - ms).toISOString();

/* Scenariusz brzegowy: wszystko odpowiada 200, ale niesie wartosci, przy
   ktorych interfejs latwo zaczyna TWIERDZIC wiecej, niz wie. Kazdy wpis
   odpowiada konkretnej wadzie z audytu 2026-09-22. */
export const BRZEGI = {
  // Limit NIGDY NIEZMIERZONY (`as_of: null`) obok wylaczonego i obok
  // zmierzonego. API oddaje dzis `used`/`utilization` jako 0.0 takze wtedy,
  // gdy wiersza pomiaru nie ma - jedynym swiadkiem jest `as_of`.
  '/api/engines/crypto/risk/limits': [
    { id: 1, name: 'max_total_exposure', scope: 'portfolio', symbol: null,
      threshold: 15000, unit: 'quote_ccy', action: 'block', enabled: true,
      wznowienie: null, max_wznowien: null, used: 0.0, utilization: 0.0,
      as_of: null },
    { id: 2, name: 'max_daily_loss', scope: 'portfolio', symbol: null,
      threshold: 500, unit: 'pct_equity', action: 'killswitch', enabled: false,
      wznowienie: 'reczne', max_wznowien: 1, used: 0.0, utilization: 0.0,
      as_of: TERAZ() },
    { id: 3, name: 'max_orders_per_hour', scope: 'portfolio', symbol: null,
      threshold: 20, unit: 'count', action: 'block', enabled: true,
      wznowienie: null, max_wznowien: null, used: 4, utilization: 0.2,
      as_of: PRZED(90_000) },
  ],
  '/api/engines/crypto/risk/violations': [],
  // Historia doklejona do sufitu limitu: aktywny bezpiecznik starszy niz
  // ostatnie 100 zdarzen bylby stad NIEWIDOCZNY.
  '/api/engines/crypto/risk/killswitch': Array.from({ length: 100 }, (_, i) => ({
    id: i + 1, triggered_at: PRZED(i * 3_600_000), reason: `zdarzenie ${i + 1}`,
    limit_name: 'max_daily_loss', released_at: TERAZ(), released_by: 'admin',
    release_note: 'powod testowy', released_auto: false, active: false,
  })),
  // Dwa dozorce, OBA zywe, ale o roznym wieku pulsu. Zdanie w interfejsie mowi
  // o wszystkich, wiec musi je opisywac ten, ktory bije najrzadziej (170 s),
  // a nie pierwszy po ORDER BY nazwa (5 s).
  '/api/engines/crypto/risk/dozorca': [
    { nazwa: 'a:crypto:limity', last_beat_at: TERAZ(), sekund_temu: 5, zyje: true, detail: null },
    { nazwa: 'z:crypto:kapital', last_beat_at: TERAZ(), sekund_temu: 170, zyje: true, detail: null },
  ],
  // Zero wywolan modelu PRZY WPISANYM KLUCZU - realna awaria z 09.09.2026
  // (konto u dostawcy bez srodkow). `wykorzystanie: null` znaczy, ze limitu
  // dobowego nie da sie odniesc do niczego.
  '/api/api-spend': {
    limit_dobowy_usd: 5.0, okres_dni: 30, koszt_okresu_usd: 3.21,
    dzis: { koszt_usd: 0, wywolania: 0, wykorzystanie: null }, dobowe: [],
  },
  '/api/settings/analiza-tryb': {
    aktywny: true, dostawca: 'anthropic', model_triazu: 'haiku',
    model_analizy: 'sonnet', mieszanka: false,
  },
  // Kanal WYLACZONY, ale ze statusem `ok` i odczytem sprzed trzech dni - to
  // ostatni odczyt SPRZED wylaczenia. Stary sukces nie jest dowodem pracy.
  '/api/feeds': [
    { id: 1, name: 'Kanal wylaczony', url: 'https://przyklad.test/rss',
      engine: 'crypto', enabled: false, poll_interval_s: 300,
      last_polled_at: PRZED(3 * 86_400_000), last_status: 'ok', last_error: null,
      articles_seen: 10, articles_kept: 1, last_24h: 0, keep_rate: 0.1 },
  ],
  // Artykul z ocena sily, ale BEZ pewnosci i nowosci (LEFT JOIN nie dal
  // wiersza analizy). Brak oceny to nie ocena rowna zeru.
  '/api/news': [
    { id: 1, title: 'Artykul bez oceny', url: 'https://przyklad.test/a',
      source: 'Zrodlo', at: TERAZ(), ingested_at: TERAZ(), lag_s: 12,
      direction: 'bullish', magnitude: 0.6, confidence: null, novelty: null,
      horizon: null, reasoning: null, model: null, symbols: [] },
  ],
  '/api/account/profile': {
    login: 'admin', display_name: null, timezone: 'Europe/Warsaw', locale: 'pl',
    role: 'owner', created_at: TERAZ(), last_login_at: TERAZ(),
    totp_enabled: true, backup_codes_left: 8, backup_codes_total: 10,
  },
  // Odstep 20 s: zaokraglenie do pelnych minut robilo z niego "co 0 min",
  // czyli komunikat, ze odstepu nie ma.
  '/api/notifications/preferences': [
    { code: 'ryzyko.test', label: 'Zdarzenie testowe', description: 'opis',
      category: 'ryzyko', severity: 'warning', enabled: true, always_on: false,
      channels: ['inapp'], cooldown_s: 20 },
  ],
  '/api/notifications/channels': [],
  '/api/notifications/quiet': {
    quiet_from: '22:00', quiet_to: '07:00', timezone: 'Europe/Warsaw',
    daily_digest: true, digest_at: '08:30',
  },
  // Kapital znany, ale zmiana dobowa NIE - `null >= 0` jest w JS prawda, wiec
  // brak pomiaru potrafi sie wyrenderowac jako zielony zysk.
  '/api/kapital/laczny': {
    silniki: ['crypto'], punkty: [],
    zmiany: {
      crypto: { teraz: 1000, doba: null, doba_pct: null, od_startu: 12, od_startu_pct: 1.2 },
      razem: { teraz: 1000, doba: null, doba_pct: null, od_startu: 12, od_startu_pct: 1.2 },
    },
  },
};
