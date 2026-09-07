# Czego brakuje

Lista **otwartych** braków, zestawiona z tym, co komercyjne systemy
automatycznego handlu mają jako standard. Każda pozycja jest zweryfikowana
odczytem źródeł, nie dokumentacji.

Pozycje zamknięte znikają z tej listy — historia jest w gicie. Przegląd powstał
2026-09-07 z siedmioma pozycjami; tego samego dnia zamknięto sześć: przepływy
kapitału (migracja 0031), ponawianie u giełdy, eksport transakcji, dryf zegara,
uzgadnianie z giełdą i reguły zleceń.

Dwie pozycje **dopisano po pomiarze**, nie po przeglądzie dokumentacji: brak
reguł handlu w bazie (dotyczył 10 z 12 instrumentów) i niezdolność demo
podmiotu unijnego do uzgodnienia sald.

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

## 1. Ścieżka egzekucji: elementy są, pętla ich nie używa

Gotowe i przetestowane: składanie zlecenia z idempotencją (`orderLinkId`),
odczyt stanu po naszym identyfikatorze, anulowanie, uzgadnianie z giełdą,
reguły zaokrąglania (`zlecenia.py`) i decyzja o wypełnieniu częściowym
(`egzekucja.py`). **Pętla decyzyjna nadal prowadzi wyłącznie rachunek
papierowy** — przełączenie jej to osobna decyzja, nie brakujący kod.

Do wpięcia zostaje jedno: **anulowanie planów, które nie weszły**. Zlecenie
z limitem jest planem na jedną świecę; bez anulowania niewypełnione plany
zbierają się na giełdzie i wypełniają po cenach, których żadna decyzja już nie
popiera. Decyzja o anulowaniu jest już podjęta w `co_z_wypelnieniem`
(`anuluj_reszte`), ale nikt jej jeszcze nie wykonuje na giełdzie.

## 2. Ułamek wypełnienia jest ZAŁOŻONY na 1.0

Symulacja wypełnia w całości każde zlecenie, które dotknęło limitu. Produkcja
zna trzeci stan i przy większych zleceniach jest on częsty.

Ułamek jest teraz **zapisywany przy każdym zleceniu** i policzalny
(`rozjazd_z_symulacja`), ale nie ma jeszcze ani jednego pomiaru, bo nie ma
egzekucji. Zmyślanie tego ułamka w symulacji byłoby gorsze niż jego brak:
z samych świec OHLC nie da się go wyprowadzić bez założeń o kolejce w arkuszu,
a założenie przebrane za pomiar jest najgorszym rodzajem liczby.

**Dopóki nie ma pomiaru, backtest zawyża wielkość pozycji o nieznaną wartość.**

## 3. Poślizg jest ZAŁOŻONY, nie mierzony

```python
POSLIZG_TAKER = 0.0004     # 4 punkty bazowe
POSLIZG_MAKER = 0.0        # zero
```

Waga tego jest policzalna. Runda kosztuje 0,200%, a zmierzona mediana zwrotu
na transakcję jest rzędu dziesiątych procenta. **Błąd o 4 punkty bazowe w tę czy
w tamtą przesuwa wynik o tyle, ile cała zmierzona przewaga.**

`POSLIZG_MAKER = 0` jest założeniem najmocniejszym: mówi, że zlecenie z limitem
wykonuje się dokładnie po cenie limitu. Jest to prawda z definicji **wtedy, gdy
się wykona** — ale nie obejmuje przypadku, w którym część zlecenia wchodzi po
cenie gorszej. Endpoint `/v5/execution/list` na demo działa, więc pomiar będzie
możliwy od pierwszego zlecenia.

## 4. Demo podmiotu unijnego nie uzgodni sald

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

## 5. Rozdzielenie uprawnień bazodanowych

Silnik, dozorca i API chodzą na tej samej roli `trader_app`. Nic nie **wymusza**,
że silnik nie zwolni sobie kill-switcha — dziś tego nie robi, bo nie ma takiego
kodu, a nie dlatego, że nie może. Świadomy dług, zapisany też w `docs/06`.

---

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
