# Pierwszy walk-forward: wynik negatywny

Data: 2026-08-27. Dane: 90 dni realnych świec 1-minutowych z Bybita
(BTCUSDT, ETHUSDT, SOLUSDT), 697 tys. wartości cech.

## Wynik

| Miara | Wartość |
|---|---|
| Okna walk-forward (informatywne) | 29 |
| Okna dodatnie out-of-sample | **7 (24,1%)** |
| Mediana Sharpe na transakcję | **−0,152** |
| Najgorsze obsunięcie w oknie testowym | −8,02% |
| Przetestowanych konfiguracji wag | 1 520 |
| Stan | **degraded** |

Próg przejścia na live to 70% dodatnich okien. Wynik jest od niego odległy
i **nie jest to kwestia dostrojenia** — 1 520 przetestowanych konfiguracji wag
nie dało układu, który wychodziłby dodatni poza próbką.

Wersja modelu została zapisana jako `candidate`, **nie** `active`. Silnik dalej
pracuje na jawnych założeniach początkowych; żadne przeuczone wagi nie zostały
promowane. To zadziałało tak, jak miało.

## Diagnoza: to nie są koszty, to brak przewagi

Rozdzielenie dwóch hipotez przez uruchomienie tej samej symulacji z kosztami i bez:

| | Sharpe / transakcję | Zwrot łączny | Średni zwrot na transakcję |
|---|---|---|---|
| Z kosztami | −0,204 | −65,95% | −0,084% |
| **Bez jakichkolwiek kosztów** | **−0,009** | **−2,93%** | **−0,004%** |

Koszt rundy: 0,190% (prowizja 0,055% × 2 + poślizg 0,04% × 2).

**Przewaga brutto wynosi −0,004% na transakcję, czyli statystycznie zero.**

To rozróżnienie jest kluczowe. Gdyby brutto wynosiło np. +0,15% przy koszcie
0,19%, wniosek brzmiałby „handluj rzadziej i taniej" — sygnały miałyby treść,
tylko egzekucja by ją zjadała. Tutaj brutto jest zerowe, więc rzadszy handel
oznacza jedynie wolniejsze tracenie.

**Obecny zestaw sygnałów nie ma zdolności predykcyjnej na świecach
5-minutowych.**

## Dlaczego to był przewidywalny wynik

Sygnały w wersji pierwszej to podręcznikowe wskaźniki techniczne — RSI, iloraz
EMA, zwroty, z-score wolumenu, pozycja w zakresie — zastosowane do
pięciominutowych świec najpłynniejszych par krypto. To najbardziej zarbitrażowany
fragment rynku, obserwowany przez tysiące uczestników z lepszą infrastrukturą.
Znalezienie tam przewagi tymi narzędziami byłoby zaskoczeniem, nie normą.

Wartością tego etapu nie jest wynik, tylko to, że **system go wykrył i pokazał**,
zamiast wyprodukować ładny backtest. Dokładnie po to zbudowano walk-forward,
licznik hipotez i blokadę promocji.

Warto też zauważyć: 24% dodatnich okien to **mniej niż 50% oczekiwane od rzutu
monetą**. Przy zerowej przewadze brutto i realnych kosztach tak właśnie wygląda
rozkład — nie ma tu paradoksu.

## Co dalej — hipotezy warte przetestowania

Kolejność odzwierciedla stosunek szansy na przewagę do kosztu wdrożenia.

1. **Rzadsze decyzje na dłuższym horyzoncie.** 1 657 transakcji w 90 dni na trzech
   instrumentach to ~6 dziennie na instrument. Przy dowolnej realnej przewadze
   ta częstotliwość jest zabójcza. Świece 1 h / 4 h zamiast 5 min.
2. **Mikrostruktura orderbooka.** Zbieramy już snapshoty (imbalance, spread),
   ale żaden sygnał ich nie używa. Nierównowaga kolejki ma udokumentowaną,
   krótkoterminową wartość predykcyjną — w przeciwieństwie do RSI.
3. **Funding rate perpetuali.** Koszt utrzymania pozycji jest bezpośrednim
   sygnałem pozycjonowania rynku i jest publiczny.
4. **Przekrojowość zamiast kierunku.** Siła względna BTC/ETH/SOL wobec siebie
   zamiast prognozowania kierunku każdego z osobna — pozycja neutralna rynkowo
   eliminuje największe źródło szumu.
5. **Sygnał newsowy z filtrem nowości.** Jedyny nieprzetestowany element
   pierwotnego projektu; wymaga klucza Anthropic.

Każda z nich to nowa hipoteza, którą trzeba przepuścić przez ten sam walk-forward.
Licznik przetestowanych konfiguracji rośnie dalej — i o to chodzi, żeby przy
ewentualnym późniejszym sukcesie dało się uczciwie zdyskontować wynik przez
liczbę podejść.

## Czego NIE należy robić

- **Nie dostrajać progów wejścia/wyjścia, aż wynik wyjdzie dodatni.** Przy 29
  oknach i zerowej przewadze brutto zawsze da się znaleźć próg, który wygląda
  dobrze. To jest definicja przeuczenia i dlatego istnieje licznik hipotez.
- **Nie wydłużać okna treningowego w nadziei na lepsze dopasowanie.** Problem
  nie leży w za małej próbce do dopasowania, tylko w braku sygnału do dopasowania.
- **Nie promować modelu `candidate` ręcznie.** Blokada jest jedyną rzeczą, która
  dzieli ten system od handlowania na podstawie szumu.
