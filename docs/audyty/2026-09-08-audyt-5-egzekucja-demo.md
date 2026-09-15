# AUDYT 5 — Trader-AI (egzekucja włączona, demo)

Metoda: czytam kod, każde znalezisko ma scenariusz. Notatki dopisywane na bieżąco.

## Robocze (do potwierdzenia)

### R1. `posprzataj_zlecenia` gubi doszłe wypełnienie na wierszu `partial`
Plik: `services/engine/gielda_exec.py:586`
Warunek `if stan.wypelniono > 0 and o["status"] != "partial":` — guard `!= "partial"`
ma nie powtarzać zgłoszenia już zaksięgowanego częściowego wypełnienia, ale
wycisza też KAŻDE dodatkowe wypełnienie na tym samym zleceniu.

Scenariusz (do weryfikacji w kodzie ścieżki partial):
- cykl N: `rozstrzygnij_plan` widzi 5/10, księguje 5, anulowanie reszty PADA
  (`anulowanie_padlo`), plan -> 'triggered', `exec.order.status` = stan.status
  (linia 338), czyli 'partial'.
- między cyklami giełda dopełnia do 10 (albo do 8).
- cykl N+1: `posprzataj_zlecenia` bierze wiersz (plan != pending, status 'partial').
  `stan.wypelniono`=10 > 0, ale `o["status"] == "partial"` -> gałąź zgłoszenia
  POMINIĘTA. Dalej `stan.status == 'filled'` -> wiersz zamknięty jako
  "domkniete", bez `reject_reason`, bez wpisu, bez licznika anomalii.
- Skutek: 5 sztuk realnej pozycji poza dziennikiem, żaden licznik tego nie widzi.

### R0. Punkty 9 i 10 z listy zmian NIE ISTNIEJĄ w tym worktree
`grep -rn "BladIpKlucza|KontoZamrozone|wymaga_czlowieka|_blad_z_kodu" services/ tests/` — 0 trafień.
`services/exchanges/base.py` ma tylko `ExchangeError` i `AuthError`, bez flagi
`wymaga_czlowieka`. `bybit.stan_zlecenia` (`services/exchanges/bybit.py:607-647`)
bierze `lista[0]` i **nie porównuje** `o["orderLinkId"]` z `id_wlasne` — zwraca
`id_wlasne=id_wlasne` (echo własnej wartości), więc wynik zawsze WYGLĄDA na nasz.
HEAD = 59324fd. Audytuję to, co jest.

Skutek gdyby filtr `orderLinkId` po stronie Bybita został zignorowany (co robi
np. przy pustym/nieznanym linku na części endpointów v5): `/v5/order/realtime`
zwraca pierwsze CUDZE otwarte zlecenie na tej kategorii, a `stan_zlecenia`
melduje je jako nasze — `zloz_zlecenie` uzna `juz_istnialo=True` i NIE ZŁOŻY
naszego zlecenia, a `rozstrzygnij_plan` zaksięguje cudze wypełnienie jako nasze.

### R2. `prowizja_ccy` naprawiono TYLKO w `exec.fill`; `exec.position.fees_paid`
i cała arytmetyka kapitału dalej traktują prowizję jako kwotowaną
Pliki: `services/engine/loop.py:643-657` (zapisz_wejscie), `loop.py:454` (update_equity)

`zapisz_wejscie` dostaje `prowizja_ccy` i owszem wpisuje je do `exec.fill.fee_ccy`
(linia 643-650). Ale dwie linie niżej ta sama liczba idzie do
`exec.position.fees_paid` (linia 653-656) **bez waluty** — kolumna jest jedna
i bez jednostki. `update_equity` liczy potem
`sum(realized_pnl - fees_paid)` (linia 454) w domyślnej walucie kwotowanej.

Scenariusz: kupno 1 SOL @ 140 USDC na spocie, Bybit pobiera 0,001 SOL prowizji
(feeCurrency=SOL). `exec.fill.fee_ccy='SOL'`, `fee=0.001` — poprawnie.
`exec.position.fees_paid = 0.001` — i `update_equity` odejmuje 0,001 USDC
zamiast 0,14 USDC. Zaniżenie kosztu ~140x, dokładnie ta awaria, którą komentarz
w `bybit.py:637-647` deklaruje jako naprawioną.
Kierunek: kapitał ZAWYŻONY -> obsunięcie ZANIŻONE -> kill-switch nie strzela.

Wyjście (sprzedaż na spocie) płaci w USDC, więc kolumna `fees_paid` na jednej
pozycji miesza dwie waluty w jednej sumie.

### R5. Ta sama wiedza o walucie prowizji nie została użyta tam, gdzie boli
najbardziej: `exec.position.qty` zostaje BRUTTO, więc WYJŚCIA SIĘ NIE DA ZŁOŻYĆ
Pliki: `services/engine/gielda_exec.py:308-314`, `services/engine/loop.py:652-656`,
`services/engine/loop.py:1368-1370` (plan wyjścia), `services/engine/uzgodnienie.py:174-177`

`co_z_wypelnieniem` zwraca `r.ilosc = stan.wypelniono` — to `cumExecQty`, ilość
BRUTTO. Przy kupnie na spocie Bybit potrąca prowizję z KUPIONEGO aktywa
(`feeCurrency == base_ccy`), więc realnie trzymamy `wypelniono − prowizja`.
`zapisz_wejscie` wpisuje do `exec.position.qty` wartość brutto. Grep potwierdza:
nic w `services/engine/` nie odejmuje prowizji bazowej od ilości pozycji.

`uzgodnienie.pozycje_z_wypelnien` (linia 118-119) prowizję bazową ODEJMUJE —
czyli strona giełdowa liczy netto, a nasza brutto. Naprawiono jedną stronę
porównania.

Scenariusz (kupno 1,0 SOL, maker 0,1%, prowizja 0,001 SOL):
1. `exec.position.qty = 1.0`, faktyczne saldo 0,999 SOL.
2. Sygnał wyjścia -> `loop.py:1368`: `zaplanuj(..., Decimal(position["qty"]))`
   = 1,0. `sprawdz_zlecenie` robi `do_kroku_w_dol(1.0, lot_size)` = 1,0 (nic
   nie zaokrągla w dół, bo 1,0 leży na siatce).
3. Bybit odrzuca sprzedaż 1,0 SOL przy saldzie 0,999 — `zloz_plan` łapie
   `ExchangeError`, wiersz idzie na 'unknown', plan zostaje 'pending'.
4. `rozstrzygnij_plan` -> `stan is None` -> plan 'expired'.
5. Następna świeca: sygnał wyjścia nadal dodatni -> ten sam plan na 1,0 ->
   ta sama odmowa. **Pozycja jest uwięziona i nie ma mechanizmu, który to
   przerwie.** W dzienniku wygląda to na zlecenia składane co świecę.

Drugi skutek, cichszy: `rozjazdy()` porównuje 1,0 z 0,999 -> `wzgledna = 0.001`,
a `TOLERANCJA = 0.001` i warunek to `<=`. Przechodzi DOKŁADNIE na granicy.
Przy stawce makera innej niż 0,100% (inny poziom VIP, promocja, perp) każda
pozycja alarmuje jako "pozycja-widmo" przy KAŻDYM uzgodnieniu.

### R6. `Filtry.kompletne` uznaje `krok_ceny = 0` za regułę znaną — `ValueError`
wywraca CAŁY cykl, razem z wyjściami
Pliki: `services/engine/zlecenia.py:60-63` (`kompletne`), `:87` i `:99`
(`raise ValueError`), `services/engine/gielda_exec.py:382-389` (brak try/except)

`kompletne` sprawdza tylko `None not in (...)`. Kolumna `ref.instrument.tick_size`
to `numeric(28,12)` BEZ `CHECK > 0` (`db/migrations/0001:46`). Wartość 0 lub
ujemna przechodzi przez bramkę reguł, a `cena_do_kroku` rzuca gołym
`ValueError("krok musi byc dodatni")`. Sprawdzone uruchomieniem:
`Filtry(..., krok_ceny=D("0")).kompletne == True`, a `sprawdz_zlecenie` rzuca.

`zloz_oczekujace` (`gielda_exec.py:382`) i `rozstrzygnij_oczekujace` (`:416`)
iterują po planach BEZ `try/except`. Jeden instrument z zerowym krokiem ceny
przerywa pętlę i cofa transakcję — nie wysyłają się także WSZYSTKIE POZOSTAŁE
plany tego cyklu, w tym wyjścia. Wysokość ryzyka zależy od tego, czy 0 da się
wprowadzić do bazy: `bybit._dec_albo_nic` zamienia "0" na None (dobrze), ale
seed `0008` i ręczny UPDATE nie są niczym chronione.

### R7. `cena_makera` przy dużym kroku względem ceny stawia wyjście DALEKO od rynku
Plik: `services/engine/zlecenia.py:198-209`
Sprawdzone uruchomieniem (`sprawdz_zlecenie`, wyjście, odniesienie 1,4, krok 1):
sprzedaż dostaje cenę **2** (+43% nad rynkiem), kupno **1** (−29% pod rynkiem).
Dla par o cenie porównywalnej z krokiem "jeden krok po naszej stronie" przestaje
znaczyć "tuż przy rynku". Jako `PostOnly` sprzedaż po 2 jest PRZYJMOWANA i leży —
czyli dokładnie ten skutek, przed którym docstring ostrzega przy krawędzi pasma.
Przy realnych `tickSize` Bybita (0,0001 dla ceny 1,4) to nie występuje, więc
jest to ryzyko zależne od jakości `ref.instrument.tick_size`, nie codzienne.
Odniesienie tuż przy zerze jest natomiast obsłużone: `cena_makera` zwraca 0
i `if rynkowa > 0` zostawia cenę planową.

### Zmiany, które są w porządku
* pkt 3 (`cena_makera`) — logika i kierunki (long/short, kupno/sprzedaż)
  poprawne, `test_pasmo.py` mocno je pilnuje. Zastrzeżenie tylko R7.
* pkt 4 (`zapisz_wyjscie_bez_pozycji` + `else`) — poprawne, w tym świadoma
  różnica w liczniku wobec ścieżki giełdowej; mutacja łapana.
* pkt 6 (bramka promocji) — `mv_id` jest zawsze NOWYM wierszem, więc
  `zapisz_promocje('candidate')` nie degraduje wersji obecnie aktywnej;
  `zapisz_promocje` wycofuje poprzednią TYLKO przy 'active'; mutacja łapana.
* pkt 7 (najnowszy klucz) — zapytanie poprawne (`created_at DESC, id DESC`),
  ostrzeżenie o dwóch aktywnych kluczach jest. Zastrzeżenie tylko do testu.
* pkt 8 (role 0034/0035) — 0034 daje roli silnika w schemacie `risk` tylko
  SELECT domyślnie, a 0035 jawnie dokłada `GRANT SELECT, INSERT, UPDATE ON
  risk.worker_heartbeat TO trader_silnik` i odbiera zapis czujce/API. Spójne.
* `rozstrzygnij_plan`: `wyslano` z `exec.order.qty` (nie z planu), `order_id`
  przekazywany do `zapisz_wejscie`, `nie_zdjete` zostawiające plan `pending` —
  wszystko poprawne, mutacje łapane.
* Kolejność cyklu (`anuluj_wejscia` -> `rozstrzygnij_oczekujace` -> decyzje ->
  `posprzataj_zlecenia` -> `zloz_oczekujace`) nie tworzy wyścigu: plan
  `pending` jest niewidoczny dla sprzątania, a `anuluj_wejscia` przy
  `wypelniono > 0` celowo nie rusza planu.

### R3. Puls: `news` melduje zdrowie przy WSZYSTKICH kanałach padniętych,
a `detail` nie jest przez nikogo czytany
Pliki: `services/ingest/news_worker.py:735-740`, `services/engine/dozor.py:847-877`,
`services/engine/dozor.py:926-930`

`run_cycle` liczy `s["bledy"]` (padnięte kanały RSS), ale to NIE jest awaria
cyklu — `puls.bij` leci bezwarunkowo po każdym cyklu bez wyjątku. Liczba idzie
do `detail["bledy_kanalow"]`.

`sprawdz_puls_workerow` SELECT-uje `worker, interwal_s, last_beat_at,
awarii_pod_rzad, ostatni_blad, wiek_s, proba_wiek_s` — **`detail` nie jest
w ogóle pobierany**, a `ocen_puls_workerow` patrzy wyłącznie na `wiek_s`.
Grep `worker_heartbeat` po `services/` i `web/`: nikt poza `puls.py` i tym
jednym SELECT-em tej tabeli nie dotyka.

Scenariusz: dostawcy RSS zaczynają odrzucać nasz User-Agent (403 na wszystkich
kanałach). news_worker co cykl: 0 pobranych, N błędów kanałów, `puls.bij` →
`last_beat_at = now()`, `awarii_pod_rzad = 0`, `ostatni_blad = NULL`.
`dozor --co workery` drukuje "żyje news: ostatni udany cykl 0 min temu,
awarii pod rząd 0". Zero alarmu. Cecha `news_sent` zamarza, a jedynym
mechanizmem, który to złapie, jest `--co swiezosc` — czyli dokładnie ta czujka,
od której puls miał być niezależny.

To samo strukturalnie w `market_worker`: pusta lista świec z giełdy (HTTP 200,
`list: []`) nie jest `ExchangeError`, więc `bledy=0`, `swiece=0` i puls bije
zdrowo (`market_worker.py:62-75,144`).

### R4. `test_sprzatanie_NIE_RUSZA_planow_ktore_jeszcze_czekaja` sprawdza tekst SQL,
nie zachowanie
Plik: `tests/test_gielda_exec.py:759-765`
```python
cur, _ = _sprzataj([], Gielda())
(sql, _), = [(s, p) for s, p in cur.sql if "FROM exec.order o" in s]
assert "p.status::text <> 'pending'" in sql
```
Test przechodzi dla `... AND (p.id IS NULL OR p.status::text <> 'pending' OR TRUE)`
i dla każdego innego wariantu, w którym łańcuch nadal występuje. Atrapa kursora
i tak zwraca `[]`, więc żadne zachowanie nie jest wykonane. DO WERYFIKACJI mutacją.

## POTWIERDZONE URUCHOMIENIEM

### R1 — POTWIERDZONE (odpalone `posprzataj_zlecenia` na atrapie z tests/)
Wiersz `exec.order` w stanie 'partial' (u nas zaksięgowane 4 z 10), plan
'triggered'; giełda dopełniła zlecenie między cyklami:

    gielda Filled 10/10         -> {'zdjete': 0, 'domkniete': 1, 'wypelnione_po_fakcie': 0}
                                   UPDATE exec.order SET status='filled' — bez reject_reason
    gielda PartiallyFilled 8/10 -> {'zdjete': 1, 'domkniete': 0, 'wypelnione_po_fakcie': 0}
                                   reject_reason='sprzątanie: zlecenie przeżyło swój plan
                                   i zostało zdjęte' — komunikat NIEPRAWDZIWY

6 (odp. 4) sztuk realnej pozycji poza dziennikiem, zero liczników, zero powodu.
`dozor.sprawdz_egzekucje` (`dozor.py:511-512`) szuka wyłącznie `status='unknown'`,
więc wiersz przestawiony na 'filled'/'cancelled' znika też z tamtej czujki.

### Mutacje — co testy ŁAPIĄ
* `cena_makera` -> stara krawędź pasma: 5 testów w `tests/test_pasmo.py` PADA. OK.
  ALE `tests/test_gielda_exec.py:640` (`test_WYJSCIE_poza_pasmem_jest_PRZYCIAGANE`)
  przechodzi dla OBU wersji — asercja `cena_limitu < D("200")` jest prawdziwa
  i dla krawędzi pasma (110,49) i dla ceny makera (100,01), a docstring testu
  opisuje ZACHOWANIE, KTÓREGO KOD JUŻ NIE MA ("do gornej krawedzi pasma").
* `prowizja_ccy = stan.prowizja_w or ...` -> `plan["quote_ccy"]`:
  `test_prowizja_ksiegowana_w_WALUCIE_Z_GIELDY` PADA. OK.
* `cena_makera` bez przesunięcia o krok (zwraca `w_dol`/`w_gore`):
  6 testów `test_pasmo.py` PADA. OK — `test_pasmo.py` jest mocny.
* bramka promocji `if tryb == "live" and not zgoda` -> `if False`:
  3 testy `test_promocja_modelu.py` PADAJĄ. OK.
* `zapisz_wyjscie_bez_pozycji` usunięte z gałęzi `else`:
  2 testy `test_rachunek_papierowy.py` PADAJĄ. OK.
* `_AWARIA` odświeża `last_beat_at`: `test_awaria_NIE_RUSZA_last_beat_at` PADA. OK.
* `nie_zdjete` usunięte (plan wygasa mimo nieudanego anulowania):
  `test_nieudane_anulowanie_ZOSTAWIA_plan_do_ponowienia` PADA. OK.
* zgłoszenie `wypelnione_po_fakcie` usunięte:
  `test_sprzatanie_ZGLASZA_wypelnienie_po_fakcie` PADA. OK.

### Mutacje — czego testy NIE ŁAPIĄ
* **`posprzataj_zlecenia`: filtr `p.status <> 'pending'` -> `... OR TRUE`
  — CAŁA SUITA PRZECHODZI.** Guard jest nośny: bez niego sprzątanie (które
  w cyklu leci PRZED `zloz_oczekujace`, a PO `rozstrzygnij_oczekujace`)
  zdejmowałoby z arkusza zlecenie planu, którego świeca jeszcze się nie
  zamknęła — czyli każde wejście i wyjście ginęłoby o świecę za wcześnie.
  Jedyny test to `assert "p.status::text <> 'pending'" in sql`.
* `zbuduj_adapter` `ORDER BY created_at DESC` -> `ASC`: pada TYLKO test
  porównujący tekst SQL. `test_po_rotacji_wygrywa_NAJNOWSZY_klucz` przechodzi
  dla obu wersji, bo atrapa połączenia nie wykonuje `ORDER BY` i zwraca listę
  w kolejności zadanej — test sprawdza własną atrapę, nie kod.
  (Drugi test deklaruje to uczciwie w docstringu; pierwszy — nie.)
* `tests/test_gielda_exec.py:640` `test_WYJSCIE_poza_pasmem_jest_PRZYCIAGANE`
  przechodzi dla starej (krawędź pasma) i nowej (cena makera) wersji.


