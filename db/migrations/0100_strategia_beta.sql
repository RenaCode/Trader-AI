-- 0100: regula "beta z filtrem trendu" (SPY, SMA 10 mies.) na demo equity.
--
-- DECYZJA WLASCICIELA D-3 (2026-10-18; uzasadnienie: renacode-infra
-- docs/projekty/trader/hipotezy.md, D-3 i H-16). Silnik demo equity gra regula
-- `services/engine/strategia_beta.py` (`loop.py --strategia beta`) zamiast
-- modelu-kandydata. To jest ALOKACJA - swiadome trzymanie bety rynku - a nie
-- hipoteza przewagi.
--
-- CO ROBI TA MIGRACJA
--
--   1. `journal.decision.strategia` dopuszcza `beta_filtr_trendu` (CHECK z 0091).
--   2. `journal.decision.powod_holdu` dopuszcza cztery powody reguly
--      (`strategia_beta.POWODY_HOLDU`). Zbior jest zamkniety (0040 -> ... ->
--      0099), wiec bez tego pierwszy taki hold wywrocilby zapis decyzji.
--      KOLEJNOSC WDROZENIA: migracja PRZED kodem petli.
--   3. SPY na Alpace `active = true`, jesli nie jest (dzis jest - 0047 wpisala
--      go z domyslnym `active`; UPDATE jest wtedy pusty). `tradeable` SWIADOMIE
--      bez zmian: uniwersum decyzyjne i jego odcisk (`uniwersum.py`) licza sie
--      z `active AND tradeable`, wiec `active` przy `tradeable = false` nie
--      zmienia ani zbioru ensemble, ani nazw cech `rel_strength#...`, na
--      ktorych liczy trener. Historie 1h i arkusz zbieraja wszystkie `active`
--      (`backfill.py`, `market_worker.py`) - SPY juz w nich jest.
--   4. LIMITY RYZYKA KONTA equity/demo - WYLACZNIE tego jednego wiersza
--      silnika i trybu. Crypto i kazde `live` nietkniete.
--   5. WERYFIKACJA limitow (blok `DO` na koncu): dokladnie 5 wierszy
--      z oczekiwanymi wartosciami, inaczej RAISE i wycofanie calosci.
--   6. Typ powiadomienia `engine.beta_bez_historii` (czujka `dozor --co beta`).
--
-- STRAZNIK ODWROTU (`loop.limity_beta_poza_beta`): strategia inna niz beta na
-- equity/demo przy `max_total_exposure` > 15 pct_equity ma wejscia wstrzymane
-- (`wstrzymanie_ryzykiem`), wiec przywrocenie ensemble bez odwrocenia tych
-- limitow nie zagra 9,7% na pozycje i 97% lacznie.
--
-- DLACZEGO LIMITY MUSZA SIE ZMIENIC (rachunek mechaniczny, nie wynik badania)
--
-- Regula trzyma 95% kapitalu w jednym instrumencie. Przy dotychczasowych
-- limitach to nie dziala z samej arytmetyki:
--
--   * `max_position_notional` 5% (`downsize`) obcina kazde wejscie do 5%
--     kapitalu, a `max_total_exposure` 15% (`block`) i tak nie wpuscilby
--     wiecej - regula grajaca 95% gralaby 5%;
--   * `max_daily_loss` 2% (kill-switch): przy 95% w indeksie akcji strata
--     konta >2% w jeden dzien to zwykla sesja z ruchem indeksu ok. -2,1%,
--     a nie awaria - kill-switch odpalalby sie na zwyklej zmiennosci;
--   * `max_drawdown` 10% i `min_equity` 95 000 (kill-switch, `min_equity`
--     zwalniany wylacznie recznie): obsuniecia indeksu rzedu 10-20% sa
--     typowe, a filtr miesieczny z definicji reaguje dopiero na zamknieciu
--     miesiaca. Kill-switch zamykalby konto w srodku zwyklej korekty, ZANIM
--     regula zdazy zadzialac - czyli zamienialby alokacje w sprzedaz po
--     spadku.
--
-- NOWE WARTOSCI
--
--   max_total_exposure     97% kapitalu  (95% reguly + bufor na roznice ceny
--   max_position_notional  97% kapitalu   limitu i ruch ceny miedzy decyzja
--                                          a wysylka; wlasny sufit reguly
--                                          95% zostaje w kodzie)
--   max_daily_loss         10%           (siatka na awarie, nie na sesje)
--   max_drawdown           40%           (siatka na awarie petli/regul -
--                                          nie na zwykle obsuniecie indeksu)
--   min_equity             60 000 USD    (przy kapitale 100 000: spadek SPY
--                                          o 30% przy 95% w rynku to ok.
--                                          -28,5% konta, czyli 71 500 -
--                                          ponad podloga)
--
-- `max_leverage` 1x i `max_orders_per_hour` zostaja: 95% to mniej niz 1x,
-- a regula sklada najwyzej kilka zlecen na dobe.
--
-- ODWROTNOSC (powrot do ensemble): wartosci sprzed tej migracji to
-- pct_equity 5 / 15, pct 2 / 10, quote_ccy 95 000 (0008, 0028).
--
-- BLOKADA: jak w 0075 - po 5 s rezygnujemy, migracja pada, `api` restartuje
-- sie i probuje ponownie.

BEGIN;

SET LOCAL lock_timeout = '5s';

ALTER TABLE journal.decision
    DROP CONSTRAINT IF EXISTS decision_strategia_chk;

ALTER TABLE journal.decision
    ADD CONSTRAINT decision_strategia_chk CHECK (
        strategia IS NULL OR strategia IN ('ensemble', 'momentum_filtr_trendu',
                                           'beta_filtr_trendu'));

COMMENT ON COLUMN journal.decision.strategia IS
    'Reguła, która wybrała akcję (loop.STRATEGIE): ensemble = wagi z aktywnej '
    'wersji modelu albo założenia początkowe; momentum_filtr_trendu = zamrożona '
    'reguła R4 (H-3) na demo, pomiar egzekucji; beta_filtr_trendu = SPY z filtrem '
    'SMA 10 mies. na demo equity, alokacja (D-3). NULL = decyzja sprzed 0091 (ensemble).';

ALTER TABLE journal.decision
    DROP CONSTRAINT IF EXISTS decision_powod_holdu_chk;

ALTER TABLE journal.decision
    ADD CONSTRAINT decision_powod_holdu_chk CHECK (
        powod_holdu IS NULL OR powod_holdu IN (
            'slaby_wynik', 'niska_pewnosc', 'short_niewykonalny',
            'trzyma_pozycje', 'wstrzymanie_ryzykiem', 'rezim_bez_progu',
            'szeroki_spread', 'decyzja_przeterminowana',
            'filtr_trendu', 'poza_skladem', 'poza_decyzja_dobowa',
            'ponizej_jednej_akcji', 'swieca_przed_sesja',
            'tor_zamkniety',
            'filtr_trendu_spy', 'historia_filtra_niepelna', 'czeka_na_sesje',
            'poza_instrumentem_reguly'));

UPDATE ref.instrument i
   SET active = true
  FROM ref.venue v
 WHERE v.id = i.venue_id
   AND v.code = 'alpaca'
   AND i.engine = 'equity'
   AND i.symbol = 'SPY'
   AND NOT i.active;

-- Jednostka ustawiana RAZEM z wartoscia: liczba bez jednostki to inny limit
-- (0028: 15 jako `quote_ccy` to 15 USD, nie 15%).
UPDATE risk.limit_def SET value = 97, unit = 'pct_equity', updated_at = now()
 WHERE engine = 'equity' AND mode = 'demo' AND scope = 'engine'
   AND name IN ('max_total_exposure', 'max_position_notional');

UPDATE risk.limit_def SET value = 10, unit = 'pct', updated_at = now()
 WHERE engine = 'equity' AND mode = 'demo' AND scope = 'engine'
   AND name = 'max_daily_loss';

UPDATE risk.limit_def SET value = 40, unit = 'pct', updated_at = now()
 WHERE engine = 'equity' AND mode = 'demo' AND scope = 'engine'
   AND name = 'max_drawdown';

UPDATE risk.limit_def SET value = 60000, unit = 'quote_ccy', updated_at = now()
 WHERE engine = 'equity' AND mode = 'demo' AND scope = 'engine'
   AND name = 'min_equity';

-- WERYFIKACJA (recenzja PR #42, B3). UPDATE, ktory nie trafil w zaden wiersz
-- (brak wiersza, literowka w nazwie, inny `scope`), konczy sie sukcesem -
-- migracja przeszlaby, a regula grajaca 95% gralaby na starych limitach 5%
-- albo kill-switch zamykalby konto na zwyklej sesji. Po UPDATE-ach musi byc
-- DOKLADNIE piec wierszy tych nazw, kazdy z oczekiwana wartoscia, jednostka
-- i wlaczony; inaczej cala migracja sie wycofuje.
DO $$
DECLARE
    wszystkie int;
    zgodne    int;
BEGIN
    SELECT count(*),
           count(*) FILTER (WHERE enabled AND (name, value, unit) IN (
               ('max_total_exposure',    97,    'pct_equity'),
               ('max_position_notional', 97,    'pct_equity'),
               ('max_daily_loss',        10,    'pct'),
               ('max_drawdown',          40,    'pct'),
               ('min_equity',            60000, 'quote_ccy')))
      INTO wszystkie, zgodne
      FROM risk.limit_def
     WHERE engine = 'equity' AND mode = 'demo' AND scope = 'engine'
       AND name IN ('max_total_exposure', 'max_position_notional',
                    'max_daily_loss', 'max_drawdown', 'min_equity');
    IF wszystkie <> 5 OR zgodne <> 5 THEN
        RAISE EXCEPTION '0100: limity equity/demo po UPDATE: % wierszy, % zgodnych '
                        '(oczekiwane 5 i 5: max_total_exposure i '
                        'max_position_notional 97 pct_equity, max_daily_loss 10 pct, '
                        'max_drawdown 40 pct, min_equity 60000 quote_ccy, enabled)',
                        wszystkie, zgodne;
    END IF;
END
$$;

-- CZUJKA "BETA BEZ HISTORII SPY" (recenzja PR #42, P1; `dozor --co beta`).
-- Niepelna historia SPY daje hold `historia_filtra_niepelna` - cel gotowka,
-- czyli silnik bez wejsc wygladajacy jak regula poza rynkiem.
INSERT INTO notify.event_type
    (code, label, description, category, default_severity, always_on, default_cooldown_s)
VALUES (
    'engine.beta_bez_historii',
    'Reguła beta bez historii SPY',
    'Silnik equity z regułą beta przez więcej niż jedną sesję zapisuje hold ' ||
    '`historia_filtra_niepelna`: brakuje zamknięć miesięcznych SPY, filtr trendu ' ||
    'jest nieznany i reguła trzyma gotówkę z powodu braku danych, nie rynku. ' ||
    'Sprawdź `market.candle` SPY (1h) i backfill.',
    'ryzyko', 'critical', true, 0)
ON CONFLICT (code) DO NOTHING;

INSERT INTO notify.preference (user_id, event_code, enabled, channels)
SELECT u.id, 'engine.beta_bez_historii', true, ARRAY['inapp']::notify.channel_kind[]
  FROM app."user" u
 WHERE u.is_active
ON CONFLICT (user_id, event_code) DO NOTHING;

COMMIT;
