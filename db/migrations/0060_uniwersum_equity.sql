-- 0060: tor gieldowy dostaje uniwersum decyzyjne
--
-- ZGODA WLASCICIELA: 2026-09-27, wprost, po przedstawieniu tresci tego UPDATE
-- i skutkow. Wczesniej ta zmiana byla wstrzymana, bo jest zmiana DANYCH
-- produkcyjnych, a nie konfiguracji.
--
-- CO BYLO NIE TAK
--
-- Migracja 0047 wpisala instrumenty equity z `tradeable = false` i nikt tego
-- nigdy nie zmienil. Zmierzone 2026-09-26 na produkcji:
--
--   engine  | aktywne (zbieraja historie) | handlowalne (uniwersum decyzyjne)
--   crypto  |                          21 |                                10
--   equity  |                          21 |                                 0
--
-- Skutek: tor equity od 2024-04 zbiera dane (94 tys. swiec, 723 tys. wartosci
-- cech) i nie moze trenowac, bo `train.py` przerywa z kodem 3 na pustym
-- uniwersum. Procesy `engine-equity`, `features-equity` i `kapital-equity`
-- chodza dla toru, ktory nie sklada zlecen.
--
-- UWAGA NA PREMISE, KTORA BYLA FALSZYWA: przez chwile twierdzilem, ze to samo
-- wywala nocny Job co noc i trzyma ArgoCD na czerwono. NIEPRAWDA - puste
-- uniwersum oddaje `KOD_TOR_NIEGOTOWY = 3`, a skrypt zadania traktuje trojke
-- jako "tor niegotowy" i Joba nie wywala. Zmierzone 2026-09-26: najnowszy
-- przebieg `Complete`, ArgoCD `Synced / Healthy`, a jedyny `Error` mial 27 h.
-- Uzasadnienie tej migracji jest wiec wezsze: tor nie moze trenowac. To
-- wystarcza, ale nic sie przez to nie palilo.
--
-- DLACZEGO TO NIE OTWIERA HANDLU
--
-- `workers.engine-equity` chodzi z `--wymagaj-modelu` (charts/trader/values.yaml).
-- Bez wersji modelu w statusie `active` petla konczy cykl BEZ DECYZJI
-- (`loop.py`, galaz `if wymagaj_modelu`), wiec ta migracja odblokowuje TRENING.
-- Zlecenia pojda dopiero, gdy model przejdzie bramke promocji - a ta od
-- 2026-09-26 wymaga deflowanego Sharpe'a liczonego PO koszcie, wiec jest
-- trudniejsza, nie latwiejsza.
--
-- ZALEZNOSC, KTORA MUSI BYC ZAMKNIETA, ZANIM EQUITY FAKTYCZNIE HANDLUJE
--
-- Od 2026-09-26 cena limitu pochodzi z arkusza z chwili wysylki, a odczyt
-- starszy niz `gielda_exec.WIEK_ARKUSZA_MAX_S` = 60 s jest odrzucany. Poza
-- sesja gieldowa KAZDY arkusz akcji jest starszy niz 60 s, wiec dopoki prog
-- wieku nie liczy sie z kalendarza sesji, tor equity odmawia kazdego wejscia
-- (powod `brak_ceny_wysylki`). Kierunek jest bezpieczny - odmowa, nie zla
-- cena - ale to znaczy, ze sama ta migracja handlu nie wlacza.
--
-- DLACZEGO SPY ZOSTAJE POZA UNIWERSUM
--
-- SPY to ETF, ktory 0047 opisala `meta.sektor = 'odniesienie'` - jawne
-- stwierdzenie, ze jest tam do POROWNANIA, nie do handlu. Handlowanie indeksem
-- obok 20 jego wlasnych skladnikow to ta sama ekspozycja policzona dwa razy,
-- a to wchodzi do `max_total_exposure`. Zaden kod nie traktuje dzis wartosci
-- 'odniesienie' specjalnie (sprawdzone gremem), wiec gdyby SPY mial handlowac,
-- trzeba to dopisac SWIADOMIE - osobna decyzja, nie skutek uboczny.
--
-- WIERSZE NIEAKTYWNE ZOSTAJA NIEHANDLOWALNE
--
-- `active` w warunku nie jest ozdoba: w tabeli sa dublety AAPL i SPY po starym
-- `venue = ibkr`, ktore 0047 wyzerowala. Bez tego warunku migracja
-- przywrocilaby do uniwersum instrumenty z gieldy, ktorej nie uzywamy.
--
-- ODWROTNOSC: `UPDATE ref.instrument SET tradeable = false WHERE engine='equity';`

BEGIN;

UPDATE ref.instrument
   SET tradeable = true
 WHERE engine = 'equity'
   AND active
   AND kind = 'stock'
   AND coalesce(meta->>'sektor', '') <> 'odniesienie';

COMMIT;
