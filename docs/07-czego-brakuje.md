# Czego brakuje

Lista **otwartych** braków, zestawiona z tym, co komercyjne systemy
automatycznego handlu mają jako standard. Każda pozycja jest zweryfikowana
odczytem źródeł, nie dokumentacji.

Pozycje zamknięte znikają z tej listy — historia jest w gicie. Przegląd powstał
2026-09-07 z siedmioma pozycjami. Przez dwie doby zamknięto ich kilkanaście,
a **większość nie pochodziła z tego przeglądu, tylko z pomiaru i z audytu kodu**:
przepływy kapitału, ponawianie u giełdy, eksport transakcji, dryf zegara,
uzgadnianie z giełdą, reguły zleceń, wypełnienia częściowe, zdejmowanie zleceń
przez kill-switch, rozdzielenie uprawnień bazodanowych, puls procesów tła,
bramka promocji modelu, delisting, pasmo ceny i sprzątanie zleceń.

Warto zapisać, skąd brały się najgroźniejsze z nich, bo to mówi więcej niż sama
lista. **Żadna nie objawiała się awarią.** Silnik rozstrzygał co drugą świecę
i meldował „już rozstrzygnięte"; tryb `live` nie miał ani jednego limitu ryzyka
i zwracał werdykt `allow`; cena limitu powstawała z innej księgi niż ta, w której
zlecenie lądowało; wyjście przyciągane do krawędzi pasma lądowało 10% nad rynkiem
i nigdy się nie wypełniało. Wszystkie cztery wyglądały w logu na zdrowie.

---

## Co mamy mocniejsze niż typowy bot

Warto zacząć od tego, bo wyznacza, czego **nie** trzeba dobudowywać.

| rzecz | stan |
|---|---|
| **Dziennik decyzji append-only** | wymuszony triggerem (`0005`), zapisywany PRZED poznaniem wyniku, ze snapshotem cech |
| **Wspólny risk kernel** | te same reguły w produkcji i w symulacji — backtest nie może mierzyć innego systemu |
| **Bitemporalność** | `feat.as_of()` filtruje po `event_time` i `ingested_at`; model nie uczy się na wiedzy z przyszłości |
| **Kill-switch z nadzorcą w osobnym procesie** | `docs/06` |
| **Rozdzielenie kapitału od wyniku** | jednostka uczestnictwa — przelew nie udaje zysku (`0031`) |
| **Metryki wyłącznie out-of-sample** z licznikiem hipotez | nie ma gdzie wpisać „wyniku backtestu" |

Ślad audytowy, który w komercyjnych narzędziach bywa funkcją „instytucjonalną"
za dopłatą, jest tu warunkiem konstrukcyjnym od migracji 0005.

---

## 1. Poślizg i ułamek wypełnienia — mierzone od dziś, jeszcze bez wyniku

Egzekucja na koncie demo jest **włączona** (`--egzekucja gielda --mode demo`).
Plan przestał być rozstrzygany przez świecę, a zaczął przez giełdę: ilość, cena
i prowizja przestały być naszym założeniem.

**Stan na 2026-09-08, sprawdzony w bazie.** W rejestrze jest 18 wypełnień, ale
**wszystkie** mają `raw->>'execution' = 'internal_paper'` — to rachunek
papierowy, czyli nasze własne założenie zapisane do bazy i odczytane z powrotem.
Pomiar je odrzuca i robi to słusznie: liczyłby wtedy `POSLIZG_*` przeciwko
samemu sobie.

Egzekucja giełdowa działa od 2026-09-07 wieczorem (silnik potwierdza w logu:
*„EGZEKUCJA NA GIEŁDZIE (demo) — zlecenia idą naprawdę"*). Od tego czasu
**nie powstał ani jeden plan zlecenia** — 0 w ostatniej dobie, przy progu
wejścia 0,2795. Nie jest to awaria: silnik podejmuje jedną decyzję na świecę
4 h i żaden sygnał nie przekroczył progu. Jest to natomiast powód, dla którego
poniższe liczby wciąż są założeniem.

**Aktualizacja 2026-10-03 (audyt 3).** W rejestrze są 72 wypełnienia, z tego
28 giełdowych (`execution = 'gielda'`). Zleceń giełdowych crypto/demo: 43 —
28 wypełnionych, 9 odrzuconych (6 z nich to odrzucenia PostOnly), 6
anulowanych. Wypełnialność z rozbiciem drukuje od tej daty także
`dozor --co poslizg` (wcześniej tylko `poslizg.py`), a `exec.fill.event_time`
dostaje czas wykonania z zegara giełdy zamiast chwili księgowania.

**Czego to wymaga, żeby przestać być założeniem:** wypełnień Z GIEŁDY, a te
zależą od tego, czy strategia w ogóle otworzy pozycję. Przy zmierzonej częstości
z backtestu (20–60 transakcji na okno 30-dniowe dla dziesięciu instrumentów)
pierwsze powinny pojawić się w ciągu dni, nie tygodni. **Jeśli po tygodniu nadal
będzie ich zero, to samo w sobie jest wynikiem** — znaczy, że próg wejścia albo
reguła limitu nie przepuszczają niczego, i wtedy pytanie brzmi „dlaczego",
a nie „czekamy dalej".

Obie liczby poniżej nadal są stałymi w kodzie:

```python
POSLIZG_TAKER = 0.0004     # 4 punkty bazowe
POSLIZG_MAKER = 0.0        # zero
```

Waga tego jest policzalna. Runda kosztuje 0,200%, a zmierzona mediana zwrotu na
transakcję jest rzędu dziesiątych procenta. **Błąd o 4 punkty bazowe w tę czy
w tamtą przesuwa wynik o tyle, ile cała zmierzona przewaga.**

Ułamek wypełnienia symulacja przyjmuje jako 1.0. `rozjazd_z_symulacja` policzy
go z produkcji, gdy tylko będzie co liczyć — a do tego czasu **backtest zawyża
wielkość pozycji o nieznaną wartość**.

## 2. Demo podmiotu unijnego nie uzgodni sald

Zmierzone, nie założone (`services/exchanges/mozliwosci.py`):

| zdolność | `api-demo.bybit.eu` |
|---|---|
| salda (UNIFIED / SPOT / asset-transfer) | **brak, 404 na każdej ścieżce** |
| metadane klucza | brak |
| prowizje, zlecenia otwarte, historia, wypełnienia | są |

Uzgodnienie działa więc na demo **wyłącznie z rejestru transakcji**, czyli
odpowiada na węższe pytanie: „czy nasz dziennik zgadza się z tym, co giełda
zapisała jako nasze transakcje". Ręczne zamknięcie w panelu i przelew
pozostają na demo **niesprawdzalne** — pełne uzgodnienie z saldami zadziała
dopiero na `api.bybit.eu`.

Znaczy to, że ta konkretna obrona **pierwszy raz zadziała w pełni na żywym
rachunku**. Ryzyko jest ograniczone (ścieżka z rejestru jest przećwiczona
i wspólna), ale nie jest zerowe i musi być wypowiedziane.

## 3. Waluta, w której brzmi obietnica

Risk kernel mierzy w `quote_ccy`, czyli w USDC; próg `min_equity` jest progiem
w USDC. Rail fiatowy podmiotu unijnego to EUR, a właściciel myśli w złotówkach.

Zmierzone na `USDCEUR` (452 dni): zakres 0,8318–0,8806, czyli **5,9% wahania**.
Zmierzony wynik strategii to około **2,9% rocznie**.

Znaczy to, że stan „kill-switch zielony, wszystkie limity w normie, rachunek
10 000 USDC" jest w pełni zgodny ze stanem „właściciel ma o kilka procent mniej,
niż wpłacił". **Bezpiecznik jest z konstrukcji ślepy na tę stratę, bo mierzy
w walucie, w której jej nie widać.**

To nie jest usterka do naprawienia w kodzie, tylko definicja do ustalenia:
w jakiej walucie brzmi warunek „bez minusa". Druga krzywa kapitału jest
w budowie; czy próg ma stać w USDC czy w walucie właściciela, pozostaje jego
decyzją.

## 4. Dryf cech: zmierzony, czujka wpięta — czeka na trzeci odczyt

**Stan na 2026-09-08.** Dryf wynosi **0,69** przy progu trwałości **0,5**
i progu pojedynczego odczytu **1,0**. Czujka jest wpięta w zadanie trenera
i odpowiada: *„stan jeszcze nie utrwalony (za mało przebiegów lub za krótki
odstęp) — cisza"*. Zadziała po trzech kolejnych odczytach powyżej progu,
rozpiętych na co najmniej 48 godzin.

**Aktualizacja 2026-10-03 (audyt 3).** Dryf wynosi **3,27**, a czujka trwałości
zadziałała 03.10 („Dryf cech utrzymuje się"). Liczba 0,69 wyżej jest
historyczna.

Wcześniejsza wersja tej sekcji podawała **PSI 0,695 przy progu 0,25** i twierdziła,
że stan trwale zły milczy na zawsze. Oba zdania były nieprawdziwe i oba
poprawiono:

- **To nie jest PSI.** `train.py` liczy wielkość efektu (d Cohena plus względna
  zmiana rozrzutu). Próg 0,25 pochodzi z literatury o Population Stability Index
  i przeniesiony na tę miarę nie znaczy nic. Interfejs malował od 0,25 na
  czerwono stan, który system uznaje za normalny — poprawione w trzech miejscach
  naraz, z progami branymi teraz z jednego źródła.
- **Stan trwale zły ma własną czujkę.** Dozór zgłaszał wyłącznie ZMIANĘ, więc
  dryf, który wszedł wysoko i tam stanął, był niewidoczny. Drugi tor wymaga
  trzech odczytów rozpiętych na 48 h, bo trener bywa uruchamiany powtórnie tego
  samego dnia i wtedy „trwały" znaczyłoby „chwilowy".

**Co z tego zostaje jako otwarte:** nic strukturalnego. To jest stan
oczekiwania, nie brak mechanizmu — i po trzech nocach trenera rozstrzygnie się
sam, w jedną albo w drugą stronę.

## Czego świadomie NIE robimy

Nie każda luka jest brakiem.

- **Sub-konto na giełdzie.** Standardowe zalecenie: bot handluje z odizolowanego
  salda. U nas konto demo jest z natury odseparowane; przy przejściu na live to
  staje się wymogiem, nie dobrą praktyką.
- **Zlecenia stop-loss i take-profit po stronie giełdy.** Strategia wychodzi
  decyzją na zamknięciu świecy, a nie progiem cenowym. Zlecenie ochronne
  u brokera zmieniłoby strategię, a nie tylko jej wykonanie — to hipoteza do
  zmierzenia, nie brakująca funkcja.
- **Więcej ustawień w interfejsie.** Im więcej pokręteł, tym więcej stopni
  swobody i tym łatwiej dopasować się do przeszłości. Ograniczenie jest celowe.
