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

| id | data | hipoteza | kryterium | wynik | decyzja |
|---|---|---|---|---|---|
| H-B2 | 2026-10-03 | Odniesienie B2 (momentum przekrojowe z filtrem trendu) jest baseline'em, który ensemble musi pobić | definicja, nie test | — | obowiązuje od pierwszego przebiegu |
| H-1 | 2026-10-03 | Ensemble 4h ma przewagę pod uczciwym modelem wypełnień (STOP/GO 1) | zwrot/rundę po dopłacie > 0 w ≥ 3/5 przesunięć **i** bije B2 przy tej samej ekspozycji w ≥ 60% okien | zwrot/rundę −11,2 pb (0/5 przesunięć > 0), bije B2 w 61% okien | **STOP** — kalibracja wag ensemble zamknięta, dalej tylko R4 |
| H-2 | 2026-10-03 | Zmiana METODY z audytu T4 (stała siatka okien + status z minimum 5 przesunięć, progi wejścia na wagach IC, horyzont IC przy `Horizon`): wpływ na werdykt toru 4h | opisowe — bez decyzji GO; wynik nie zmienia H-1 | patrz niżej | H-1 STOP obowiązuje |

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
