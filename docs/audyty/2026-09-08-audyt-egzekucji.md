# AUDYT Trader-AI — dziennik ustaleń

Zasada: dopisuję po KAŻDYM ustaleniu. Kontekst ginie, plik zostaje.

## Stan prac
- [x] Rozpoznanie repo
- [ ] 1. decided_for_bar / okno_decyzji
- [ ] 2. PostOnly
- [ ] 3. pasmo cenowe (zloz_plan/sprawdz_zlecenie)
- [ ] 4. zapisz_wejscie(order_id)
- [ ] 5. rozstrzygnij_plan
- [ ] 6. anuluj_wejscia
- [ ] 7. update_equity realized_pnl
- [ ] 8. RiskKernel limity
- [ ] 9. migracje 0032/0033/0034
- [ ] 10. atrapa Kursor
- [ ] 11. role procesów

## ZNALEZISKA
(brak jeszcze)

## Notatki robocze (nie-znaleziska / do weryfikacji)

### zlecenia.py (pasmo)
- `sprawdz_zlecenie` L301-308: wyjście poza pasmem → `c = clamp(c, dolna, gorna)`.
  PODEJRZENIE: przyciąga do DALSZEJ krawędzi pasma, nie do odniesienia.
  Sprzedaż z c > gorna ląduje na odniesienie*(1+prog), czyli >=2% nad rynkiem
  → przy PostOnly zlecenie jest przyjęte jako maker i NIGDY się nie wypełnia.
  Docstring twierdzi "zostawia wyjście wykonalnym" — nieprawda. DO POTWIERDZENIA.
- prog minimalny = ROZJAZD_GIELD = 0.02 (2%) + ruch. Sufit 0.25.

### gielda_exec.py
- L351-357 `zloz_oczekujace`: nazwa cechy ATR zaszyta na sztywno "atr_pct__4h",
  funkcja nie dostaje horyzontu `h`. Przy innym horyzoncie ATR=None → pasmo
  awaryjne 25% zawsze. Do sprawdzenia czy loop kiedykolwiek woła z innym h.
- L346-352: podzapytanie o ATR bez ograniczenia wieku (`event_time`) i bez
  filtra wersji — bierze ostatnią wartość jakkolwiek starą.
- `anuluj_wejscia` L470-500: wyścig — stan_zlecenia mówi wypelniono==0, potem
  zlecenie się wypełnia, anuluj_zlecenie zwraca 110001 (traktowane jako sukces),
  wiersz i plan idą na 'cancelled' → wypełnienie zgubione. Okno wąskie.

## ZNALEZISKO A (poważne) — nieudane anulowanie reszty przy wypełnieniu CZĘŚCIOWYM zostawia żywe zlecenie, a plan idzie na 'triggered'
Plik: services/engine/gielda_exec.py, L~193-206 (try/except anulowania) i L211 (`if r.rodzaj == "brak"`).
Poprawka nr 5 (`nie_zdjete`) obsługuje TYLKO gałąź `r.rodzaj == "brak"`.
`co_z_wypelnieniem` (services/engine/egzekucja.py L71-85) ustawia `anuluj_reszte=True`
także dla 'czesciowe' i 'nie_do_zamkniecia'.
Scenariusz:
 1. Plan wejścia BTCUSDT qty 0.010, PostOnly, leży w arkuszu.
 2. Wypełnia się 0.004. Cykl rozstrzygający: rodzaj='czesciowe', anuluj_reszte=True.
 3. `adapter.anuluj_zlecenie` rzuca ExchangeError (5xx / rate-limit / timeout).
    `anulowanie_padlo` ustawione, ale gałąź ratunkowa jest tylko dla 'brak'.
 4. Kod leci dalej: `zapisz_wejscie` księguje pozycję 0.004,
    `journal.planned_order.status='triggered'`, `exec.order.status='partial'`.
 5. Reszta 0.006 ZOSTAJE ŻYWA na giełdzie. Rozstrzyganie szuka planów 'pending' —
    ten już nie jest pending. Anulowanie ponawia tylko `anuluj_wejscia`, a ta
    rusza wyłącznie po kill-switchu.
 6. Reszta wypełnia się godzinę później → na giełdzie 0.010, w dzienniku 0.004.
Skutek: ekspozycja zaniżona o 60%, risk kernel zwalnia miejsce na kolejne wejścia,
wyjście sprzeda 0.004 i zostawi 0.006 sierotę, uzgodnienie zgłosi pozycję-widmo.

## ZNALEZISKO B — po odpowiedzi "giełda nie zna zlecenia" wiersz exec.order zostaje na 'unknown' na zawsze
Plik: services/engine/gielda_exec.py L~156-162 (gałąź `if stan is None:`).
Aktualizowany jest tylko `journal.planned_order` → 'expired'. Wiersz `exec.order`
wstawiony w `zloz_plan` przy ExchangeError ma status 'unknown' i NIKT go nie zamyka
(gałąź `r.rodzaj=="brak"` niżej robi to poprawnie, ta nie).
Scenariusz: PostOnly odrzuca zlecenie przy składaniu (przecięłoby spread) → ExchangeError
→ wiersz 'unknown'. Następny cykl: stan_zlecenia=None → plan 'expired', wiersz nadal
'unknown'. `anuluj_wejscia` filtruje `status IN ('new','partial','unknown')`, więc przy
KAŻDYM kill-switchu odpytuje i próbuje anulować zlecenia, których nigdy nie było —
i czujka zdrowia egzekucji liczy je jako wiszące. Rośnie w nieskończoność.

## Notatka: PostOnly a rozjazd z rachunkiem papierowym
- Cena limitu = zamknięcie świecy 4h z podmiotu GLOBALNEGO (loop.py L~1160 `swieca_dec["close"]`),
  potem `cena_do_kroku` (kupno w dół / sprzedaż w górę). Zlecenie ląduje w księdze UNIJNEJ.
- Sprzedaż z limitem <= najlepszy bid → PostOnly ODRZUCA. Zamknięcie świecy bywa równe
  bidowi (ostatnia transakcja po stronie sprzedającego), więc znaczna część zleceń jest
  odrzucana już przy składaniu. Rachunek papierowy (`rozstrzygnij_plany`) nie zna tego
  stanu w ogóle — wypełnia, gdy świeca dotknęła limitu.
- Skutek dla WYJŚĆ: odrzucone wyjście → pozycja zostaje otwarta co najmniej o jedną
  świecę 4h dłużej; w spadającym rynku wyjście goni cenę świeca po świecy.

## Role bazodanowe (0034) — sprawdzone, na razie bez znaleziska
Przeczytane: db/migrations/0034_role_procesow.sql w całości + kto czym chodzi:
- workers.yaml: domyślnie `silnik`; jawnie `dozorca` tylko dla dozorcy (values.yaml L~270)
- zywotnosc-cron: `czujka`; proba-cron: `proba`; trainer-cron: `silnik`; API: `trader_app`
- backup.yaml wstawia app.backup_run — chodzi psql z `trader.databaseUrl` (właściciel), OK
Zweryfikowane zapisy vs. nadania:
- silnik: journal/exec/risk.limit_usage/violation/engine_heartbeat/killswitch_event INSERT — OK
- dozorca: RiskKernel.check_account nie pisze usage/violations; tylko INSERT killswitch_event
  + UPDATE(released_at, released_auto, release_note) z wznowienie.py L174 — OK
- czujka: dozor.py + uzgodnienie.py tylko SELECT + notify (INSERT notification/delivery,
  UPDATE channel(last_sent_at,last_error)) — OK
- proba: proba_killswitcha.py UPDATE limit_def(value,updated_at) i killswitch_event
  (released_at, released_auto, release_note), SELECT notify.notification — OK
- API: routers/risk.py L143-147 UPDATE dokładnie (released_at, released_by, release_note) — OK
- SecretBox bierze klucz z env (SECRETS_KEY), nie z app.secret → brak app.secret u czujki/dozorcy
  nie blokuje powiadomień — OK

## Mutacje testów (wprowadzam błąd z powrotem — czy test pada?)
Baza: cały pytest ZIELONY (505 testów).
| # | mutacja | wynik |
|---|---|---|
|1| `okno_decyzji` → `return bar_start` | PADA test_limity_trybu::test_silnik_rozstrzyga_KAZDA_swiece_a_nie_co_druga |
|2| `timeInForce: PostOnly` → `GTC` | PADA test_zlecenia::test_zlecenia_ida_jako_PostOnly_a_nie_GTC (ale to test GREPUJĄCY ŹRÓDŁO, nie behawioralny) |
|3| wyłączenie całego pasma | PADAJĄ 2 testy w test_gielda_exec |
|4| `zapisz_wejscie` zawsze INSERT | PADAJĄ 2 testy (atrapa Kursor łapie UNIQUE) |
|5| `nie_zdjete` wyłączone | PADA test_nieudane_anulowanie_ZOSTAWIA_plan_do_ponowienia |
|6| `anuluj_wejscia` ignoruje stan giełdy | PADA test_killswitch_NIE_anuluje_zlecenia_ktore_sie_juz_wypelnilo |
|7| `update_equity` + `AND closed_at IS NOT NULL` | PADA test_wynik_wyjscia_czesciowego_WCHODZI_do_kapitalu |
|8| `sprawdz_komplet` → `[]` | PADAJĄ 3 testy |
|8b| `seed_equity` także dla live | PADA test_zasiew_kapitalu_dotyczy_TYLKO_demo |
Wniosek: deklarowane poprawki 1,3,4,5,6,7,8 mają testy ROZRÓŻNIAJĄCE. Poprawka 2 ma tylko
test na tekst źródła.

## update_equity — sprawdzone, BEZ znaleziska
equity = wplaty + sum(realized_pnl - fees_paid ze WSZYSTKICH pozycji) + unrealized(otwarte).
`zapisz_wyjscie` przy wyjściu częściowym: qty -= ile, realized_pnl += pnl, fees_paid += prowizja.
`wycen` liczy tylko `closed_at IS NULL` i po POMNIEJSZONEJ qty. Prowizja wejścia siedzi
w fees_paid, cena wejścia w avg_price bez prowizji. Nie ma podwójnego liczenia.

## ZNALEZISKO C — pasmo przyciąga WYJŚCIE do DALSZEJ krawędzi, więc przy PostOnly wyjście i tak nie wchodzi
Plik: services/engine/zlecenia.py L305-308 (`c = cena_do_kroku(min(max(c, pasmo.dolna), pasmo.gorna), ...)`).
Policzone (venv, sprawdz_zlecenie):
  odniesienie=100, atr_pct=1.0 → prog = 0.1049
  sprzedaż wyjścia z limitem 200 → cena po przyciągnięciu 110.49 (10,5% NAD rynkiem)
  ATR nieznany (prog=0.25), rynek 50, limit 100 → cena 62.50 (25% NAD rynkiem)
Scenariusz (rynek spadający — dokładnie pytanie zlecającego):
 1. Świeca 4h zamyka się na 100 (księga GLOBALNA). Silnik decyduje „exit", plan sell @ 100.
 2. Zanim plan idzie na giełdę, księga UNIJNA jest na 80 (krach / rozjazd danych).
 3. `ocena_pasma`: odchylenie 25% > prog 10,5% → wyjście przyciągane do `gorna`
    = 80*1.1049 = 88.39. Wciąż 10,5% NAD rynkiem.
 4. PostOnly przyjmuje takie zlecenie jako maker — i ono LEŻY. Nie wypełni się.
 5. Po dwóch świecach plan wygasa, wiersz zlecenia anulowany, pozycja NADAL OTWARTA.
    Następna świeca: nowa decyzja exit, ten sam mechanizm. W trwającym spadku wyjście
    goni cenę świeca po świecy, tracąc po 4 h za każdym razem.
Symetrycznie w drugą stronę: gdy c < dolna, przyciągnięcie do `dolna` = 89,5% rynku
nadal PRZECINA spread → PostOnly ODRZUCA złożenie (zloz_plan łapie ExchangeError,
zapisuje status 'unknown', zwraca False) — wyjście nie idzie w ogóle.
Docstring w L270-282 twierdzi „Przyciągnięcie zostawia wyjście wykonalnym" — nieprawda
w obu kierunkach. Wykonalne byłoby przyciągnięcie do `odniesienia` (albo do bliższej
krawędzi), nie do dalszej.
Test tego NIE rozróżnia: tests/test_gielda_exec.py L642-656 sprawdza tylko
`cena_limitu < D("200")` — przechodzi zarówno dla przyciągnięcia do 110.49, jak i do 100.
Brak w ogóle testów jednostkowych `prog_pasma`/`ocena_pasma` (grep po tests/: zero).

## ZNALEZISKO D — prowizja z giełdy księgowana zawsze w walucie kwotowanej, choć na spocie bywa pobierana w kupionym aktywie
Pliki: services/exchanges/base.py L130-142 (`StanZlecenia` NIE MA pola waluty prowizji),
services/engine/gielda_exec.py L~316-321 (`zapisz_wejscie(..., prowizja, teraz, plan["quote_ccy"], ...)`).
Projekt sam wie, że tak jest: `Wypelnienie` (base.py L152-155) ma osobne `prowizja_w`
„bo na spocie prowizja bywa pobierana W KUPIONYM AKTYWIE", a
`uzgodnienie.pozycje_z_wypelnien` odejmuje ją od pozycji. Ścieżka egzekucji tej wiedzy
nie ma — `stan_zlecenia` czyta z Bybita tylko `cumExecFee`, bez `feeCurrency`.
Scenariusz: kupno 10 SOL @ 100 USDC, maker 0,1% → Bybit pobiera 0,01 SOL.
 - `exec.fill.fee = 0.01`, `fee_ccy = 'USDC'` → koszt zapisany jako 0,01 USDC zamiast ~1 USDC
 - `exec.position.fees_paid = 0.01` → `update_equity` odejmuje 0,01 zamiast 1,00;
   kapitał zawyżony o ~99% prowizji wejścia przy KAŻDYM kupnie
 - `exec.position.qty = 10`, a na giełdzie leży 9,99 → różnica 0,1%, dokładnie na progu
   `UZGODNIENIE_TOLERANCJA = 0.001`, więc uzgodnienie tego nie zgłosi (albo zgłosi losowo)
Skutek: krzywa kapitału systematycznie zawyżona o prowizję wejść, czyli o połowę rundy
kosztowej (0,1% z 0,2%) — a projekt sam mierzy przewagę w dziesiątych częściach procenta.

## DOWÓD do ZNALEZISKA A (uruchomiony: dowod_a.py)
Wypełnienie 4/10 + `blad_anulowania=ExchangeError("503")`:
  rodzaj  = czesciowe
  anulowane = []            <-- reszta 6 ZOSTAJE ŻYWA na giełdzie
  UPDATE exec.order SET status='filled', qty=4 ...   (zapisz_wejscie nadpisuje qty wysłaną!)
  INSERT INTO exec.position ... qty=4
  UPDATE journal.planned_order SET status='triggered'   <-- plan zamknięty
  UPDATE exec.order SET status='partial' ...
Czyli: plan zamknięty, pozycja 4, na giełdzie żyje zlecenie na 6. Nic tego nie ponowi.
`dozor.sprawdz_egzekucje` liczy status 'partial' do `przyjete` — czyli jako ZDROWE.
Gdyby kill-switch to zdjął, `anuluj_wejscia` zobaczy wypelniono>0, policzy jako
"zostaje do zaksięgowania" — a plan jest 'triggered', więc NIKT tego nie zaksięguje.

## ZNALEZISKO E — odrzucenie PostOnly ląduje jako 'unknown', czyli jako kandydat na pozycję-widmo, i zostaje tam na zawsze
bybit.py L599-604: każdy niezerowy retCode → ExchangeError.
gielda_exec.zloz_plan L106-118: ExchangeError → INSERT exec.order status='unknown'.
gielda_exec.rozstrzygnij_plan L156-162 (`stan is None`): zamyka TYLKO plan, wiersz
'unknown' zostaje.
dozor.py L509-512: `SELECT client_order_id FROM exec.order WHERE status='unknown'
AND submitted_at < now() - 4h` — bez górnej granicy okna, bez engine/mode.
dozor.py L466-475 (`ocen_egzekucje`): każdy taki wiersz → powiadomienie krytyczne
"Zlecenia o NIEZNANYM stanie ... kandydat na pozycję-widmo", CODZIENNIE, bez końca.
Komentarz przy PostOnly (bybit.py L691-692) zakłada co innego: "trafia do czujki
zdrowia egzekucji, ktora liczy odrzucone do przyjetych" — ale 'unknown' NIE jest
liczone jako 'rejected' i nie wchodzi też do `przyjete`.
Skala: cena limitu = zamknięcie świecy 4h, wysyłka do 5 min później, zmierzony dryf
median 0,08% / p90 0,32% (komentarz loop.py L~1150) >> spread. Kupno jest odrzucane
przez PostOnly dokładnie wtedy, gdy rynek zszedł PONIŻEJ limitu — czyli mniej więcej
w połowie przypadków, i to w tej połowie, którą symulacja liczy jako pewne wypełnienie
(`low <= limit` spełnione trywialnie).

## ZNALEZISKO F (drobne) — poprawka `wyjscie_bez_pozycji` NIE trafiła do bliźniaczej funkcji papierowej
services/engine/loop.py L818-828 (`rozstrzygnij_plany`): `if poz: close_paper(...)`
bez gałęzi `else`. Plan idzie na 'triggered', `st["wypelnione"] += 1`, ani linijki
na ekranie. Dokładnie ten kształt błędu, który naprawiono w gielda_exec (zmiana 5).
Ścieżka papierowa jest tym, z czym porównuje trener, więc cicha „sprzedaż bez pozycji"
zawyża wynik rachunku papierowego wobec produkcji.

## PODSUMOWANIE — kolejność wg groźby
1. A — częściowe wypełnienie + nieudane anulowanie → żywe zlecenie sierota, plan 'triggered'
   (gielda_exec.py L203 vs L211). Pozycja-widmo, ekspozycja zaniżona.
2. E — odrzucenie PostOnly jako 'unknown' + wiersz nigdy niezamykany (gielda_exec.py L157-162,
   dozor.py L509-512). Stały fałszywy alarm + realny rozjazd z backtestem ~50% zleceń.
3. C — pasmo przyciąga wyjście do DALSZEJ krawędzi (zlecenia.py L307); przy PostOnly
   wyjście nie wchodzi w żadnym kierunku. Test tego nie rozróżnia.
4. D — prowizja z giełdy zawsze jako quote_ccy (gielda_exec.py L303 + base.py L130-142),
   a na spocie kupno płaci w aktywie. Kapitał zawyżony o prowizję wejść.
5. B — (część E) wiersze 'unknown' rosną w nieskończoność.
6. F — brak `wyjscie_bez_pozycji` w ścieżce papierowej (loop.py L824).
7. Drobne: `zloz_oczekujace` ma zaszyte "atr_pct__4h" bez horyzontu (gielda_exec.py L357);
   podzapytanie o ATR bez ograniczenia wieku; wyścig w `anuluj_wejscia` (stan → fill → cancel).

## OBSZARY SPRAWDZONE BEZ ZNALEZISK
- `update_equity` / `realized_pnl` z pozycji otwartych — rachunek poprawny, brak podwójnego liczenia.
- `decided_for_bar` / `okno_decyzji` — poprawne; `current_bar` daje świecę domkniętą,
  a `decided_at >= bar_start + długość` jest dokładnie oknem tej świecy.
- Role bazodanowe 0034 — przeczytana migracja + porównana z faktycznymi zapisami
  każdego procesu (silnik, dozorca, czujka, próba, API). Żaden proces nie traci
  potrzebnego prawa.
- `zapisz_wejscie(order_id=...)` — jeden plan = jeden wiersz `exec.order`, potwierdzone testem.
- `RiskKernel` bez limitów, `seed_equity` tylko demo — poprawne i pokryte testami.
- Atrapa `Kursor` — realnie łapie oba błędy, które deklaruje (liczba parametrów,
  CHECK statusu planu, UNIQUE client_order_id).
