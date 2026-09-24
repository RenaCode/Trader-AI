-- 0053: limit dźwigni — brakujące zabezpieczenie strony krótkiej
--
-- POWÓD
--
-- `services/engine/risk_rules.py` nie znał pojęcia dźwigni ani razu. Nie było
-- to przeoczenie jednej reguły, tylko ZAŁOŻENIE wpisane w uzasadnienie innego
-- limitu — w `wartosc_limitu()` stoi zdanie:
--
--     "Limit łącznej ekspozycji przestaje wtedy cokolwiek ograniczać, bo na
--      spocie BEZ DŹWIGNI nie da się przekroczyć 100% rachunku"
--
-- Na spocie to prawda i dlatego przez cały czas nic się nie działo. Na
-- kontrakcie wieczystym ekspozycja 10x kapitału jest USTAWIENIEM, nie awarią,
-- a strata na pozycji krótkiej nie ma górnego końca: kupno może stracić
-- najwyżej to, co kosztowało, sprzedaż krótka traci tyle, ile urośnie cena.
--
-- Drugi powód jest osobny i dotyczy tego systemu wprost: kill-switch NIE JEST
-- zleceniem stop-loss. `risk_rules` mówi to sam przy `min_equity` ("sprawdza
-- się raz na cykl decyzyjny, więc między sprawdzeniami cena może zejść niżej").
-- Pętla budzi się co 300 s, dozorca co 60 s. Przy dźwigni 1x luka między
-- sprawdzeniami kosztuje tyle, ile rynek zrobi w tę minutę; przy 10x kosztuje
-- dziesięć razy tyle, a limit obsunięcia zostaje ten sam.
--
-- DLACZEGO 1x, SKORO DZIŚ I TAK NIC NIE SHORTUJE
--
-- Bo ta migracja ma wejść PRZED pierwszym instrumentem z dźwignią, a nie razem
-- z nim. Limit dobierany w chwili, w której zaczyna przeszkadzać, dobiera się
-- pod wynik. `max_total_exposure` wpuszcza dziś 15% kapitału, więc 1x jest
-- o rząd wielkości luźniejsze niż limit, który faktycznie wiąże — nie jest
-- ograniczeniem strategii, tylko sufitem na wypadek, gdyby limit alokacji
-- został kiedyś zapisany kwotowo (tak było do 0028) albo podniesiony.
--
-- JEDNOSTKA `x` — KROTNOŚĆ KAPITAŁU
--
-- Nie `pct_equity`, choć arytmetyka byłaby ta sama. `pct_equity` znaczy
-- "procent kapitału przeznaczony na coś" i jest limitem ALOKACJI;
-- `x` znaczy "ile razy kapitał jest zaangażowany" i jest limitem RYZYKA.
-- Zlanie ich w jedną jednostkę odebrałoby możliwość zapisania jednego z nich
-- kwotowo bez zepsucia drugiego — a dokładnie to wydarzyło się przed 0028.
--
-- `action = 'block'`, nie `killswitch`: przekroczona dźwignia jest własnością
-- ZLECENIA, którego jeszcze nie złożyliśmy, a nie stanem rachunku. Odmowa
-- wejścia wystarcza; zatrzymanie całego silnika wymagałoby ręcznego zwolnienia
-- za coś, co się nie wydarzyło.
--
-- CO TA MIGRACJA ZMIENIA NA PRODUKCJI: NIC WIDOCZNEGO. Całe uniwersum
-- decyzyjne to `kind='spot'` (`uniwersum.py` odfiltrowuje `perp`), a limit
-- 1x leży dziesięciokrotnie nad `max_total_exposure`. Od tej chwili
-- `risk.limit_usage` dostaje jednak szereg czasowy wykorzystania dźwigni —
-- czyli liczbę, której nikt do tej pory nie mierzył.

BEGIN;

INSERT INTO risk.limit_def (engine, mode, name, scope, value, unit, action) VALUES
    ('crypto','demo','max_leverage','engine', 1, 'x', 'block'),
    ('equity','demo','max_leverage','engine', 1, 'x', 'block')
ON CONFLICT (engine, mode, name, scope, instrument_id) DO NOTHING;

COMMENT ON COLUMN risk.limit_def.unit IS
'Jednostka wartości limitu:
  pct        - procent (strata dzienna, obsunięcie od szczytu)
  pct_equity - procent KAPITAŁU; przelicza się w chwili oceny, więc ten sam
               zapis znaczy to samo na rachunku 100 000 i na 10 000
  pct_wplat  - procent wpłat netto (próg kapitału po wypłatach)
  quote_ccy  - kwota w walucie kwotowanej
  count      - sztuki (liczba zleceń)
  x          - KROTNOŚĆ kapitału (dźwignia): 1 = "nie angażuj więcej, niż masz".
               Różni się od pct_equity intencją, nie arytmetyką: pct_equity
               ogranicza ALOKACJĘ i wolno go zapisać kwotowo, x ogranicza
               RYZYKO i kwotowo nie ma sensu.';

COMMIT;
