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

---

# Druga iteracja: świeca 4 h, 12 instrumentów

Data: 2026-08-27, kilka godzin po pierwszej. Dane: 4 lata (2022-08 … 2026-08),
420 tys. świec godzinowych, 1,1 mln wartości cech, 12 instrumentów.

## Wynik

| Miara | 5 min / 3 instr. | **4 h / 12 instr.** |
|---|---|---|
| Okna informatywne | 29 | **33** |
| Okna dodatnie OOS | 7 (24,1%) | **20 (60,6%)** |
| Mediana Sharpe/transakcję | −0,152 | **+0,153** |
| Najgorsze obsunięcie | −8,02% | **−37,07%** |
| Konfiguracji przetestowanych | 1 520 | 2 400 |
| Stan | degraded | **learning** |

Próg przejścia na live to 70% dodatnich okien. Jest 61%.

## Czy to zasługa kalibracji, czy samego horyzontu?

Porównanie na tych samych danych i tym samym protokole, przy **stałych** wagach
(brak dopasowania, więc wynik jest w pełni out-of-sample):

| Wagi | Okna | Dodatnie | Mediana Sharpe |
|---|---|---|---|
| Założenia początkowe (ręczne) | 40 | 17 (42,5%) | −0,043 |
| Równe (1/7 każda) | 21 | 8 (38,1%) | −0,065 |

Sprawdzałem hipotezę, że kalibracja tylko spłaszcza wagi w stronę równych —
wyuczone wagi są wyraźnie bardziej płaskie niż ręczne założenia. **Hipoteza
okazała się fałszywa**: równe wagi wypadają gorzej niż ręczne. Poprawa nie
bierze się z samego spłaszczenia.

Legalne porównanie brzmi więc: **42,5% (ustalone wagi) → 60,6% (kalibracja
walk-forward)**, przy czym ta druga liczba pochodzi z protokołu, w którym wagi
każdego okna dobierano wyłącznie na jego własnym oknie treningowym.

**Czego NIE wolno z tego cytować.** Uruchomiłem też ocenę uśrednionych wag
produkcyjnych na tych samych oknach, z których je wyliczono — wyszło 62,5%.
Ta liczba jest skażona, bo dane oceny brały udział w doborze wag. Zostawiam ją
tu wyłącznie jako ostrzeżenie, że da się ją policzyć i że wygląda przekonująco.

## Zastrzeżenie, które waży więcej niż wynik

**Najgorsze obsunięcie w oknie testowym wynosi −37%, a skonfigurowany limit
`max_drawdown` to 10% z akcją `killswitch`.**

Symulacja walk-forward **nie stosuje risk kernela**: nie skaluje pozycji, nie
zna limitu ekspozycji, nie zatrzymuje się po serii strat. To znaczy, że wyniki
powyżej nie są osiągalne w tej konfiguracji — realny system zatrzymałby handel
zanim doszedłby do połowy tego obsunięcia.

Poprzednie −8,02% nie jest z tym porównywalne: liczone było wyłącznie na wyniku
zrealizowanym, więc systematycznie zaniżone (patrz przegląd kodu §1.8).

Wniosek: zanim cokolwiek pójdzie na żywo, symulacja musi przejść przez ten sam
risk kernel co produkcja. Dopóki tego nie robi, mierzy strategię, której system
i tak nie wykonałby.

## Interpretacja

Wynik przeszedł z „brak przewagi" (24%, mediana ujemna) do „coś tu jest, ale za
mało" (61%, mediana dodatnia). To istotna zmiana, ale **61% nie jest przewagą** —
przy 33 oknach i instrumentach silnie skorelowanych ze sobą (efektywna liczba
niezależnych obserwacji jest mniejsza niż 33) taki wynik mieści się w rozrzucie,
jakiego można oczekiwać przy niewielkiej przewadze i przy jej braku.

Doszła też selekcja na poziomie meta: horyzont 4 h wybrałem PO zobaczeniu, że
5 min zawodzi. To jest forma wielokrotnego testowania, której licznik hipotez
nie łapie, bo liczy konfiguracje wag, nie decyzje projektowe.

## Co dalej

1. **Risk kernel w symulacji** — bez tego wyniki opisują strategię, której
   system nie wykona.
2. Pozostałe hipotezy z pierwszej iteracji: mikrostruktura orderbooka,
   przekrojowość (siła względna zamiast kierunku), sygnał newsowy.
3. Dopiero potem — kolejny walk-forward, z pełną świadomością, że licznik
   hipotez urósł do 2 400 i każdy następny wynik trzeba o to dyskontować.
