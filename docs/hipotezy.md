# Rejestr hipotez

Każda hipoteza strukturalna ma tu wiersz **przed** pierwszym przebiegiem, który ją mierzy:
parametry, kryterium i sposób pomiaru. Wynik i decyzja dopisywane po przebiegu, bez
zmiany parametrów ani kryterium. Zmiana parametrów po zobaczeniu wyniku to nowa
hipoteza z nowym numerem i kolejna próba do licznika N (audyt Fable F-3.3, R3).

| id | data | hipoteza | kryterium | wynik | decyzja |
|---|---|---|---|---|---|
| H-B2 | 2026-10-03 | Odniesienie B2 (momentum przekrojowe z filtrem trendu) jest baseline'em, który ensemble musi pobić | definicja, nie test | — | obowiązuje od pierwszego przebiegu |
| H-1 | 2026-10-03 | Ensemble 4h ma przewagę pod uczciwym modelem wypełnień (STOP/GO 1) | zwrot/rundę po dopłacie > 0 w ≥ 3/5 przesunięć **i** bije B2 przy tej samej ekspozycji w ≥ 60% okien | patrz niżej | patrz niżej |

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

### Wynik H-1

(dopisany po przebiegu, poniżej)
