# Czego brakuje

Lista **otwartych** braków, zestawiona z tym, co komercyjne systemy
automatycznego handlu mają jako standard. Każda pozycja jest zweryfikowana
odczytem źródeł, nie dokumentacji.

Pozycje zamknięte znikają z tej listy — historia jest w gicie. Przegląd powstał
2026-09-07 z siedmioma pozycjami; cztery zamknięto tego samego dnia: przepływy
kapitału (migracja 0031), ponawianie u giełdy, eksport transakcji, dryf zegara.

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

## 1. Ścieżka egzekucji: kod jest, wpięcia nie ma

Adapter potrafi już złożyć zlecenie z limitem (`zloz_zlecenie`), odczytać jego
stan po naszym identyfikatorze (`stan_zlecenia`) i je anulować — z idempotencją
opartą na `orderLinkId` i bez ślepego ponawiania POST-a. **Pętla decyzyjna tego
nie używa**: nadal prowadzi wyłącznie rachunek papierowy.

Do wpięcia brakuje trzech rzeczy, każdej z własnym powodem:

**Uzgadnianie z giełdą.** Nikt nie porównuje naszego obrazu pozycji z tym, co
giełda faktycznie trzyma. Na rachunku papierowym to tautologia; na żywym jest
jedyną obroną przed cichym rozjazdem stanu — a rozjazd w systemie, który sam
składa zlecenia, kończy się podwójną pozycją albo pozycją-widmem.

**Wypełnienia częściowe w pętli.** Adapter je już rozpoznaje
(`StanZlecenia.czesciowe`), ale symulacja zna dwa stany: wypełnione albo nie.
Strategia zaprojektowana na wypełnienia całkowite musi wiedzieć, co zrobić
z połową — i ta odpowiedź musi być taka sama w symulacji i w produkcji, bo
inaczej backtest znowu mierzy inny system.

**Anulowanie planów, które nie weszły.** Zlecenie z limitem jest planem na jedną
świecę. Bez anulowania niewypełnione plany zbierają się na giełdzie i wypełniają
po cenach, których żadna decyzja już nie popiera.

## 2. Poślizg jest ZAŁOŻONY, nie mierzony

```python
POSLIZG_TAKER = 0.0004     # 4 punkty bazowe
POSLIZG_MAKER = 0.0        # zero
```

Dwie stałe. Nigdzie nie ma pomiaru poślizgu faktycznego — mimo że mamy do tego
dane: `market.book_snapshot` trzyma arkusz, więc różnicę między ceną decyzji
a ceną wykonania da się policzyć, gdy tylko pojawi się egzekucja.

Waga tego jest policzalna. Runda kosztuje 0,200%, a zmierzona mediana zwrotu
na transakcję jest rzędu dziesiątych procenta. **Błąd o 4 punkty bazowe w tę czy
w tamtą przesuwa wynik o tyle, ile cała zmierzona przewaga.**

`POSLIZG_MAKER = 0` jest założeniem najmocniejszym: mówi, że zlecenie z limitem
wykonuje się dokładnie po cenie limitu. Jest to prawda z definicji **wtedy, gdy
się wykona** — ale nie obejmuje przypadku, w którym część zlecenia wchodzi po
cenie gorszej.

Pozycja jest drugą stroną poprzedniej: bez egzekucji nie ma czego mierzyć.

## 3. Rozdzielenie uprawnień bazodanowych

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
