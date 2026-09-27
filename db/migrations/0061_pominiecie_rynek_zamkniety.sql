-- 0061: dwa powody pominięcia dla stanu rynku w CHWILI WYSYŁKI.
--
-- PREMISA, KTÓRA BYŁA FAŁSZYWA I KTÓRĄ TA MIGRACJA POPRAWIA
--
-- 0060 napisała (sekcja "ZALEZNOSC, KTORA MUSI BYC ZAMKNIETA"), że poza sesją
-- każdy arkusz akcji jest starszy niż `WIEK_ARKUSZA_MAX_S` = 60 s, więc tor
-- equity "odmawia kazdego wejscia (powod `brak_ceny_wysylki`)", a kierunek jest
-- bezpieczny. NIEPRAWDA W OBU POŁOWACH — zmierzone 2026-09-27 na kodzie z main:
--
--   1. POZA SESJĄ EGZEKUCJA NIE JEST WOŁANA W OGÓLE. `loop.run_once` ma bramkę
--      sesji PRZED decyzjami, a `zloz_oczekujace` stoi na KOŃCU przebiegu, więc
--      cykl poza sesją kończy się na `return stats` w bramce. Przebieg
--      `run_once` na silniku equity z adapterem wybuchającym przy każdym
--      sięgnięciu po atrybut kończy się bez jednego kontaktu z giełdą i z ZEREM
--      wierszy w `journal.pominiecie`. `brak_ceny_wysylki` nie miał więc jak
--      powstać ani razu.
--
--   2. GDYBY BYŁA WOŁANA, WYNIKIEM NIE BYŁABY ODMOWA — BYŁOBY ZLECENIE.
--      `brak_ceny_wysylki` wymaga, żeby zawiodły OBA odczyty ceny, a próg wieku
--      osłania tylko `arkusz_wysylki`. Drugie źródło, `adapter.cena_odniesienia`,
--      oddaje goły `Decimal` BEZ znacznika czasu (oba adaptery), więc jego wieku
--      nie da się sprawdzić — i poza sesją ten odczyt SIĘ UDAJE, oddając cenę
--      zamknięcia. Zmierzone na `zloz_plan` z arkuszem w wieku 120 s (dwukrotność
--      progu) i odczytaną ostatnią transakcją: zlecenie ZOSTAŁO ZŁOŻONE po
--      232,44, a `journal.pominiecie` nie dostało ani jednego wiersza.
--
-- Kierunek był więc ODWROTNY do opisanego: nie bezpieczna odmowa, tylko ciche
-- wysłanie ceny sprzed godzin. Próg wieku fail-open przez nieosłonięte źródło
-- zapasowe.
--
-- CO TE POWODY OPISUJĄ
--
-- Szczelina, która z tego zostaje, jest węższa niż opisana w 0060, ale realna:
-- stan sesji czytano RAZ, na początku cyklu, a wysyłka dzieje się na jego końcu.
-- Cykl wystartowany 15:59 czasu Nowego Jorku przechodził bramkę jako "otwarta"
-- i wysyłał zlecenia po 16:00. Alpaca przyjmuje wtedy zlecenie `day` i KOLEJKUJE
-- JE DO NASTĘPNEGO OTWARCIA (nagłówek `services/exchanges/alpaca.py`), czyli
-- pozycja powstaje nazajutrz na otwarciu, po cenie z wczorajszego zamknięcia,
-- przez lukę nocną, bez decyzji, która by ją popierała.
--
-- `loop.run_once` czyta teraz stan sesji PONOWNIE, bezpośrednio przed
-- `zloz_oczekujace`, i `zloz_plan` wstrzymuje na nim WEJŚCIA.
--
-- DLACZEGO DWA POWODY, A NIE JEDEN
--
-- To są dwa różne zdarzenia i mieszanie ich kosztowałoby dokładnie tyle, ile
-- zwykle kosztuje w tym projekcie: alarm na stanie normalnym uczy odklikiwać
-- całą kategorię (ten wzorzec opisuje `dozor.py` przy kill-switchu).
--
--   * `rynek_zamkniety` to stan NORMALNY. Zdarza się każdej nocy, w każdy
--     weekend i w każde święto giełdowe; nie wymaga żadnej reakcji. Zapisywanie
--     go jako `brak_ceny_wysylki` czytałoby się w dzienniku jak problem
--     z giełdą.
--   * `sesja_nieznana` to USTERKA odczytu — zegar giełdy nie odpowiedział.
--     Wejście jest wstrzymane (brak wiedzy nie daje domysłu "pewnie otwarte"),
--     ale to JEST rzecz do naprawienia i musi dać się odfiltrować osobno.
--
-- ŚWIĘTA I SESJE PRZEDŁUŻONE, czyli czego te powody NIE rozstrzygają
--
-- Źródłem prawdy jest `/v2/clock` Alpaki, a nie własny kalendarz (uzasadnienie
-- stoi w 0055 i zostaje w mocy). Wynika z tego, co następuje:
--
--   * ŚWIĘTA są obsłużone bez jednej linii kodu o świętach. `is_open` jest
--     fałszem w Dzień Dziękczynienia i w skróconą sesję wigilijną tak samo jak
--     w nocy, a `next_open` podaje właściwą datę. Własny kalendarz godzin
--     regularnych dałby tu odpowiedź BŁĘDNĄ.
--   * SESJE PRZEDŁUŻONE (pre-market 04:00-09:30, after-hours 16:00-20:00) są
--     traktowane jako RYNEK ZAMKNIĘTY, bo `/v2/clock` mówi o sesji regularnej.
--     To jest zgodne z tym, co robimy: `alpaca.zloz_zlecenie` nie wysyła
--     `extended_hours`, więc zlecenie i tak nie weszłoby w tych godzinach.
--     Gdyby handel w sesji przedłużonej miał kiedyś ruszyć, trzeba zmienić OBIE
--     rzeczy razem — flagę w zleceniu i tę bramkę — a nie jedną z nich.
--   * `ref.session_calendar` ZOSTAJE NIEUŻYWANY. Warunek zmiany tej decyzji
--     postawiony w 0055 brzmi "pierwszy proces, ktory naprawde potrzebuje
--     HISTORYCZNYCH godzin sesji" i ta migracja go NIE spełnia: pyta wyłącznie
--     o teraz.
--
-- CZEGO TA MIGRACJA NIE ZMIENIA
--
-- Progu `WIEK_ARKUSZA_MAX_S` = 60 s. Podniesienie go dla rynku ciągłego byłoby
-- cichym zezwoleniem na starą cenę, a dla akcji nie jest potrzebne: bramką jest
-- stan sesji, nie luźniejszy próg. Tor krypto nie przechodzi tu niczego nowego —
-- `sesja.stan_sesji` kończy pytanie na `is_24_7` i oddaje `otwarta=True`, nie
-- sięgając po zegar, więc nie płaci za tę bramkę ani jednym zapytaniem.
--
-- WYJŚĆ TA BRAMKA NIE DOTYCZY. Odmowa wyjścia zostawia pozycję otwartą wbrew
-- decyzji, czyli kosztuje pieniądze, a nie okazję — ta asymetria stoi już
-- w nagłówku `zlecenia.py` i tu jest tylko powtórzona.
--
-- KLUCZ GŁÓWNY ZOSTAJE BEZ ZMIAN, z tego samego powodu co w 0059: `zloz_oczekujace`
-- chodzi co 300 s, a plan żyje dwie świece, więc ta sama odmowa wróci
-- kilkakrotnie i `ON CONFLICT DO NOTHING` zostawia znacznik PIERWSZEGO
-- wystąpienia.
ALTER TABLE journal.pominiecie DROP CONSTRAINT IF EXISTS pominiecie_powod_check;
ALTER TABLE journal.pominiecie
    ADD CONSTRAINT pominiecie_powod_check
    CHECK (powod IN ('za_malo_cech', 'brak_ceny', 'niepelne_sygnaly',
                     'cena_odjechala', 'brak_ceny_wysylki',
                     'rynek_zamkniety', 'sesja_nieznana'));

COMMENT ON COLUMN journal.pominiecie.powod IS
'za_malo_cech = feat.as_of() oddało mniej niż 4 cechy; brak_ceny = nie było ani
domkniętej świecy, ani ceny minutowej; niepelne_sygnaly = pokrycie wag poniżej
progu, decyzja odłożona; cena_odjechala = wejście NIE zostało wysłane, bo cena
z chwili wysyłki odjechała od ceny decyzyjnej dalej niż 1,5×ATR pary;
brak_ceny_wysylki = wejście NIE zostało wysłane, bo z giełdy nie dało się
odczytać ani arkusza, ani ostatniej ceny, a cena z planu jest sprzed ~75 minut;
rynek_zamkniety = wejście NIE zostało wysłane, bo rynek instrumentu był
zamknięty w chwili wysyłki (/v2/clock). STAN NORMALNY, nie awaria - zdarza się
każdą noc, weekend i święto giełdowe i NIE wymaga reakcji;
sesja_nieznana = wejście NIE zostało wysłane, bo zegar giełdy nie odpowiedział.
To JEST usterka odczytu i wymaga reakcji - brak wiedzy wstrzymuje wejście, ale
nie wolno go mylić z rynkiem zamkniętym.';
