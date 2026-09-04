-- 0020: rozdzielenie "zbieramy dane" od "handlujemy"
--
-- DLACZEGO
--
-- Flaga `active` znaczyła dotąd obie rzeczy naraz: procesy `market` i `backfill`
-- filtrują po niej, żeby wiedzieć, czego historię utrzymywać, a pętla decyzyjna
-- i trener — żeby wiedzieć, co obstawiać. Dopóki zbiór był ten sam, zlepienie
-- nie przeszkadzało.
--
-- Przestało, gdy okazało się, że giełda, na której stoi konto, oferuje 10
-- z naszych 24 instrumentów (patrz 0018). Zawężenie uniwersum przez `active`
-- urwałoby zbieranie świec dla pozostałych czternastu — a wtedy powrót do nich,
-- gdyby giełda je dodała albo gdyby konto przeniosło się gdzie indziej,
-- zaczynałby się od dziury w historii. Cztery lata świec to najdroższa rzecz,
-- jaką ten projekt posiada.
--
-- Od teraz:
--     active     — utrzymujemy historię, cechy i sygnały
--     tradeable  — instrument wchodzi do uniwersum decyzyjnego
--
-- POWÓD ZAWĘŻENIA, NIE TYLKO JEGO TREŚĆ
--
-- Kalibracja na instrumentach, których nie da się kupić, mierzy strategię
-- niewykonalną. Zmierzone: zawężenie do 10 dostępnych podniosło odsetek
-- dodatnich okien z 62,5% do 71,8%, bo odpadły instrumenty najcieńsze —
-- czyli nie jest to ustępstwo, tylko poprawa. Pomiar w docs/03.

ALTER TABLE ref.instrument
    ADD COLUMN tradeable boolean NOT NULL DEFAULT true;

COMMENT ON COLUMN ref.instrument.tradeable IS
    'Czy instrument wchodzi do uniwersum decyzyjnego. Historia zbierana jest '
    'dla wszystkich z active=true, niezależnie od tej flagi.';

-- Pary nieobecne na podmiocie unijnym. Historia zbierana dalej.
UPDATE ref.instrument
   SET tradeable = false
 WHERE engine = 'crypto'
   AND symbol IN ('APTUSDT','ATOMUSDT','AVAXUSDT','BCHUSDT','BNBUSDT','DOTUSDT',
                  'FILUSDT','INJUSDT','LTCUSDT','OPUSDT','SEIUSDT','TIAUSDT',
                  'TRXUSDT','UNIUSDT');

-- Kontrakty wieczyste i tak są nieaktywne, ale niech flaga mówi to wprost:
-- podmiot unijny ich nie oferuje.
UPDATE ref.instrument SET tradeable = false WHERE kind = 'perp';
