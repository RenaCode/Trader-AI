# Czego brakuje

Lista **otwartych** braków, zestawiona z tym, co komercyjne systemy
automatycznego handlu mają jako standard. Każda pozycja jest zweryfikowana
odczytem źródeł, nie dokumentacji.

Pozycje zamknięte znikają z tej listy — historia jest w gicie. Przegląd powstał
2026-09-07 z siedmioma pozycjami; tego samego dnia zamknięto osiem: przepływy
kapitału, ponawianie u giełdy, eksport transakcji, dryf zegara, uzgadnianie
z giełdą, reguły zleceń, wypełnienia częściowe i zdejmowanie zleceń przez
kill-switch.

Trzy pozycje **dopisano po pomiarze**, nie po przeglądzie dokumentacji: brak
reguł handlu w bazie (dotyczył 10 z 12 instrumentów), niezdolność demo podmiotu
unijnego do uzgodnienia sald oraz `|| true` w komendach pięciu podów, które
połykało każdą awarię.

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

Nie ma jeszcze **ani jednego** wypełnienia, więc obie liczby poniżej nadal są
stałymi w kodzie:

```python
POSLIZG_TAKER = 0.0004     # 4 punkty bazowe
POSLIZG_MAKER = 0.0        # zero
```

Waga tego jest policzalna. Runda kosztuje 0,200%, a zmierzona mediana zwrotu na
transakcję jest rzędu dziesiątych procenta. **Błąd o 4 punkty bazowe w tę czy
w tamtą przesuwa wynik o tyle, ile cała zmierzona przewaga.**

Ułamek wypełnienia symulacja przyjmuje jako 1.0 — a do czasu pierwszego pomiaru
**backtest zawyża wielkość pozycji o nieznaną wartość**.

**Narzędzie pomiarowe jest gotowe i czeka na dane** (`services/engine/poslizg.py`).
Liczy poślizg jako odległość między ceną, na której oparta była decyzja, a ceną
z `exec.fill` — osobno dla makera i takera, bo obie stałe dotyczą czego innego.
Ceną odniesienia jest `journal.planned_order.limit_price`, a dla zleceń bez
limitu `journal.decision.target_price`. Znak jest znormalizowany tak, że dodatni
zawsze znaczy „na naszą niekorzyść"; bez tego kupna i sprzedaże kasowałyby się
w średniej. Ułamek wypełnienia idzie tą samą funkcją co w cyklu
(`rozjazd_z_ulamkow`), odczytany z `exec.order.raw`.

Trzy rzeczy, które ten pomiar robi celowo inaczej, niż wyglądałby najprościej:

- **mierzy wyłącznie wiersze z giełdy.** Rachunek papierowy zapisuje cenę jako
  `cena * (1 ± POSLIZG)`, więc policzenie z nich poślizgu oddałoby dokładnie
  założoną stałą i wyglądało na jej świetne potwierdzenie, będąc echem,
- **odróżnia „nie zmierzono" od „zmierzono zero".** Pusta próba zwraca `None`
  i wypisuje `NIE ZMIERZONO`, nigdy `0,00 pb` — zero czytałoby się jak
  potwierdzenie założenia makera z zera obserwacji,
- **progiem jest przedział ufności, nie różnica.** Dopóki przedział obejmuje
  stałą, werdykt brzmi „nieodróżnialne od założenia", a nie „zmierzone".
  Przy próbie jednocyfrowej używany jest rozkład t, nie 1,96.

Wynik jest wypisywany codziennie przez `dozor.py --co poslizg`, wpięty
w `zywotnosc-cron`. Nie wysyła powiadomienia i nie zmienia żadnej stałej:
zamiana jednego zgadywania na inne nie byłaby poprawką.

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
