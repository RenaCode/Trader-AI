-- 0059: dwa powody pominięcia, którymi egzekucja odmawia WYSŁANIA wejścia.
--
-- CO BYŁO ZŁE (zmierzone w exec.order i journal.pominiecie, 25-26.09.2026)
--
-- `journal.planned_order.limit_price` to cena zamknięcia świecy decyzyjnej
-- (`loop.run_once`: `price = swieca_dec["close"]`, dalej `zaplanuj`). Zlecenie
-- idzie na giełdę 71-82 minuty później, bo pętla czeka na komplet cech:
--
--     bar 2026-09-25 08:00 -> za_malo_cech 12:01, niepelne_sygnaly 12:11,
--                             decyzja 13:11:48   (+71 min)
--     bar 2026-09-25 20:00 -> decyzja 01:21:16   (+81 min)
--     bar 2026-09-26 00:00 -> decyzja 05:21:52   (+82 min)
--
-- Egzekucja wysyła `timeInForce: PostOnly` (`bybit.zloz_zlecenie`), więc cena
-- przecinająca spread MUSI zostać odrzucona — to zachowanie zamówione, nie
-- awaria giełdy. Wszystkie trzy odrzucenia w historii produkcji to kupna
-- SOLUSDC z limitem NAD askiem; oba wypełnienia miały limit po stronie
-- pasywnej. Separacja zupełna, 3/3 kontra 2/2.
--
-- Od 2026-09-26 cena limitu powstaje z arkusza księgi egzekucji odczytanego
-- W CHWILI WYSYŁKI (`gielda_exec.arkusz_wysylki`). Zmiana ceny nie może jednak
-- być bezwarunkowa: gdy rynek odjechał od ceny decyzyjnej dalej niż 1,5×ATR
-- pary, wejście przestaje być tym, co decyzja popierała, i NIE JEST składane.
-- Tak samo, gdy z giełdy nie da się odczytać ANI arkusza, ANI ostatniej ceny —
-- bo wtedy jedyną ceną, jaką mamy, jest ta sprzed 75 minut.
--
-- DLACZEGO TE ODMOWY MUSZĄ BYĆ W `journal.pominiecie`, A NIE TYLKO W `exec.order`
--
-- Wiersz `exec.order` z `reject_reason` powstaje i zostaje (patrz `zloz_plan`),
-- ale odpowiada na pytanie „co się stało z tym zleceniem". Pytanie, które
-- naprawdę się zadaje, brzmi „czemu dla świecy 2026-09-25 08:00 nie weszliśmy
-- w SOLUSDC" — a na nie odpowiada wyłącznie tabela kluczowana
-- (instrument, świeca, powód), po której jedno złączenie zestawia odmowę
-- z decyzją, która ją zrodziła. Dokładnie ten argument stoi w 0041.
--
-- ROZSZERZENIE ZNACZENIA TABELI JEST ŚWIADOME. 0041 opisała ją jako „instrumenty,
-- które wypadły z przebiegu PRZED oceną". Te dwa powody są późniejsze: decyzja
-- JEST podjęta i zapisana, nie doszło do wysłania zlecenia. Wspólne jest to,
-- co tabela naprawdę mierzy — (instrument, świeca) bez pozycji i powód, dla
-- którego jej nie ma. Osobna tabela na to samo pytanie kazałaby pytać w dwóch
-- miejscach i jedno z nich byłoby zapominane.
--
-- KLUCZ GŁÓWNY ZOSTAJE BEZ ZMIAN. `zloz_oczekujace` chodzi co 300 s, a plan żyje
-- dwie świece, więc ta sama odmowa wróci kilkakrotnie; `ON CONFLICT DO NOTHING`
-- zostawia znacznik PIERWSZEGO wystąpienia, czyli chwili, w której cena
-- odjechała.
ALTER TABLE journal.pominiecie DROP CONSTRAINT IF EXISTS pominiecie_powod_check;
ALTER TABLE journal.pominiecie
    ADD CONSTRAINT pominiecie_powod_check
    CHECK (powod IN ('za_malo_cech', 'brak_ceny', 'niepelne_sygnaly',
                     'cena_odjechala', 'brak_ceny_wysylki'));

COMMENT ON COLUMN journal.pominiecie.powod IS
'za_malo_cech = feat.as_of() oddało mniej niż 4 cechy; brak_ceny = nie było ani
domkniętej świecy, ani ceny minutowej; niepelne_sygnaly = pokrycie wag poniżej
progu, decyzja odłożona; cena_odjechala = wejście NIE zostało wysłane, bo cena
z chwili wysyłki odjechała od ceny decyzyjnej dalej niż 1,5×ATR pary;
brak_ceny_wysylki = wejście NIE zostało wysłane, bo z giełdy nie dało się
odczytać ani arkusza, ani ostatniej ceny, a cena z planu jest sprzed ~75 minut.';
