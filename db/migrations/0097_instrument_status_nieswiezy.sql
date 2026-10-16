-- 0097: `instrument.status_nieswiezy` - osobny kod dla "status nie byl sprawdzany".
--
-- CO BYLO ZLE (przeglad PR #34, 2026-10-16)
--
-- Czujka `dozor --co instrumenty` meldowala "status N instrumentow nie byl
-- sprawdzany" kodem `instrument.suspended` - tym samym, ktorym
-- `services/ingest/instrumenty.py` zglasza ZAWIESZENIE i WZNOWIENIE pary.
-- Katalogowy cooldown tego kodu (3600 s, migracja 0033) liczy sie per
-- (odbiorca, kod), wiec meldunek czujki o JEDNYM silniku (np. equity bez
-- odczytu statusu) polykal przez godzine powiadomienie o zawieszeniu pary
-- DRUGIEGO silnika. A odswiezacz zglasza tylko ZMIANE stanu - polkniete
-- powiadomienie nie wraca nigdy.
--
-- DLACZEGO `always_on` I COOLDOWN 0
--
-- Ten alarm opisuje AWARIE ODSWIEZACZA (stan trwaly), nie zdarzenie rynkowe.
-- Powtorke liczy czujka z rosnacym odstepem od poczatku awarii (doba, dwie,
-- cztery, potem raz w tygodniu - `dozor.odstep_powtorki_zadania`), tak jak
-- dla `job.nieudany`. Do tego potrzebny jest WIERSZ przy kazdym zgloszeniu:
-- `notify()` zwraca None bez zapisu przy wylaczonej preferencji albo jej
-- cooldownie, a wtedy nastepny przebieg nie wie, ze juz zglaszal (patrz 0094).
-- `always_on` omija oba, wiec wiersz jest zawsze.
--
-- KOTWICA OKNA PRZEJSCIA. Data zastosowania tej migracji
-- (`schema_migrations.applied_at`) to chwila wdrozenia obrazu z odswiezaczem
-- statusu dla Alpaki - `instrumenty.ODSWIEZACZ_OD`. Do pierwszego przebiegu
-- `trader-instrumenty` (do 6 h) daty sprawdzenia akcji sa puste z braku
-- przebiegu, nie z awarii.

BEGIN;

SET LOCAL lock_timeout = '5s';

INSERT INTO notify.event_type
    (code, label, description, category, default_severity, always_on, default_cooldown_s)
VALUES (
    'instrument.status_nieswiezy',
    'Status instrumentów nie był sprawdzany u giełdy',
    'Odświeżacz statusu (zadanie trader-instrumenty) nie potwierdził u giełdy ' ||
    'statusu instrumentów dłużej niż próg czujki. Kolumna statusu pokazuje wtedy ' ||
    'ostatnią znaną wartość i wygląda jak potwierdzenie handlu - delisting ' ||
    'przeszedłby niezauważony. Przypomnienia co dobę, dwie, cztery, potem co tydzień.',
    'ryzyko', 'warning', true, 0)
ON CONFLICT (code) DO NOTHING;

COMMIT;
