-- 0030: cecha przekrojowa dostaje odcisk uniwersum w tożsamości
--
-- `rel_strength` udaje własność instrumentu, a jest własnością PARY
-- (instrument, uniwersum). Percentyl BTC wśród dziesięciu par to inna liczba
-- niż wśród dwudziestu czterech — ta sama nazwa, dwa różne znaczenia.
--
-- Dopóki nazwa nie mówi, dla jakiego zbioru wartość policzono, zmiana uniwersum
-- zostawia w bazie wiersze, które WYGLĄDAJĄ na aktualne: nie ma awarii, nie ma
-- pustego pola, jest liczba — tyle że opisująca inny świat.
--
-- Zdarzyło się to dwa razy:
--   * migracja 0023 przestawiła uniwersum decyzyjne z par USDT na USDC,
--     a wartości sprzed niej zostały obok nowych, nieodróżnialne po nazwie;
--   * porównania uniwersum kandydującego wymagały przeliczenia tej cechy, co
--     NADPISYWAŁO wartości produkcyjne — eksperymentu nie dało się zrobić,
--     nie psując tego, co handluje.
--
-- Od teraz nazwa to `rel_strength__4h#<odcisk>`, a `params->>'uniwersum'`
-- niesie ten sam odcisk. Silnik i trener filtrują po nim: wartość policzona dla
-- innego zbioru przestaje być widoczna, zamiast po cichu znaczyć co innego.
--
-- DLACZEGO STARA DEFINICJA MUSI ZOSTAĆ WYŁĄCZONA, A NIE ZOSTAWIONA
--
-- Filtr przepuszcza cechy bez odcisku (`params->>'uniwersum' IS NULL`), bo tak
-- wyglądają wszystkie cechy per instrument — RSI, ATR, zwroty. Stara definicja
-- siły względnej też go nie ma, więc przeszłaby razem z nimi i zderzyła się
-- z nową w tym samym kluczu roli. Wygrałby przypadkowy wiersz.
--
-- Wartości NIE SĄ KASOWANE. Są dowodem, jak liczono wcześniej, a przy wyłączonej
-- definicji nie czyta ich nikt.

BEGIN;

UPDATE feat.feature_def
   SET active = false,
       description = description || ' [WYŁĄCZONA w 0030: nazwa bez odcisku '
                     || 'uniwersum, patrz services/engine/uniwersum.py]'
 WHERE name LIKE 'rel\_strength\_\_%' ESCAPE '\'
   AND name NOT LIKE '%#%'
   AND active;

COMMIT;
