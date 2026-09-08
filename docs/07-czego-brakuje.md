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

## 3. ~~Rozdzielenie uprawnień bazodanowych~~ — zrobione (migracja 0034)

Było: silnik, dozorca i API chodziły na tej samej roli `trader_app`, więc nic
nie **wymuszało**, że silnik nie zwolni sobie kill-switcha — nie robił tego, bo
nie miał takiego kodu, a nie dlatego, że nie mógł.

Jest: pięć ról, po jednej na proces. Rola silnika ma na `risk.killswitch_event`
`SELECT` i `INSERT` — czyta zatrzymanie i potrafi go zażądać — ale **nie ma
`UPDATE` ani `DELETE`**, więc próba zwolnienia kończy się `InsufficientPrivilege`
i zatrzymaniem poda. Nie ma też prawa zmienić `risk.limit_def`, co jest tym samym
wyłączeniem bezpiecznika, tylko cichszym: podniesiony próg nie zostawia wiersza
w dzienniku ani nie wysyła powiadomienia.

Rozdział działa w obie strony: dozorca zwalnia (trzy kolumny zwolnienia, bez
prawa przepisania `reason` i `triggered_at`), ale nie może zapisać zlecenia ani
pozycji i nie ma dostępu do zaszyfrowanych kluczy giełdy.

Co z tego nadal zostaje długiem:

- **Jedna baza, nie jedna instancja.** Role dzielą ten sam serwer Postgresa.
  Właściciel schematów (`trader`) może wszystko, a migrator chodzi jego rolą
  w podzie API — kompromitacja obrazu API nadal daje pełny dostęp.
- **Próba pożarowa może podnieść próg kapitału.** Musi, bo na tym polega. Nie
  może natomiast wyłączyć limitu (`enabled`) ani zmienić jego akcji.

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
