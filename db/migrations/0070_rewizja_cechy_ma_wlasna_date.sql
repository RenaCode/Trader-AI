-- 0070: rewizja wartości cechy ma własną datę — `feat.feature_value.revised_at`.
--
-- CO BYŁO ZŁE
--
-- `features.insert_values` poprawia istniejące wiersze (ON CONFLICT DO UPDATE,
-- od 2026-09-06 — świece zapisane w trakcie trwania zaniżały ATR i całą resztę).
-- Przy poprawce wpisywał jednak `ingested_at = znane_od(event_time)`, czyli tę
-- samą datę, co przy pierwszym zapisie. Rewizja wyglądała więc w bazie dokładnie
-- jak wartość znana od zamknięcia świecy, a oba strażniki bitemporalne —
-- `train.load_panel` (`ingested_at <= event_time + 2 świece`) i
-- `loop.sprawdz/kontrola_datowania` — przepuszczały ją Z DEFINICJI. Nie dało się
-- odróżnić „silnik widział tę liczbę” od „silnik widział inną, tę wpisaliśmy
-- później” — a to jest dokładnie rozjazd między tym, na czym silnik decydował,
-- a tym, na czym trener się uczy.
--
-- Samo `ingested_at = now()` przy poprawce byłoby gorsze: `load_panel` wyrzuciłby
-- każdą przeliczoną wstecz cechę i trener uczyłby się na dziurach w miejscach,
-- gdzie dane były ZŁE — czyli selekcja próby po błędzie.
--
-- CO ZNACZY KOLUMNA
--
--   `ingested_at` — bez zmian: chwila, od której wartość była DO WYLICZENIA
--                   z danych giełdy (`znane_od`), także dla backfillu.
--   `revised_at`  — chwila (zegar ścienny, `now()`) OSTATNIEGO nadpisania wartości
--                   przez `insert_values`. NULL = wiersz nigdy nie był poprawiony
--                   ALBO był poprawiony przed tą migracją (wtedy daty nie znamy
--                   i nie zgadujemy jej — patrz niżej).
--
-- BEZ BACKFILLU
--
-- Poprawek sprzed migracji nie da się odtworzyć: stara wartość została
-- nadpisana w miejscu, bez śladu i bez daty (`ingested_at` też nadpisane tą
-- samą, wyliczaną datą). Wpisanie tu czegokolwiek byłoby zgadywaniem, a NULL
-- uczciwie znaczy „nie wiemy o żadnej rewizji”. Pomiar na produkcji 2026-10-03:
-- 10 985 336 wierszy, 0 z `ingested_at > event_time + 30 dni` — nie ma nawet
-- poszlaki do odtworzenia.
--
-- BEZPIECZEŃSTWO NA DZIAŁAJĄCEJ BAZIE
--
-- `feat.feature_value` to hypertable z kompresją (0004, polityka 30 dni);
-- na produkcji 2026-10-03: 255 chunków, w tym 249 skompresowanych, 252 MB.
--
--   * ADD COLUMN bez DEFAULT, NULL-owalna: zmiana wyłącznie w katalogu, bez
--     przepisywania wierszy — ani w chunkach zwykłych, ani w skompresowanych.
--     TimescaleDB (>= 2.1, produkcja: 2.29.2) wspiera ADD COLUMN na hypertable
--     ze skompresowanymi chunkami właśnie w tym wariancie (NULL / stała
--     wartość domyślna); kolumnę dokłada też do tabel skompresowanych.
--     Sprawdzone na kopii schematu z 25 skompresowanymi chunkami.
--   * Blokada: ACCESS EXCLUSIVE na hypertable i każdym chunku, na czas zmiany
--     katalogu (milisekundy). Groźne jest CZEKANIE na nią — ALTER w kolejce
--     blokuje za sobą każdy kolejny odczyt cech (silnik, trener). Stąd
--     `lock_timeout`: jeśli w 5 s nie dostaniemy blokady (np. trwa długi odczyt
--     panelu trenera albo zadanie kompresji), migracja pada, transakcja się
--     cofa, a kontener `migrate` poda API ponawia start po backoffie.
--   * Bez indeksu: kolumna jest czytana wyłącznie razem z wierszem, który i tak
--     się odczytuje (panel trenera, kontrola datowania z 7 dni).
--   * GRANT niepotrzebny: role procesów mają prawa nadane na CAŁĄ tabelę
--     (0013, 0034: `GRANT ... ON ALL TABLES IN SCHEMA feat`), nie per kolumna —
--     sprawdzone w information_schema.column_privileges na produkcji.

SET LOCAL lock_timeout = '5s';

ALTER TABLE feat.feature_value
    ADD COLUMN revised_at timestamptz;

COMMENT ON COLUMN feat.feature_value.revised_at IS
    'Zegar ścienny OSTATNIEGO nadpisania wartości przez features.insert_values. '
    'NULL = nigdy nie poprawiana albo poprawiona przed migracją 0070. '
    'revised_at > event_time + 2 świece znaczy: decyzja z tamtej chwili widziała INNĄ wartość.';
