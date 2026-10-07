# Rejestr hipotez

Każda hipoteza strukturalna ma tu wiersz **przed** pierwszym przebiegiem, który ją mierzy:
parametry, kryterium i sposób pomiaru. Wynik i decyzja dopisywane po przebiegu, bez
zmiany parametrów ani kryterium. Zmiana parametrów po zobaczeniu wyniku to nowa
hipoteza z nowym numerem i kolejna próba do licznika N (audyt Fable F-3.3, R3).

Od 2026-10-03 (audyt Fable T8) `train.py --sucho`, `train.py --panel` i
`scripts/eksperyment.py` wymagają `--hipoteza H-n` z wierszem w tej tabeli —
przebieg bez numeru kończy się odmową. Wersja modelu ze zmianą struktury
`params` względem aktywnej (`train.zmiana_struktury`) zostaje `candidate`
i wchodzi dopiero ręcznym przebiegiem z `--zatwierdz-promocje`, po wpisie tutaj.
Od 2026-10-05 (poprawka F) to samo dotyczy **każdej** nowej wersji, także w demo:
automatycznej promocji `learning` → `active` już nie ma.

| id | data | hipoteza | kryterium | wynik | decyzja |
|---|---|---|---|---|---|
| H-B2 | 2026-10-03 | Odniesienie B2 (momentum przekrojowe z filtrem trendu) jest baseline'em, który ensemble musi pobić | definicja, nie test | — | obowiązuje od pierwszego przebiegu |
| H-1 | 2026-10-03 | Ensemble 4h ma przewagę pod uczciwym modelem wypełnień (STOP/GO 1) | zwrot/rundę po dopłacie > 0 w ≥ 3/5 przesunięć **i** bije B2 przy tej samej ekspozycji w ≥ 60% okien | zwrot/rundę −11,2 pb (0/5 przesunięć > 0), bije B2 w 61% okien | **STOP** — kalibracja wag ensemble zamknięta, dalej tylko R4 · zamknięte decyzją A (2026-10-04) |
| H-2 | 2026-10-03 | Zmiana METODY z audytu T4 (stała siatka okien + status z minimum 5 przesunięć, progi wejścia na wagach IC, horyzont IC przy `Horizon`): wpływ na werdykt toru 4h | opisowe — bez decyzji GO; wynik nie zmienia H-1 | patrz niżej | H-1 STOP obowiązuje |
| H-3 | 2026-10-04 | Tor R4: momentum przekrojowe 30 d na decyzji dobowej z filtrem BTC > EMA 50 d, top 3 long-only, pod pesymistycznym modelem wejść (STOP/GO 2) | wszystkie cztery naraz: K1 okna dodatnie ≥ 65% **i** bije B1 przy tej samej ekspozycji w ≥ 65% okien; K2 DSR ≥ 0,95 na 5/5 przesunięć; K3 percentyl permutacyjny ≥ 70 na 5/5 przesunięć; K4 bije B2 przy tej samej ekspozycji w ≥ 55% okien | K1 NIE (okna dodatnie 46%, bije B1 35%), K2 TAK (DSR 0,950–0,980, N = 2), K3 TAK (percentyl 100), K4 NIE (bije B2 44%); oba warianty ekspozycji | **STOP** — R4 nie wchodzi na demo; żaden tor nie spełnia kryteriów, decyzja właściciela (sekcja 12) · zamknięte decyzją A (2026-10-04); reguła R4 na demo wyłącznie jako pomiar egzekucji E-1 |
| A | 2026-10-04 | Decyzja właściciela po STOP/GO 2 (sekcja 12): zamknięcie poszukiwania przewagi | — (decyzja, nie test) | H-1 STOP, H-3 STOP | **zamknięte z wnioskiem „brak przewagi spełniającej kryteria”**; tor badawczy (ensemble 4h, R4) zamknięty |
| E-1 | 2026-10-04 | **Pomiar egzekucji, NIE hipoteza przewagi.** Reguła R4 „momentum + filtr trendu” z parametrami zamrożonymi z H-3 na koncie demo, 15% kapitału | opisowe, bez GO: realizacja na giełdzie wobec modelu pesymistycznego (wypełnialność wejść, opóźnienie wysyłki, prowizje, wyjście względem ceny decyzji) | w toku od wdrożenia; przerwany 2026-10-07 przed próbą ~30 wejść | **zamknięte decyzją D-1 (2026-10-07)**: R4 przestaje być strategią demo; wynik finansowy NIE jest wynikiem; nic z tego przebiegu nie podlega promocji |
| D-1 | 2026-10-07 | Decyzja właściciela: demo służy do trenowania algorytmu przed live. Silnik demo (crypto 4h i equity 1h) gra **modelem-kandydatem** (najnowsza wersja `candidate` albo nowsza `active`), żeby zbierać dane w przód (live-OOS kandydata) | — (decyzja, nie test); warunek **DODATKOWY** do bramki live, nie zastępujący: ≥ 12 tygodni kandydata na demo **i** test znaków ≥ 8/12 tygodni dodatnich **i** realizacja wobec symulacji w granicy dopłaty karty | w toku od wdrożenia | bramka przejścia na live bez zmian; kandydat nigdy na live (odmowa w kodzie) |
| H-4 | 2026-10-05 | Cotygodniowy trening poprawia model: wersja v_k jest lepsza od v_k−1 **w przód**, na tygodniu zamkniętym po zapisaniu obu | test znaków po oknach: v_k lepsza w ≥ 10 z pierwszych 12 tygodni z różnicą ≠ 0 (p ≈ 0,019); n = liczba okien kalendarzowych, nie transakcji | w toku — szkielet pomiaru, miara okna niezaimplementowana | — |

---

## H-B2: parametry odniesienia B2 (zapisane przed pierwszym przebiegiem)

Źródło: porada Fable R3, audyt Fable T5. Parametry wzięte z literatury o momentum
przekrojowym w krypto, **nie strojone**. Kod: `services/engine/baseline.py`
(`PARAMETRY_B2`, `b2_momentum`).

| parametr | wartość |
|---|---|
| uniwersum | to samo co strategia w danym oknie (instrumenty aktywne w oknie, `tradeable`) |
| sygnał | zwrot od T−30 d do T−1 d (pominięty ostatni dzień) |
| wybór | top 3 po sygnale, long-only, równe wagi (1/3 kapitału na pozycję) |
| rebalans | co 7 dni od początku okna testowego (decyzja na zamknięciu doby UTC) |
| filtr trendu | w rynku tylko, gdy zamknięcie dobowe BTC > EMA 50 dni zamknięć dobowych; sprawdzany codziennie; inaczej gotówka |
| wykonanie | zlecenie rynkowe po zamknięciu następnej świecy 4h, prowizja taker + poślizg 4 pb na nogę, dopłata z karty rynkowej (spread, pył) raz na rundę |
| koniec okna | wszystko domknięte po cenie ostatniej świecy okna |
| porównanie | strategia bije B2, gdy `zwrot_strategii > (zwrot_B2 / ekspozycja_B2) × ekspozycja_strategii`; B2 całe okno w gotówce = zwrot 0, strategia bije je dodatnim zwrotem |

Pozostałe odniesienia (też ustalone z góry):

- **B0**: gotówka 0% i 4% w skali roku na całym kapitale (informacyjnie, bez skalowania ekspozycją).
- **B1**: koszyk równych wag kupiony na początku okna i trzymany (`portfolio.kup_i_trzymaj`), porównanie przy tej samej ekspozycji.
- **B3**: BTC i ETH osobno, long przy zamknięciu dobowym > EMA 50 d, inaczej gotówka, 5% kapitału na instrument.

Ekspozycja strategii w porównaniach to `ekspozycja_przenoszona` (każdy przedział 4h,
przez który pozycja niesie cenę, liczony raz). Do 2026-10-03 porównanie z B1 brało
`avg_exposure`, które gubi jeden przedział na transakcję, także na domknięciu końcem
okna (audyt 2 R-d, audyt Fable F-3.5), czyli zaniżało odniesienie na korzyść strategii.

## H-1: STOP/GO 1, ensemble 4h pod uczciwym modelem wypełnień

Zapisane przed pomiarem.

- **Pytanie**: czy obecny ensemble 4h (wagi IC per reżim, progi per reżim) ma dodatnią
  przewagę po kosztach, gdy symulacja wypełnia zlecenia z limitem tak jak produkcja?
- **Model wypełnień** (`portfolio.Egzekucja`, `train.py --limit --egzekucja-pesymistyczna --opoznienie 20`):
  limit = cena w chwili wysyłki (koniec świecy decyzyjnej + 20 min, interpolacja na
  świecach 1h) minus/plus ½ spreadu instrumentu (`book_spread_bps`, średnia per
  instrument); wypełnienie tylko wtedy, gdy `low` (kupno) / `high` (sprzedaż)
  **kolejnej godziny** dotknie limitu. Niewypełnione wejście przepada, niewypełnione
  wyjście czeka na następną decyzję.
- **Wariant porównawczy**: ten sam przebieg bez `--egzekucja-pesymistyczna --opoznienie`
  (model sprzed zmiany: limit po `close`, wypełnienie w całej następnej świecy 4h).
- **Dane**: kopia bazy produkcyjnej z 2026-10-03 04:34 UTC (`trader_prod_kopia`,
  lokalnie), panel wyeksportowany raz (`scripts/eksport_paneli.py`), `--kind spot_eu --limit`
  (cennik i tryb produkcji), `--wybor ic`, `--trials 60`.
- **Przesunięcia**: `--days` 1400, 1399, 1398, 1397, 1396, czyli siatka okien przesunięta
  o dobę, jak w pomiarze z 27.09.
- **Miary na przesunięcie**: średni zwrot na rundę po dopłacie z karty (pb), odsetek
  okien z Sharpe/transakcję > 0, DSR, wypełnialność symulowana (oczekiwane 60–80%;
  produkcja demo ok. 65%), odsetek okien, w których strategia bije B2 przy tej samej
  ekspozycji.
- **Kryterium GO**: zwrot na rundę po dopłacie > 0 w ≥ 3 z 5 przesunięć **i** bije B2
  w ≥ 60% okien (zbiorczo po 5 przesunięciach). Inaczej **STOP**: kalibracja wag
  ensemble zamknięta, dalsza praca badawcza tylko R4 (horyzont 1d, momentum z filtrem).
- **Czego wynik nie rozstrzyga**: czy 20 min to właściwe opóźnienie (mediana po
  poprawce z 02.10 niezmierzona) i czy godzina to właściwe okno życia zlecenia.
  Wynik przy innych wartościach to osobna hipoteza.

### Wynik H-1 (2026-10-03)

Przebieg: panel z `trader_prod_kopia` (dump 20261003T043403Z, koniec okna 2026-10-03
04:34 UTC, 10 instrumentów `tradeable`, 1402 dni), `scripts/eksperyment.py --kind spot_eu
--limit --trials 60 --wybor ic`, przesunięcia `--days` 1402…1398 (−2…+2 doby wokół 1400
z `trainerCron`). Zero połączeń z produkcją. Uwaga do parametrów: rejestr wyżej wymieniał
1400…1396; pomiar poszedł na 1402…1398 (symetrycznie wokół wartości z `values.yaml`),
kryterium bez zmian.

**(a) model wypełnień sprzed zmiany** (limit po `close`, cała następna świeca 4h):

| --days | okna | dodatnie | transakcji | zwrot/rundę po dopłacie (pb) | DSR | wypełnialność | bije B2 |
|---|---|---|---|---|---|---|---|
| 1402 | 39 | 28 (72%) | 1597 | +54,09 | 0,887 | 100,0% | 31/39 (79%) |
| 1401 | 39 | 28 (72%) | 1583 | +46,78 | 0,728 | 100,0% | 32/39 (82%) |
| 1400 | 40 | 30 (75%) | 1572 | +48,85 | 0,795 | 100,0% | 30/40 (75%) |
| 1399 | 39 | 30 (77%) | 1529 | +44,05 | 0,631 | 100,0% | 29/39 (74%) |
| 1398 | 40 | 26 (65%) | 1535 | +46,32 | 0,714 | 100,0% | 28/40 (70%) |

**(b) model pesymistyczny** (`--egzekucja-pesymistyczna --opoznienie 20`):

| --days | okna | dodatnie | transakcji | zwrot/rundę po dopłacie (pb) | DSR | wypełnialność | bije B2 |
|---|---|---|---|---|---|---|---|
| 1402 | 38 | 21 (55%) | 1334 | −11,64 | 0,0000 | 76,1% | 25/38 (66%) |
| 1401 | 36 | 19 (53%) | 1300 | −13,95 | 0,0000 | 76,1% | 23/36 (64%) |
| 1400 | 37 | 19 (51%) | 1283 | −10,72 | 0,0000 | 75,7% | 21/37 (57%) |
| 1399 | 38 | 18 (47%) | 1291 | −8,61 | 0,0001 | 76,3% | 25/38 (66%) |
| 1398 | 38 | 18 (47%) | 1279 | −10,88 | 0,0000 | 76,3% | 21/38 (55%) |

Zbiorczo po pięciu przesunięciach (średnia / min / max):

| miara | (a) dziś | (b) pesymistyczny |
|---|---|---|
| zwrot/rundę po dopłacie 9,27 pb (pb) | +48,0 / +44,1 / +54,1 | **−11,2 / −14,0 / −8,6** |
| przesunięcia ze zwrotem > 0 | 5/5 | **0/5** |
| okna dodatnie (Sharpe > 0) | 72% (65–77%) | 51% (47–55%) |
| DSR (N = 59) | 0,75 (0,63–0,89) | 0,0000 (0,0000–0,0001) |
| wypełnialność symulowana | 100% | 76% (75,7–76,3%) |
| koszt niewypełnienia (składnik karty, POMIAR) | — | 47 pb/rundę (43–49) |
| bije B1 przy tej samej ekspozycji | 75% | 51% |
| bije B2 przy tej samej ekspozycji | 150/197 = 76% | 115/187 = **61%** |
| bije B3 przy tej samej ekspozycji | 80% | 66% |

Odniesienia w tych samych oknach (średnio na okno 30 dni, wariant b; w wariancie a różnice < 0,3 pp):

| | średni zwrot w oknie (% kapitału) | średnia ekspozycja | okna z zyskiem |
|---|---|---|---|
| strategia (b) | −0,02 | 1,6% | 53% |
| strategia (a) | +0,23 | 1,5% | 73% |
| B0 4% p.a. | +0,33 | 100% | 100% |
| B1 koszyk | +2,68 | 100% | 50% |
| B2 momentum + filtr | +4,37 | 53% | 45% |
| B3 trend BTC/ETH | +0,22 | 4,9% | 35% |

**Werdykt STOP/GO 1: STOP.** Zwrot na rundę po dopłacie > 0 w 0 z 5 przesunięć (próg 3).
Warunek B2 formalnie spełniony (61% ≥ 60%), ale to nie jest przewaga: B2 zarabia
skośnie (45% okien z zyskiem, średnio +4,4% w oknie), więc strategia przy ekspozycji 1,6%
„bije” je w każdym oknie, w którym B2 straciło, nawet tracąc sama.

Co mówi pomiar:

- Cała przewaga z wariantu (a) (+48 pb/rundę) siedzi w założeniu o wypełnieniach.
  Wypełnialność 76% w symulacji (produkcja demo ok. 65%, więc model jest wciąż
  łagodniejszy od rynku), a ruch, który przepada na niewypełnionych wejściach, to
  średnio ok. 2% w 3 świece: wypełniają się wejścia, przy których cena poszła przeciw
  nam, a uciekają te, które by zarobiły (selekcja negatywna, F-3.2).
- DSR spada z 0,75 do zera. Żadne przesunięcie nie zbliża się do progu 0,95.
- Status we wszystkich 10 przebiegach `learning`, więc obecna bramka nie
  wypromowałaby tego modelu w żadnym wariancie. Wynik (a) był jednak czytany jako
  „blisko progu”, a to nie była prawda.

Ograniczenia (nie zmieniają werdyktu, są kandydatami na osobne hipotezy):

- Wyjścia w modelu (b) też są zleceniami z limitem żyjącymi godzinę, a niewypełnione
  czekają do następnej decyzji. Produkcja od T6 (41e6649) po odrzuceniu PostOnly ponawia
  wyjście i przechodzi na takera. Model (b) jest więc dla wyjść surowszy niż produkcja.
  Wpływ tej różnicy NIE jest zmierzony. Koszt niewypełnienia (47 pb) liczony jest
  z utraconych **wejść**, a wyjście takerem na `spot_eu` kosztuje 0,25% zamiast stawki
  makera, więc model „wyjście zawsze wypełnione takerem” nie musi dać wyniku lepszego.
  Do sprawdzenia jako osobna hipoteza (wariant modelu z wyjściem T6).
- Opóźnienie 20 min i godzinne okno życia zlecenia są przyjęte, nie zmierzone (patrz wyżej).
- Pomiar na jednej kopii bazy z jednego dnia; `n_selekcji` = 59 z eksportu.

**Decyzja (zgodnie z kryterium zapisanym przed pomiarem):** kalibracja wag ensemble 4h
zamknięta; dalsza praca badawcza tylko w torze R4. Wariant (b) jest od tej chwili
domyślnym modelem do pomiarów; włączenie `--egzekucja-pesymistyczna --opoznienie 20`
w `trainerCron` to decyzja właściciela (audyt T1: „w CronJobie **on** po akceptacji”).

Odtworzenie pomiaru: `scripts/odtworz_lokalnie.sh` (kopia lokalna) →
`scripts/eksport_paneli.py --days 1402 --do 2026-10-03T04:34:03+00:00` →
`scripts/eksperyment.py --panel … --przesuniecia 1402,1401,1400,1399,1398 --kind spot_eu
--limit [--egzekucja-pesymistyczna --opoznienie 20]`. Równoważność pliku z bazą
sprawdzona tego samego dnia: przebieg `--days 1400` z bazy i z pliku wyeksportowanego
na tę samą chwilę dał identyczne `PODSUMOWANIE_JSON`.

## H-2: zmiana metody T4 — wpływ na werdykt (2026-10-03)

Zapisane przed przebiegiem, którego wynik stoi niżej. **Uczciwie:** jeden
przebieg harnessu na kodzie T4 (weryfikacja, że kod działa) poszedł przed tym
wpisem i jego wynik był widziany; wynik niżej pochodzi z przebiegu powtórzonego
na końcowym kodzie (T4 + T8), z tymi samymi nastawami.

- **Co się zmienia** (jedna zmiana metody, trzy części z audytu T4, wszystkie
  wymagane przez audyt, nie dobrane pod wynik):
  1. granice okien zakotwiczone w `KOTWICA_SIATKI` = 2022-09-01 (+ k × 30 dni),
     werdykt z przesunięć −2…+2 dni, status z minimum (F-2.2);
  2. progi wejścia per reżim liczone na wagach IC, którymi się handluje,
     zamiast na założeniach początkowych (F-2.1) — to zmienia STRATEGIĘ
     (progi produkcji), nie tylko pomiar;
  3. horyzont zwrotu w IC przy `Horizon` (4h: 3 świece, bez zmiany; 1h: 12).
- **Dane i nastawy**: ten sam panel co H-1 (`trader_prod_kopia`, dump
  20261003T043403Z, 1402 dni), `scripts/eksperyment.py --kind spot_eu --limit
  --trials 60 --wybor ic --days 1400`, warianty (a) model wypełnień sprzed T1,
  (b) pesymistyczny `--opoznienie 20`; dla rozdzielenia części 1 i 2:
  (c) = (b) z siatką sprzed T4 (`--od teraz`).
- **Kryterium**: opisowe. H-1 (STOP) **nie** jest tym przebiegiem ponownie
  rozstrzygana: część 2 zmienia strategię po zobaczeniu wyniku H-1, więc
  dodatni wynik tutaj byłby wyborem po fakcie (sekcja 13 pkt 2 i 12 raportu).
  Ponowne otwarcie kalibracji ensemble wymaga nowej hipotezy z kryterium
  zapisanym przed pomiarem, na danych spoza tego panelu.

### Wynik H-2 (2026-10-03)

Kod: gałąź `fable-t2-t4-t8` (ee7a403), panel jak w H-1, `--days 1400`, przesunięcia
−2…+2 dni, `n_selekcji` 59 (z eksportu; nowy klucz `progi_na_wagach` nie dokłada próby, bo `n_konfiguracji_z_listy` liczy bieżący przebieg zawsze raz).

| wariant | siatka | progi na | zwrot/rundę po dopłacie, pb (min / śr / max) | przesunięcia > 0 | DSR (min / max) | transakcji / przesunięcie | okna informatywne | statusy przesunięć → status |
|---|---|---|---|---|---|---|---|---|
| (a) wypełnienia sprzed T1 | kotwica | IC | +65,0 / +68,1 / +70,5 | 5/5 | 0,946 / 0,981 | 844–881 | 31–35 | validated 3/5, learning 2/5 → **learning** |
| (b) pesymistyczny | kotwica | IC | −4,8 / +3,5 / +8,8 | 4/5 | 0,005 / 0,032 | 669–690 | 28–29 | learning 5/5 → **learning** |
| (c) pesymistyczny | sprzed T4 | IC | −8,6 / −1,1 / +5,9 | 2/5 | 0,003 / 0,022 | 652–700 | 29–31 | degraded 1/5, learning 4/5 → **learning** |
| (d) pesymistyczny | kotwica | bazowe (jak przed T4) | −19,7 / −15,6 / −11,8 | 0/5 | 0,0000 / 0,0000 | 1291–1311 | 35–37 | learning 5/5 → **learning** |

Co mówi pomiar:

- **Sama siatka nie zmienia wniosku H-1**: (d) to H-1 (b) na nowej siatce, −15,6 pb
  wobec −11,2 pb, 0/5 przesunięć > 0.
- **Progi na wagach IC zmieniają strategię**: liczba transakcji spada o połowę
  (≈ 680 wobec ≈ 1300 na przesunięcie), średni zwrot na rundę przechodzi z −15,6 na
  +3,5 pb, ale przesunięcia rozjeżdżają się od −4,8 do +8,8 pb, a ta sama zmiana na
  siatce sprzed T4 (c) daje 2/5 > 0. DSR nigdzie nie przekracza 0,04 przy progu 0,95.
  Okien informatywnych jest mniej (28–29 wobec 35–37): przy mniejszej liczbie
  transakcji więcej okien spada poniżej `min_trades`. Średnie odniesień B1/B2 między
  wariantami liczą się więc na INNYCH zbiorach okien i nie są porównywalne.
- **(a) pokazuje wadę F-2.2 na liczbach**: przy tych samych nastawach trzy
  przesunięcia dają `validated`, dwa `learning`. Stary trener pokazałby dowolny
  z tych statusów zależnie od dnia. Nowy daje `learning`.
- `scripts/eksperyment.py` wypisuje dla (b) mechanicznie „GO” (4/5 > 0, B2 68%).
  To **nie** jest rozstrzygnięcie STOP/GO 1: kryterium H-1 dotyczyło strategii
  sprzed zmiany progów, a ta zmiana przyszła po zobaczeniu wyniku H-1.

**Decyzja:** H-1 STOP obowiązuje. Progi na wagach IC wchodzą do produkcji jako
poprawka zgodności symulacja–produkcja (F-2.1), ale pierwsza wersja z nimi zostanie
`candidate` (`learning.zmiana_struktury`: nowy klucz `progi_na_wagach` i progi per
reżim). Promocja wymaga decyzji właściciela (`--zatwierdz-promocje`). Pytanie, czy
ensemble z tymi progami ma przewagę, to nowa hipoteza z kryterium zapisanym przed
pomiarem na danych spoza tego panelu (np. po 4 tygodniach nowych świec).

Odtworzenie: `scripts/eksperyment.py --panel … --kind spot_eu --limit --trials 60
--hipoteza H-2 [--egzekucja-pesymistyczna --opoznienie 20] [--od teraz]`; (d) to
(b) z `train.bramki_na_wagach` podmienionym na progi z `dane.bazowe` (skrypt
pomiarowy poza repo, jednorazowy).

## H-3: STOP/GO 2, tor R4 (momentum dobowe z filtrem trendu)

Zapisane 2026-10-04 **przed** napisaniem kodu pomiaru i przed jakimkolwiek przebiegiem.
Źródło: porada Fable R4, audyt Fable sekcja 12 (tydzień 3, STOP/GO 2). Parametry
z literatury o momentum przekrojowym w krypto, te same liczby co B2 (H-B2), **nie
strojone**. Kod: `services/engine/r4_momentum.py`, uruchomienie
`scripts/eksperyment.py --r4 --hipoteza H-3`. Każda zmiana czegokolwiek niżej po
zobaczeniu wyniku to nowa hipoteza (H-4…) i kolejna próba do N.

- **Pytanie**: czy wolna klasa sygnału (ranking 30-dniowy, decyzja raz na dobę,
  filtr reżimu rynku) ma przewagę po kosztach i pod tym samym pesymistycznym
  modelem wejść, pod którym ensemble 4h jej nie ma (H-1)? I czy dokłada coś
  do B2, czyli do tego samego pomysłu w wersji „rynkowo, co tydzień”?

**Sygnał i decyzja** (stałe):

| parametr | wartość |
|---|---|
| uniwersum | instrumenty aktywne w danym oknie WF (`train.okna_wf`, to samo co strategia, H-1 i B1–B3) |
| sygnał | zwrot od T−30 d do T−1 d (pominięty ostatni dzień), z zamknięć świec 4h znanych w chwili T |
| decyzja | raz na dobę, na zamknięciu doby UTC (zamknięcie świecy 4h 20:00–24:00); pierwsza decyzja okna na pierwszym takim zamknięciu wewnątrz okna (jak B2) |
| wybór | top 3 po sygnale (remis: kolejność alfabetyczna symbolu), long-only, równe wagi przy wejściu, bez dorównywania wag w trakcie trzymania |
| filtr trendu | zamknięcie dobowe BTC > EMA 50 zamknięć dobowych (ta sama funkcja co B2/B3); inaczej cel = gotówka (wszystko wychodzi) |
| wyjście | wyłącznie wypadnięcie z top 3 albo wyłączenie filtrem; **bez** progu `EXIT_RATIO`, bez stopów; mean reversion i reszta sygnałów ensemble **wyłączone** (nie liczone wcale) |
| rebalans | codziennie (różnica wobec B2: B2 zmienia skład co 7 dni) |
| koniec okna | wszystko domknięte po cenie ostatniej świecy okna, takerem (jak B2) |

**Ekspozycja — dwa warianty, oba zapisane z góry, N = 2:**

- **P (budżet produkcyjny)**: łączna ekspozycja = limit produkcyjny
  `max_total_exposure` = 15% kapitału, czyli 5% na pozycję. To „ta sama co
  strategia” w sensie budżetu, którym strategia jest ograniczona.
- **F (pełna, jak B2)**: 1/3 kapitału na pozycję, łącznie do 100%.

Porównania „przy tej samej ekspozycji” są wolne od skali, więc oba warianty
dają te same zwroty na rundę z dokładnością do składnika „pył” karty (zależy od
notionalu). Werdykt wydaje się dla **każdego** wariantu osobno; GO wymaga, by
przynajmniej jeden spełnił wszystkie cztery kryteria, a raport podaje oba.

**Model wykonania** (`--egzekucja-pesymistyczna --opoznienie 20` z H-1, plus wyjście takerem):

- **wejście**: zlecenie z limitem wysłane 20 min po zamknięciu doby; cena wysyłki
  z interpolacji na świecach 1h (`portfolio.wycena_wysylki`), limit = cena − ½
  spreadu instrumentu (`book_spread_bps` per instrument z pliku panelu); wypełnienie
  tylko, gdy `low` kolejnej godziny ≤ limit, po cenie limitu, prowizja maker.
  Niewypełnione wejście przepada; jeśli instrument nadal jest w top 3, kolejna
  próba następuje przy następnej decyzji dobowej (dzień później). Brak świecy 1h
  potrzebnej do wyceny = niewypełnione (liczone osobno).
- **wyjście**: taker (produkcja od T6 przechodzi na takera po odrzuceniu PostOnly),
  cena wysyłki 20 min po zamknięciu doby z tej samej interpolacji, poślizg
  `POSLIZG_TAKER` 4 pb, prowizja taker; wyjście zawsze wypełnione.
- **karta kosztu**: ta sama co w H-1 (`karta_rundy` dla `spot_eu`, spread z pliku
  panelu, pył dla notionalu wariantu), dopłata spoza symulacji odejmowana od każdej
  rundy (`koszty.zwroty_po_doplacie`); niewypełnienie mierzone w symulacji, więc
  do dopłaty nie wchodzi. Odniesienia B1–B3 z kartą rynkową, jak w H-1.

**Dane i siatka**: plik panelu z kopii produkcji (`trader_prod_kopia`, dump
20261003T043403Z, `panele_crypto_4h_20261003.json.gz`, ten sam co H-1/H-2), `--kind
spot_eu`, `--days 1400`, siatka okien toru 4h zakotwiczona w `KOTWICA_SIATKI`
(2022-09-01, krok 30 dni, okno testowe 30 dni, 180 dni przed oknem jako historia —
R4 niczego na niej nie uczy, potrzebuje tylko 30 d momentum i 50 d EMA),
przesunięcia −2…+2 dni. Te same okna testowe co H-2, więc odniesienia B1–B3
są liczone w identycznych oknach.

**Miary na przesunięcie**: liczba okien; okna z ekspozycją > 0 (porównywalne);
okna dodatnie = zwrot okna po dopłacie > 0, liczone wśród okien z ekspozycją > 0
(okno całe w gotówce nie ma czego porównywać — ta sama zasada co
`baseline.bije`, wypisywane osobno); bije B1/B2/B3 przy tej samej ekspozycji
(`baseline.bije`); średni zwrot na rundę po dopłacie (pb); DSR na złączonej próbie
rund po dopłacie (`statystyka.deflowany_sharpe`, N = 2 warianty H-3, odchylenie
prób = podstawienie H0, bo innych konfiguracji R4 nie ma; informacyjnie także
DSR przy N = 59 + 2 z licznika toru 4h); wypełnialność wejść; percentyl permutacyjny.

**Test permutacyjny**: 200 permutacji na przesunięcie (ziarno 20261004). W każdej
permutacji przy każdej decyzji dobowej wartości sygnału są losowo przetasowane
między instrumentami dostępnymi w tej decyzji; filtr BTC, model wykonania, koszty
i okna bez zmian. Statystyka: suma zwrotów okien (% kapitału) po dopłacie.
Percentyl = odsetek permutacji ze statystyką mniejszą od faktycznej (remisy po
połowie). Test sprawdza **wybór instrumentów**, nie timing filtra — timing ocenia
porównanie z B1.

**Kryteria GO** (wszystkie naraz, dla co najmniej jednego wariantu ekspozycji):

- **K1**: okna dodatnie ≥ 65% **i** bije B1 przy tej samej ekspozycji w ≥ 65% okien
  (oba zbiorczo po 5 przesunięciach). Kryterium z sekcji 12 („≥ 65% okien
  dodatnich przy odniesieniu o tej samej ekspozycji”) jest dwuznaczne; z góry
  przyjmuję obie lektury naraz, czyli wersję surowszą.
- **K2**: DSR ≥ 0,95 na 5/5 przesunięć (N = 2).
- **K3**: percentyl permutacyjny ≥ 70 na 5/5 przesunięć.
- **K4**: bije B2 przy tej samej ekspozycji w ≥ 55% okien (zbiorczo).

Niespełnienie dowolnego = **STOP** dla toru R4: nie wchodzi na demo. Jeśli
żaden tor (ensemble H-1, R4 H-3) nie przechodzi, decyzja właściciela z sekcji 12:
B2 na demo jako strategia albo zamknięcie z wnioskiem „brak przewagi”.

**Czego wynik nie rozstrzyga**: czy 20 min to właściwe opóźnienie i czy godzina
to właściwe okno życia zlecenia wejścia (jak w H-1); ok. 39 okien po 30 dni to
mało — przedziały Wilsona przy odsetkach okien są w raporcie, nie tylko punkty.

### Wynik H-3 (2026-10-04)

Kod: gałąź `fable-r4`, `899ec9f` (rejestr `3c41f5b` przed kodem i przed przebiegiem).
Przebieg jeden, na końcowym kodzie, bez wcześniejszego podglądu wyników na danych
produkcyjnych (testy szły wyłącznie na danych syntetycznych). Panel jak w H-1/H-2
(`trader_prod_kopia`, dump 20261003T043403Z), `scripts/eksperyment.py --panel …
--r4 --hipoteza H-3 --days 1400 --kind spot_eu`, przesunięcia −2…+2, 200 permutacji.
Zero połączeń z bazą. Czas: 95 s.

Spread per instrument w modelu wejść (pb): ADA 6,53, ARB 7,34, BTC 0,23, DOGE 7,23,
ETH 0,41, LINK 15,57, NEAR 34,08, SOL 1,63, SUI 4,21, XRP 2,04. Dopłata z karty:
P 8,14 pb, F 7,96 pb na rundę (spread innej księgi 7,93 + pył); odniesienia B1–B3
8,03 pb. Prowizja w symulacji: maker 10 pb przy wejściu, taker 25 pb + poślizg 4 pb
przy wyjściu. Niewypełnienie zmierzone w symulacji: 30 pb/rundę (już w zwrotach).

**Wariant F (pełna ekspozycja, 1/3 kapitału na pozycję):**

| przesunięcie | okna (z ekspozycją) | dodatnie | rund | zwrot/rundę po dopłacie (pb) | Sharpe/rundę | DSR (N=2) | DSR (N=61) | wypełnialność | bije B1 | bije B2 | bije B3 | percentyl perm. |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| −2 | 39 (35) | 15/35 (43%) | 328 | +178,6 | +0,1053 | 0,9695 | 0,5166 | 77,0% | 12/35 (34%) | 16/35 (46%) | 15/35 (43%) | 100,0 |
| −1 | 39 (35) | 16/35 (46%) | 335 | +175,1 | +0,1035 | 0,9709 | 0,5251 | 77,9% | 13/35 (37%) | 15/35 (43%) | 13/35 (37%) | 100,0 |
| 0 | 39 (35) | 17/35 (49%) | 337 | +168,8 | +0,1025 | 0,9681 | 0,5089 | 78,6% | 14/35 (40%) | 16/35 (46%) | 18/35 (51%) | 100,0 |
| +1 | 39 (35) | 16/35 (46%) | 334 | +187,7 | +0,1076 | 0,9795 | 0,5842 | 79,5% | 12/35 (34%) | 15/35 (43%) | 18/35 (51%) | 100,0 |
| +2 | 39 (35) | 16/35 (46%) | 336 | +154,8 | +0,0938 | 0,9504 | 0,4272 | 79,6% | 10/35 (29%) | 15/35 (43%) | 16/35 (46%) | 100,0 |

**Wariant P (budżet produkcyjny 15%)**: te same okna, rundy i odsetki co F (porównania
są wolne od skali); zwrot/rundę +154,6…+187,6 pb, DSR (N=2) 0,9500…0,9794 — minimum
na przesunięciu +2 to 0,95003, czyli K2 przechodzi o włos; bije B3 na +2: 15/35.

Odniesienia w tych samych oknach (średnio na okno 30 dni, po 5 przesunięciach):

| | średni zwrot w oknie (% kapitału) | średnia ekspozycja | zwrot na jednostkę ekspozycji | okna z zyskiem |
|---|---|---|---|---|
| R4 F | +4,94 | 50,8% | +9,7% | 80/195 (41%) |
| R4 P | +0,74 | 7,6% | +9,7% | 80/195 (41%) |
| B1 koszyk | +3,14 | 100% | +3,1% | 105/195 (54%) |
| B2 momentum + filtr (co tydzień, rynkowo) | +4,17 | 53,4% | +7,8% | 83/195 (43%) |
| B3 trend BTC/ETH | +0,21 | 4,9% | +4,3% | 77/195 (39%) |

Kryteria (zbiorczo po 5 przesunięciach, identycznie dla P i F):

| kryterium | wynik | próg | spełnione |
|---|---|---|---|
| K1a okna dodatnie | 80/175 = 46% (CI95 39–53%) | ≥ 65% | NIE |
| K1b bije B1 przy tej samej ekspozycji | 61/175 = 35% (CI95 28–42%) | ≥ 65% | NIE |
| K2 DSR (N = 2) | 0,950–0,980, 5/5 | ≥ 0,95 na 5/5 | TAK (minimum o włos) |
| K3 percentyl permutacyjny | 100, 5/5 | ≥ 70 na 5/5 | TAK |
| K4 bije B2 przy tej samej ekspozycji | 77/175 = 44% (CI95 37–51%) | ≥ 55% | NIE |

**Werdykt STOP/GO 2 dla R4: STOP** (oba warianty). Tor R4 nie wchodzi na demo.

Co mówi pomiar:

- **Ranking ma wartość, strategia nie ma stabilnej przewagi.** Średnio R4 zarabia
  więcej na złotówkę zaangażowaną niż B2 i trzy razy więcej niż koszyk, a zwrot na
  rundę po wszystkich kosztach (+155…+188 pb) jest dodatni na każdym przesunięciu.
  Ale ta średnia stoi na kilku oknach hossy altów (X–XI 2023, X–XI 2024: +35…+82%
  w oknie przy pełnej ekspozycji). W typowym oknie R4 traci: dodatnich 46%,
  lepiej od koszyka przy tej samej ekspozycji 35%, lepiej od B2 44%. To profil
  „długa opcja na trend rynku”, nie przewaga, którą da się potwierdzić na 6 tygodniach
  demo — przez większość miesięcy będzie pod kreską.
- **Zastrzeżenie (przegląd 2026-10-04, T-11):** wszechświat H-3 to DZISIEJSZE pary USDC z Bybit EU zastosowane wstecz na 1400 dni (survivorship) — ranking top 3 nie mógł wybrać monety, która później zniknęła z obrotu. +155..+188 pb na rundę to więc GÓRNE oszacowanie, nie pomiar. Porównania względne (B1/B2, test permutacyjny) dzielą ten sam zbiór i są dotknięte mniej.
- **Filtr trendu kosztuje w rynku bocznym.** Okna, w których BTC krąży wokół EMA 50 d
  (np. okna od 2024-08-21, 2025-08-16, 2026-03-14, 2026-07-12), dają wejście na kilka dni i wyjście ze
  stratą; ekspozycja jest wtedy mała, więc porównanie „na jednostkę ekspozycji”
  wypada źle (2024-08: −25% na jednostkę wobec +7% koszyka; 2026-07: −58% wobec
  −4%). Okna z filtrem
  wyłączonym przez całe 30 dni (4 z 39) są poza porównaniami (ekspozycja 0).
- **R4 a B2.** R4 to B2 z codziennym rebalansem i wejściem limitem. Codzienny
  rebalans podnosi zwrot na jednostkę ekspozycji (+9,7% wobec +7,8%), ale nie
  zmienia profilu: oba wygrywają w mniejszości okien. Pytanie „czy R4 dokłada coś
  do B2” ma odpowiedź: w średniej tak, w typowym oknie nie (44%).
- **DSR zależy od N.** Przy N = 2 (warianty H-3, jak zarejestrowano) DSR 0,950–0,980;
  przy N = 61 (licznik toru 4h + H-3) 0,43–0,58. Minimum przy N = 2 leży na samym
  progu (0,95003), więc K2 jest spełnione formalnie, nie z zapasem.
- **Zastrzeżenie do K3 (bez wpływu na werdykt).** Permutacja zarejestrowana w H-3
  tasuje sygnał przy każdej decyzji osobno, więc losowy ranking zmienia skład co
  dzień: ok. 1 100 rund na przesunięcie wobec 337 faktycznych, czyli trzy razy
  więcej kosztów. Percentyl 100 jest przez to zawyżony. Diagnostyka **po fakcie**
  (nie kryterium): permutacja z jednym losowym przyporządkowaniem symboli na całe
  okno, która zachowuje trwałość sygnału i liczbę rund (mediana 325–336), daje
  percentyl 98,5–100 na 5/5 przesunięć. Wybór instrumentów ma więc wartość także
  po wyrównaniu obrotu. Skrypt: poza repo (`scratchpad/r4/diag_perm.py`).
- Wypełnialność wejść 77–80% (produkcja demo ok. 65%), koszt niewypełnienia 30 pb na
  rundę mierzony w symulacji. Brak dziur w świecach 1h na tych oknach.

**Decyzja (zgodnie z kryterium zapisanym przed pomiarem):** R4 STOP. Razem z H-1
(STOP) żaden tor nie spełnia kryteriów STOP/GO 2. Według sekcji 12 audytu to
**[DECYZJA] właściciela**: B2 na demo jako strategia albo zamknięcie toru badawczego
z wnioskiem „brak przewagi spełniającej kryteria”. Rekomendacja i jej uzasadnienie
w raporcie dla właściciela. Każdy wariant R4 (inne okno, inny filtr, rebalans) to
nowa hipoteza i kolejna próba do N — przy obecnym N = 61 DSR toru i tak nie
przekracza 0,6.

Odtworzenie: `scripts/eksperyment.py --panel panele_crypto_4h_20261003.json.gz --r4
--hipoteza H-3 --days 1400 --kind spot_eu [--logi katalog/]`.

## Decyzja A i E-1: reguła R4 na demo jako pomiar egzekucji (2026-10-04)

**Decyzja A (właściciel, 2026-10-04).** Po H-1 (STOP) i H-3 (STOP) żaden tor nie
spełnia kryteriów STOP/GO 2. Z dwóch wyjść z sekcji 12 właściciel wybrał
zamknięcie poszukiwania przewagi z wnioskiem **„brak przewagi spełniającej
kryteria”**. H-1 i H-3 są zamknięte; ich wyniki i kryteria zostają bez zmian.
Kalibracja ensemble i warianty R4 nie są kontynuowane. Każda nowa praca nad
przewagą to nowa hipoteza z nowym numerem i kryterium zapisanym przed pomiarem.

**E-1 nie jest hipotezą przewagi.** Na koncie demo silnik crypto handluje prostą,
jawnie nazwaną regułą „momentum + filtr trendu” (`momentum_filtr_trendu`), żeby
zmierzyć **egzekucję**, czyli wypełnialność, poślizg, opóźnienie i prowizje,
względem modelu pesymistycznego, pod którym liczono H-1 i H-3. Według H-3 reguła
zarabia w mniejszości okien (46%), więc **przez większość miesięcy będzie pod
kreską. To jest stan oczekiwany, nie awaria.** Zwrot rachunku demo w tym
przebiegu nie jest wynikiem i nie wolno go czytać jako potwierdzenia ani
obalenia H-3: 6 tygodni demo to ułamek jednego okna 30 d przy profilu „kilka
dobrych miesięcy w roku”.

Parametry są **zamrożone**: dokładnie te zmierzone w H-3 (`899ec9f`, wynik
`5952e87`), wariant ekspozycji P. Każda zmiana poniższych wartości to nowa
hipoteza, a nie poprawka. Test `tests/test_strategia_momentum.py` przypina te
liczby.

| parametr | wartość | źródło |
|---|---|---|
| sygnał | zwrot T−30 d … T−1 d z zamknięć świec 4h (`market.candle` 1h → `time_bucket` 4h, jak `train.load_panel`) | H-3 |
| wybór | top 3 po sygnale, long-only, remis alfabetycznie | H-3 |
| filtr | zamknięcie dobowe BTC > EMA 50 zamknięć dobowych (historia 400 dni; różnica EMA wobec pełnego panelu < 1e-6) | H-3 / H-B2 |
| decyzja | skład liczony na zamknięciu doby UTC; **wejścia tylko na świecy 20:00–24:00**, niewypełnione wejście przepada do następnej doby | H-3 |
| wyjście | pozycja spoza składu albo przy wyłączonym filtrze wychodzi na **każdej** świecy 4h, aż wyjdzie | H-3 („wyjście zawsze wypełnione”) |
| budżet | 15% kapitału łącznie, 5% na pozycję, równe wagi przy wejściu, bez dorównywania | H-3, wariant P |
| sufit | własny sufit strategii 15% (pozycje po cenie rynkowej + zlecenia i plany wejść), niezależnie od `risk.limit_def` | E-1 |

**Kod.** `services/engine/strategia_momentum.py` (reguła, budżet, odczyt z bazy;
funkcje z `r4_momentum`, nie kopie), `loop.py --strategia momentum`
(`charts/trader/values.yaml`, `workers.engine`). Bramki konta, kill-switch,
risk kernel, blokada szerokiego spreadu, `decyzja_przeterminowana` i egzekucja
(PostOnly, wyjście takerem po odmowie, T6) są te same co dla ensemble.
Strategia nie czyta modelu: `journal.decision.model_version_id` = NULL,
`journal.decision.strategia` = `momentum_filtr_trendu` (migracja 0091). Pętla
odmawia startu z tą strategią poza `--mode demo`.

**Czego to nie zmienia.** Wersja produkcyjna modelu (v39) i wersje `candidate`
z treningu tygodniowego zostają bez zmian; nic nie jest promowane. Tor equity
zostaje wstrzymany. Likwidacja po kill-switchu zostaje wyłączona. Konfiguracja
konta nie-demo bez zmian.

**Znane odstępstwa od symulacji H-3** (nie korygowane, bo to właśnie mierzymy
albo to są zabezpieczenia produkcji):

- cena limitu i życie zlecenia są produkcyjne (`gielda_exec`: cena makera z arkusza
  tuż przed wysyłką), nie „cena 20 min po decyzji − ½ spreadu, godzina”;
- wejście w instrument ze średnim spreadem z 7 dni > 15 pb jest blokowane (F-1.5),
  więc pozycji może być mniej niż trzy (R4 trzymało wtedy pełny skład);
- wyjście z rotacji zwalnia ekspozycję dopiero po wypełnieniu, więc wejście z tej
  samej decyzji może odbić się od sufitu 15% i przepaść do następnej doby;
- pozycje otwarte przez ensemble przed przełączeniem są traktowane jak każde inne:
  spoza składu wychodzą, w składzie zostają (z wielkością nadaną przez ensemble).

**Pomiar.** `python services/engine/poslizg.py --strategia momentum_filtr_trendu`
(w podzie silnika) wypisuje dotychczasowy raport poślizgu i wypełnialności,
zawężony do decyzji tej strategii, i sekcję „realizacja wobec modelu
pesymistycznego”:

| miara | model (H-1/H-3) | skąd na giełdzie |
|---|---|---|
| wypełnialność wejść (plany, nie wiersze `exec.order`) | 77,0–79,6% (H-3 F); H-1: 76% | `exec.order.status` |
| opóźnienie wysyłki wejścia od zamknięcia świecy | 20 min (przyjęte, niezmierzone) | `exec.order.submitted_at` − zamknięcie świecy decyzji |
| prowizja wejścia / wyjścia | 10 pb maker / 25 pb taker (`spot_eu`) | `exec.fill.fee` (prowizja kupna w walucie bazowej przeliczona po cenie) |
| wyjście względem ceny decyzji | +4 pb (`POSLIZG_TAKER`) plus dryf 20 min | `exec.fill.price` vs `journal.decision.target_price` |
| wypełnione wejście względem ceny decyzji, selekcja negatywna | limit ½ spreadu pod ceną wysyłki; koszt niewypełnienia 30 pb/rundę | `exec.fill`, zwrot naprzód 24/48 h niewypełnionych |

Zapytania filtrują po `journal.decision.strategia`, więc zlecenia ensemble sprzed
przełączenia nie mieszają się z próbą. Przebieg nie ma kryterium GO. Kończy go
decyzja właściciela; po zebraniu próby (rząd 30 rozstrzygniętych wejść) liczby
z raportu trafiają tutaj jako wynik E-1.

---

## Decyzja D-1: demo jako live-OOS modelu-kandydata (2026-10-07)

**Decyzja (właściciel, 2026-10-07).** Demo służy do trenowania algorytmu przed
live. Silnik demo crypto **przestaje grać regułą R4** (`--strategia momentum`,
pomiar egzekucji E-1) i gra **modelem-kandydatem** na koncie demo, żeby zbierać
dane w przód. To samo dotyczy silnika demo equity, który do tej pory czekał na
wersję `active`, a tej bramka promocji bez zgody człowieka nie nadaje
(poprawka F, 2026-10-05), więc o modelu equity demo nie zbierało niczego.
**Bramka przejścia na live nie zmienia się.**

**E-1 zamknięty** tą decyzją, przed zebraniem próby ~30 rozstrzygniętych wejść.
Reguła bez przewagi (H-1, H-3: STOP) mierzyła egzekucję kosztem czasu demo,
który ma iść na kandydata, a egzekucję (wypełnialność, opóźnienie, prowizje,
wyjście wobec ceny decyzji) mierzy równie dobrze każda inna strategia na tej
samej ścieżce giełdowej: `poslizg.py` filtruje po `journal.decision.strategia`,
więc decyzje R4 z 2026-10-04 … 2026-10-07 zostają osobną, małą próbą E-1, a od
wdrożenia D-1 próba egzekucji rośnie na decyzjach `ensemble`. H-1, H-3 i A
zostają bez zmian; D-1 nie jest hipotezą przewagi i nie otwiera kalibracji
ensemble na nowo. Kod reguły (`services/engine/strategia_momentum.py`) zostaje,
powrót do E-1 to podmiana flagi w charcie.

**Nowa rola demo: live-OOS kandydata.** Walk-forward mierzy kandydata na
historii, z której wyszedł; demo mierzy go na tygodniach, których przy
treningu nie było, pod prawdziwą egzekucją. Wynik demo D-1 jest wynikiem
**kandydata**, nie modelu zatwierdzonego.

| co | wartość |
|---|---|
| model | najnowsza wersja `{engine}_signal_weighting_{bar}` o statusie `candidate`, albo `active`, jeśli nowsza (po zatwierdzonej promocji); nigdy `training` ani `retired`; kandydat z wynikiem `degraded` wchodzi |
| przeładowanie | model czytany z bazy co cykl; nowy trening wchodzi w kolejnym cyklu, log „MODEL PRZEŁADOWANY”, `model_version_id` w pulsie |
| ślad | `journal.decision.model_version_id` = wersja kandydata, `strategia` = `ensemble` |
| brak kandydata | `--wymagaj-modelu`: cykl bez decyzji z logiem „BRAK MODELU-KANDYDATA”, puls `bez_modelu` |
| tryb | wyłącznie `--mode demo`: odmowa startu w `loop.main`, odmowa w `run_once` i w `active_model` (`kandydat_tylko_na_demo`); na live tylko `active` |

**Kryterium do bramki live (zapisane przed pomiarem, warunek DODATKOWY).**
Model może przejść bramkę live dopiero wtedy, gdy oprócz wszystkich dotychczasowych
warunków (docs/08-przed-go-live.md, bramka promocji `--zatwierdz-promocje`)
spełnia **wszystkie trzy**:

1. **≥ 12 tygodni** gry tym modelem (tą samą strukturą `params`) na demo;
2. **test znaków: ≥ 8 z 12 tygodni dodatnich** — zwrot tygodnia demo po kosztach
   z giełdy (`exec.fill.fee`), tydzień kalendarzowy UTC; tydzień bez transakcji
   to remis, nie próba, i jest raportowany;
3. **realizacja wobec symulacji w granicy dopłaty karty**: zwrot zrealizowany
   na demo nie odstaje od zwrotu symulacji tych samych decyzji o więcej niż
   `Karta.doplata_poza_symulacja` (`services/engine/koszty.py`) na rundę.

Zmiana struktury modelu (`train.zmiana_struktury`) w trakcie 12 tygodni zeruje
licznik: tygodnie innej struktury nie są tygodniami tego modelu. Żadne z tych
kryteriów nie **zastępuje** bramki live ani nie promuje wersji samo; to warunek
konieczny, nie wystarczający. Miara tygodnia i porównanie z symulacją nie są
jeszcze zaimplementowane (jak H-4 2026-10-05: szkielet do zrobienia przed
pierwszym werdyktem; najwcześniej 12 tygodni po wdrożeniu D-1).

---

## H-4: test w przód „czy trening się uczy” (zapisane 2026-10-05, przed pomiarem)

**Skąd.** Diagnoza z 05.10: trener co tydzień (`40 4 * * 0`) liczy walk-forward
na ~3,8 roku, do którego przybywa 0,5% nowych danych, więc kolejne przebiegi
mierzą prawie to samo. Okna walk-forward nakładają się w 29/30 dni, a zmiana
miary 02-03.10 wyzerowała porównania przebiegów (n = 1). Status `learning`
nie odpowiada więc na pytanie, czy nowa wersja jest lepsza od poprzedniej.
Wynik treningu nie steruje handlem (Decyzja A, silnik `--strategia momentum`),
więc H-4 jest pomiarem, a nie bramką promocji: nic z niego nie promuje się
samo (poprawka F).

**Hipoteza.** Wersja v_k modelu `crypto_signal_weighting_4h` daje lepszy wynik
od v_k−1 na tygodniu, którego żadna z nich nie widziała.

**Pary i okna** (`services/engine/w_przod.py`, `pary_okien`):

- okno = pełny tydzień kalendarzowy, poniedziałek 00:00 UTC → poniedziałek 00:00 UTC;
  pierwsze okno: **2026-10-12** (05.10 hipoteza zapisana w trakcie tygodnia, więc
  ten tydzień odpada);
- v_k = wersja najnowsza zapisana (`created_at`) przed początkiem okna,
  v_k−1 = wersja najnowsza przed początkiem okna poprzedniego. Obie są więc
  zamrożone przed oknem, a v_k jest oceniana dopiero w tygodniu po zapisaniu;
- tydzień bez nowej wersji (trener padł) nie daje pary; dwie wersje przed jednym
  oknem (planowa i ręczne powtórzenie) dają jedną parę;
- okna są rozłączne z konstrukcji, więc **n = liczba okien**.

**Miara okna.** Zwrot portfela reguły ensemble z `params` danej wersji (wagi,
progi per reżim) na tym tygodniu, po kosztach `spot_eu` w modelu pesymistycznym
(jak H-1), przy tej samej ekspozycji dla obu wersji, na uniwersum decyzyjnym
z chwili początku okna, wyłącznie danymi sprzed każdej decyzji.
Różnica d = miara(v_k) − miara(v_k−1). d = 0 (obie bez transakcji albo
identyczne decyzje) to remis: nie jest próbą, jest raportowany.

**Kryterium (zapisane przed pomiarem).** Werdykt zapada po **pierwszych 12
oknach z d ≠ 0** i tylko z nich (bez dokładania okien, zatrzymanie warunkowe
to nie test):

- **UCZY SIĘ**: v_k lepsza w ≥ 10 z 12 (jednostronny test znaków, p ≈ 0,019);
- **NIE WYKAZANO**: w każdym innym wypadku. Wtedy cotygodniowy trening nie ma
  dowodu, że cokolwiek poprawia, a dalsze decyzje (rzadszy trening, krótszy
  panel, zatrzymanie) należą do właściciela i są nowymi hipotezami.

Zmiana kodu miary, cech albo kryterium w trakcie zbierania okien przerywa serię:
dalszy pomiar to nowa hipoteza z nowym numerem. Najwcześniejszy termin werdyktu
to 12 tygodni po 2026-10-12, czyli **2027-01-04**.

**Stan na 2026-10-05: SZKIELET.** `services/engine/w_przod.py` liczy pary i okna
z `learn.model_version` oraz werdykt z listy różnic (testy:
`tests/test_w_przod.py`). **Brakuje miary okna** (`miara_okna` rzuca
`NotImplementedError`, nie zwraca zera). Co dalej: panel z bazy obcięty do
[od, do) z historią do rozgrzania sygnałów, `train.precompute_panels`
i `train.simulate_portfolio` z `params` wersji i kartą `spot_eu`, wynik
każdej pary zapisany w bazie razem z `git_sha` kodu miary, żeby seria nie
mieszała dwóch definicji. Okna da się policzyć wstecz po dopisaniu miary,
bo wersje i świece zostają w bazie; okno, które nie istniało przed jego
początkiem, nadal jest testem w przód.

**Uwaga do miary: wiadomości (poprawka C, 2026-10-05).** Od wdrożenia tej zmiany
symbol z analizy LLM wiąże artykuł z **wszystkimi** aktywnymi instrumentami tego
samego aktywa bazowego w tym samym silniku (ETHUSDT → ETHUSDT i ETHUSDC;
`news_worker.mapa_instrumentow`). Wcześniej tylko z symbolem wypisanym dosłownie,
a model zwykle wybierał parę USDT, którą nie handlujemy (uniwersum decyzyjne stoi
na USDC od 0023). **Zmiana działa tylko do przodu, bez uzupełniania wstecz:**
`news.article_instrument` nie ma `ingested_at`, więc powiązanie dopisane dziś
do starego artykułu weszłoby do cech świec, w których tej wiedzy jeszcze nie
było, czyli przeciek przyszłości. Skutek dla pomiaru: cechy wiadomości par USDC
zmieniają rozkład od daty wdrożenia. Panel walk-forward obejmujący tę datę łączy
dwa reżimy tej cechy, a czujka dryfu może się na niej odezwać. W H-4 obie wersje
każdej pary są oceniane na tym samym oknie i tych samych cechach, więc zmiana
wchodzi do obu ramion jednakowo.
