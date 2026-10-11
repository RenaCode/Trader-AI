# Brama 0: giełdy dostępne z UE i koszt rundy na koncie docelowym

Stan na 2026-10-11 (niedziela, po przebiegu trenera `trader-trainer-29861440`).
Zlecenie właściciela po audycie strategii z 10.10: „giełda/cennik to jedyna
dźwignia o właściwej skali — jeśli żadna giełda dostępna z UE nie daje rundy
≤ 12 pb, projekt kończy się wynikiem negatywnym i to jest wynik".

Zero zleceń, zero wywołań API z kluczami. Z sieci wzięto wyłącznie publiczne
księgi (odczyt) i publiczne cenniki. Każda liczba ma oznaczenie:

- **[Z]** zmierzone — na naszym koncie albo z naszych danych;
- **[S]** zmierzone jednorazowo z zewnątrz — snapshot publicznej księgi, nie
  30 dni własnych zleceń;
- **[D]** deklarowane — cennik publiczny giełdy (z URL i datą odczytu); **[D2]**
  deklarowane przez źródło wtórne, bo strona giełdy odmówiła robotowi;
- **[A]** założone; **[N]** nie sprawdzone.

Tablica kandydatek i werdykty są **liczone**, nie przepisane:
`services/engine/koszty.py`, `KANDYDACI_BRAMY_0`, `brama_0_kandydatki`,
`zapas_na_niewypelnienie_pb`; test `tests/test_brama_0_kandydaci.py` pilnuje,
że każda kandydatka z tablicy ma tu wiersz i że żadna nie wychodzi OTWARTA
z samych deklaracji.

---

## 1. Definicja bramy i co z niej wynika dla pomiaru

`koszty.brama_0` (PR #19): **runda = 2 × maker + spread + niewypełnienie ≤ 12 pb**,
gdzie:

| składnik | wymagane źródło (docs/08) | co mam dziś |
|---|---|---|
| maker | `/v5/account/fee-rate` (lub odpowiednik) z konta **docelowego**, nie demo | bybit.eu: [Z] z konta **demo**; pozostałe giełdy: [D]/[D2] |
| spread | 30 dni `book_snapshot` **własnej** księgi | bybit.eu: 7,03 pb z karty 11.10 (częściowo obca księga, 30 dni); wszystkie giełdy: [S] jeden odczyt 11.10 |
| niewypełnienie | koszt z demo, ≥ 100 rozstrzygniętych wejść | demo bybit.eu: −152 pb/48 h na 12 niewypełnionych (ujemny → 0, n = 12); symulacja pesymistyczna 11.10: **0,400 % rundy** (świece globalne); inne giełdy: **nie da się zmierzyć z zewnątrz** |

Próg 12 pb wynika z IC 0,03–0,04 i σ zwrotu 12 h 3,2 % (docs/03): przewaga brutto
≈ 10–13 pb na rundę. Z definicji funkcji: **ZAMKNIĘTA**, gdy suma znanych
składników > 12 pb (pozostałe są nieujemne); **OTWARTA** wyłącznie przy komplecie
pomiarów z konta docelowego; inaczej **NIEROZSTRZYGNIĘTA**. Z tego wynika, że
analiza cenników może brame tylko **zamknąć** — otworzyć ją może wyłącznie demo
na giełdzie docelowej.

Uwaga do logu z 11.10: sekcja `BRAMA 0` jeszcze się w nim nie wypisała — przebieg
poszedł obrazem sprzed scalenia PR #19. Pojawi się od przebiegu 18.10. Liczby
z karty kosztu (prowizja 0,500 %, spread 0,0703 % podłoga, niewypełnienie
0,400 % w wariancie pesymistycznym) są w logu i stąd je biorę.

---

## 2. Ramy prawne (stan 2026-10-11)

- **MiCA/CASP.** Polska **nie ma** ustawy wdrażającej (trzecie weto prezydenta
  12.02.2026). Okres przejściowy dla podmiotów krajowych skończył się
  **1.07.2026**; KNF nie wydaje zezwoleń. Rezydent PL może korzystać z CASP
  licencjonowanego w innym kraju UE na zasadzie paszportu — i tylko z takiego.
  Źródła: comparic.pl (weto 12.02.2026), bankier.pl „Polska w ogonie Europy",
  comparic.pl „stanowisko KNF, kluczowa data 1 lipca 2026" (odczyt 2026-10-11).
- **Perpetuale = CFD.** Oświadczenie ESMA z **24.02.2026** (ESMA35-243228190-8024):
  kontrakty „perpetual" oferowane detalowi to CFD w rozumieniu MiFID II —
  limity dźwigni (krypto **2:1**), ochrona przed ujemnym saldem, zamknięcie
  przy 50 % depozytu, zakaz zachęt. Dla strategii long-only bez dźwigni to
  nie jest ograniczenie; jest nim **ryzyko regulacyjne produktów**: OKX X-Perps
  i Coinbase „perpetual-style" mają sztuczny 5-letni termin właśnie po to,
  by nie być CFD. Źródła: esma.europa.eu/node/228326, aoshearman.com,
  regulationtomorrow.com (odczyt 2026-10-11).
- **Binance** nie ma MiCA: od 1.07.2026 Binance Poland nie przyjmuje nowych
  klientów, usługi ograniczone (parkiet.com, unchainedcrypto.com). **Bitget**
  bez licencji (wniosek w FMA). **Gemini** zamknęło konta EEA 6.04.2026.
  **Deribit** (Dubaj/Panama) jest poza UE. Te cztery są w tabeli dla
  kompletności, ze statusem „niedostępna".

---

## 3. Kandydatki: runda wg wzoru Bramy 0

Prowizja = 2 × maker na najniższym poziomie **bez VIP i bez tokenu giełdy**
(konto małe, obrót demo ~1,6 transakcji/dobę). Spread = mediana pełnego spreadu
(ask − bid)/mid po 10 aktywach uniwersum (`BTC ETH SOL DOGE XRP SUI ADA NEAR
ARB LINK`), 12 próbek co 2 s, **2026-10-11 17:48 UTC** (niedziela wieczór —
rynek spokojny, liczby raczej zaniżone niż zawyżone). „Zapas" = 12 − prowizja
− spread: tyle zostaje na niewypełnienie.

| giełda (`nazwa` w kodzie) | rynek | podmiot / licencja | dostęp PL | maker / taker | status stawki | prowizja pb | spread pb [S] | suma znana | zapas | werdykt |
|---|---|---|---|---|---|---|---|---|---|---|
| `bybit_eu` | spot USDC | Bybit EU GmbH, FMA AT (MiCA) | tak | 0,25 / 0,25 % | **[Z] konto demo**; [D] flat 0,25 % od 5.10.2026 | 50,0 | 2,46 (karta 30 d: 7,03) | 52,5 | −40,5 | **ZAMKNIĘTA** |
| `okx_eu_usdc` | spot USDC | OKX Europe, MFSA MT (MiCA) | tak | 0,08 / 0,10 % | [D2]; **0,20 / 0,35 % bez konta X-Perps** | 16,0 | 3,21 | 19,2 | −7,2 | **ZAMKNIĘTA** |
| `okx_eu_eur` | spot EUR | j.w. | tak | 0,08 / 0,10 % | [D2] | 16,0 | 7,91 | 23,9 | −11,9 | **ZAMKNIĘTA** |
| `bitvavo` | spot EUR | Bitvavo B.V., AFM NL (MiCA) | tak | 0,15 / 0,25 % | [D2] (strona 403) | 30,0 | 1,48 | 31,5 | −19,5 | **ZAMKNIĘTA** |
| `kraken_pro` | spot EUR | Payward Europe Solutions, CBI IE (MiCA) | tak | 0,40 / 0,80 % (Level 1, od 9.07.2026) | [D] | 80,0 | 1,13 | 81,1 | −69,1 | **ZAMKNIĘTA** |
| `coinbase_advanced` | spot EUR | Coinbase Europe, CSSF LU (MiCA) | tak | 0,60 / 1,20 % (Intro) | [D2] | 120,0 | 2,57 (7/10 aktywów) | 122,6 | −110,6 | **ZAMKNIĘTA** |
| `bitstamp` | spot EUR | Bitstamp Europe, CSSF LU (MiCA) | tak | 0,30 / 0,40 % (> 1 000 USD/30 d) | [D2] | 60,0 | — | 60,0 | — | **ZAMKNIĘTA** |
| `kucoin_eu` | spot USDC | KuCoin EU, FMA AT (MiCA) | [N] start operacji | 0,10 / 0,10 % | [D2] | 20,0 | 4,85 (9/10) | 24,9 | −12,9 | **ZAMKNIĘTA** |
| `cryptocom_exchange` | spot USD | Crypto.com, MFSA MT (MiCA) | [N] czy Exchange w licencji | 0,075 / 0,075 % | [D2] | 15,0 | 0,72 | 15,7 | −3,7 | **ZAMKNIĘTA** |
| `binance` | spot USDC | brak MiCA | **nie** (od 1.07.2026) | 0,10 / 0,10 % | [D] | 20,0 | 0,89 | 20,9 | −8,9 | ZAMKNIĘTA (i niedostępna) |
| `bitget` | spot USDC | brak MiCA | **nie** | 0,10 / 0,10 % | [D2] | 20,0 | 2,42 (8/10) | 22,4 | −10,4 | ZAMKNIĘTA (i niedostępna) |
| `kraken_perp` | perp USD (multi-collateral) | Payward Europe Digital Solutions (CY), CySEC 342/17 (MiFID II) | tak (EEA; kwestionariusz + NIP) | **0,02 / 0,05 %** (Level 1) | [D] kraken.com/features/fee-schedule 11.10 | **4,0** | **1,99** | **6,0** | **+6,0** | **NIEROZSTRZYGNIĘTA** |
| `okx_xperps` | „perp" 5-letni, USD/EUR/krypto | OKX Malta (MiFID II) | tak (30 EEA, detal od 28.07.2026) | 0,02 / 0,05 % | [D2] | 4,0 | — [N] | 4,0 | — | NIEROZSTRZYGNIĘTA |
| `coinbase_futures_eu` | „perp" 5-letni | Coinbase Financial Services Europe, CySEC 374/19 (MiFID II) | [N] (26 krajów, lista bez PL) | „od 0,02 % za kontrakt" | [D2] bez rozbicia maker/taker | — | — | 0 (nic nie wiadomo) | — | NIEROZSTRZYGNIĘTA |
| `one_trading_perp` | perp EUR (OTF) | One Trading, AFM NL (MiFID II) | **nie** (detal DE/NL/AT) | 0,10 / 0,20 % (< 10 k EUR) | [D2] | 20,0 | — | 20,0 | — | ZAMKNIĘTA (2 pary: BTC, ETH) |
| `gemini_perp` | perp USDC | Gemini Artemis, MFSA MT (MiFID II) | **nie** (konta EEA zamknięte 6.04.2026) | 0,02 / 0,07 % | [D] | 4,0 | — | 4,0 | — | poza zakresem |
| `deribit` | perp USDC | Deribit FZE Dubaj / DRB Panama | **nie** (poza UE; perp = CFD) | 0,015 / 0,035 % | [D2] | 3,0 | 4,67 | 7,7 | +4,3 | poza zakresem |
| `alpaca_stock` | akcje USA | Alpaca Securities LLC (FINRA/SIPC) | indywidualne tak; firmowe tylko USA | **0 / 0** | [D] + SEC/FINRA | 0,0 | — [N] (IEX) | 0 + SEC 0,21 + TAF | — | NIEROZSTRZYGNIĘTA |

Wnioski z tabeli, zanim ktokolwiek zmierzy niewypełnienie:

1. **Każdy spot dostępny z UE jest ZAMKNIĘTY na samej prowizji** — najtańszy
   (crypto.com 0,075 %) daje 15 pb, czyli ponad próg, zanim doliczy się spread.
   OKX EU 0,08 % to 16 pb, i to tylko z otwartym kontem MiFID. Rabaty tokenem
   giełdy (KCS −20 %, OKB, CRO) zbijają stawkę najwyżej do 0,06–0,075 %, czyli
   12–15 pb samej prowizji, zapas zero — a wymagają trzymania tokenu giełdy
   (ryzyko kontrahenta × 2). **Tor crypto spot nie przechodzi Bramy 0 na żadnej
   giełdzie dostępnej z UE i nie ma cennika, który by to zmienił.**
2. **Jedyny cennik z zapasem to maker 0,02 % na perpetualach pod MiFID**
   (Kraken, OKX X-Perps; Coinbase bez rozbicia): 4 pb prowizji + ~2 pb spreadu
   = 6 pb, **zostaje 6 pb na niewypełnienie**.
3. **Spread nie jest problemem** nigdzie poza egzotyką: mediany 0,7–3 pb
   (bybit.eu 2,46 pb w snapshotcie — mniej niż 7,03 pb z karty 30 dni, bo
   niedziela wieczór i mediana zamiast średniej). NEAR i ARB są szerokie
   wszędzie (5–22 pb) — na perpetualach Krakena NEAR 6,8 pb.

### Spready per aktywo (pełny spread, pb, mediana z 12 próbek, 2026-10-11 17:48 UTC)

| księga | BTC | ETH | SOL | DOGE | XRP | SUI | ADA | NEAR | ARB | LINK | mediana | średnia |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bybit.eu spot USDC (**nasza**) | 2,63 | 0,08 | 3,56 | 2,29 | 1,41 | 3,97 | 11,79 | 22,50 | 1,51 | 0,74 | 2,46 | 5,05 |
| bybit.com spot USDC (świece modelu) | 0,01 | 0,04 | 0,89 | 5,73 | 0,70 | 4,85 | 3,93 | 19,04 | 10,80 | 8,18 | 4,39 | 5,42 |
| kraken spot EUR | 0,01 | 0,44 | 1,00 | 1,14 | 0,08 | 1,98 | 1,80 | 10,67 | 5,62 | 1,11 | 1,13 | 2,39 |
| kraken spot USD | 0,01 | 0,04 | 0,89 | 0,88 | 0,07 | 0,88 | 2,73 | 6,58 | 5,02 | 3,20 | 0,89 | 2,03 |
| **kraken perp USD (PF_*)** | 0,12 | 0,39 | 1,78 | 1,15 | 0,70 | 2,20 | 2,95 | 6,76 | 3,01 | 2,98 | **1,99** | 2,20 |
| okx spot USDC | 0,01 | 1,30 | 2,67 | 3,44 | 2,12 | 3,53 | 7,87 | 5,19 | 5,02 | 2,97 | 3,21 | 3,41 |
| okx spot EUR | 0,31 | 1,30 | 2,99 | 8,98 | 3,16 | 7,90 | 8,81 | 13,56 | 32,34 | 7,91 | 7,91 | 8,73 |
| okx spot USDT | 0,01 | 0,04 | 0,89 | 1,15 | 0,70 | 0,88 | 3,93 | 1,73 | 1,50 | 1,49 | 1,02 | 1,23 |
| binance spot USDC | 0,00 | 0,04 | 0,89 | 1,15 | 0,70 | 0,88 | 3,93 | 3,46 | 5,02 | 0,74 | 0,89 | 1,68 |
| bitvavo spot EUR | 0,13 | 0,04 | 0,10 | 1,67 | 0,08 | 2,37 | 1,98 | 6,79 | 9,56 | 1,29 | 1,48 | 2,40 |
| coinbase spot EUR | 1,57 | 1,58 | 1,99 | 2,57 | 3,16 | — | 6,61 | — | — | 6,34 | 2,57 | 3,40 |
| coinbase spot USD | 0,00 | 0,35 | 0,89 | 2,29 | 1,41 | 1,76 | 1,18 | 4,86 | 7,03 | 1,86 | 1,59 | 2,16 |
| kucoin spot USDC | 0,01 | 0,04 | 0,89 | 5,73 | 1,48 | 4,85 | 5,90 | 12,11 | — | 9,86 | 4,85 | 4,54 |
| deribit perp USDC | 0,01 | 0,04 | 0,09 | 4,58 | 0,07 | 5,29 | 7,08 | 4,76 | 8,54 | 8,18 | 4,67 | 3,86 |
| bitget spot USDC | 0,00 | 0,04 | 0,89 | 3,44 | 1,41 | 8,82 | 11,82 | — | — | 4,46 | 2,42 | 3,86 |
| crypto.com spot USD | 0,00 | 0,04 | 0,89 | 0,34 | 0,70 | 0,53 | 3,93 | 3,89 | 4,97 | 0,74 | 0,72 | 1,61 |

Skrypt: `scripts/spready_kandydatek.py` (publiczne endpointy `Depth`/`books`/
`orderbook`/`tickers`, bez kluczy). Jeden odczyt to **nie** jest pomiar z bramy:
spread w bramie ma być z 30 dni własnych `book_snapshot`.

---

## 4. Niewypełnienie — składnik, który rozstrzyga, i którego cennik nie zmienia

Model wypełnień decyduje o znaku wyniku całej strategii (audyt 10.10, H-1/H-5),
a w bramie jest jedynym składnikiem, którego nie widać z zewnątrz.

| źródło | wartość | co mierzy |
|---|---|---|
| symulacja pesymistyczna, przebieg 11.10, tor 4h (log: „niewypelnienie: 0.400% (pomiar)") | **40 pb rundy** = (1 − 0,787) × średni ruch, który przepadł ≈ 1,9 % | świece 1h z księgi **globalnej**, model „dotknięcie = wypełnienie", wejścia + wyjścia razem |
| demo bybit.eu, czujka selekcji negatywnej 03–05.10 | **−152 pb** w 48 h na 12 niewypełnionych (wypełnione −412 pb) | kierunek rynku, n = 12; ujemny → `max(0, x)` = 0 w bramie |
| wypełnialność wejść demo (E-1) | 65,6 % (42/64), Wilson 53–76 % | reguła R4, nie ensemble |
| giełdy kandydatki | **brak** | nie da się zmierzyć bez własnych zleceń |

Konsekwencja: jeśli 40 pb z symulacji jest choć w połowie prawdziwe dla innej
księgi, **żadna giełda nie przejdzie** — 20 pb samego niewypełnienia to więcej
niż cały próg (test `test_przy_niewypelnieniu_z_symulacji_zadna_krypto_nie_przechodzi`).
Dlaczego ta liczba jest wysoka i dlaczego to nie jest artefakt: sygnały są
momentum/przekrojowe, więc zlecenie z limitem na bidzie **nie wchodzi
dokładnie wtedy, gdy rynek rusza w naszą stronę** — niewypełnienie jest
selekcją negatywną, nie szumem. Taker to nie jest wyjście: 2 × 0,05 % = 10 pb
+ spread 2 pb + poślizg = próg wyczerpany bez niewypełnienia, przy zerowym
zapasie i niezmierzonym poślizgu (n = 0, docs/08).

Rozstrzyga więc **pomiar niewypełnienia na giełdzie docelowej**, i tylko on.

---

## 5. Rynki inne niż spot

### 5.1 Perpetuale u brokerów MiFID w UE

| | Kraken (CY) | OKX X-Perps (MT) | Coinbase (CY) |
|---|---|---|---|
| prowizja maker/taker | 0,02 / 0,05 % [D] | 0,02 / 0,05 % [D2] | „od 0,02 %" [D2] |
| uniwersum 10 aktywów | 10/10 (PF_*USD) | [N] | BTC, ETH, SOL [D2] |
| dźwignia | do 10×; **my: 1× lub mniej** (notional ≤ depozyt) | do 10× | do 10× |
| finansowanie | co 8 h? [N] (Kraken: „funding rate" per kontrakt; stawki `funding_rate` mamy tylko z Bybit) | [N] | co godzinę [D2] |
| demo/API | **demo-futures.kraken.com** (osobne konto, identyczne API) | **x-simulated-trading: 1** (konto demo) | [N] |
| dostęp PL | EEA pasportowane; indywidualny klient: pełna weryfikacja + kwestionariusz wiedzy + NIP [D] | 30 krajów EEA [D2] | 26 krajów, PL [N] |
| ryzyko regulacyjne | perp = CFD (ESMA 02.2026): dźwignia 2:1, close-out 50 %, NBP — dla long-only 1× bez znaczenia | 5-letni termin omija CFD — może zostać zakwestionowany | j.w. |

**Finansowanie nie jest we wzorze bramy, a jest kosztem.** Długa pozycja płaci,
gdy stawka jest dodatnia (zwykle). Przy medianie trzymania 5 świec 4h (log
11.10) to 2–3 wypłaty; przy stawce bazowej 0,01 %/8 h to **2–3 pb na rundę [A]**,
w hossie altów 0,03–0,10 %/8 h → 7–25 pb [A]. Symulacja nalicza je od 24.09
z cechy `funding_rate` po stronie i chwilach wypłaty (`koszty.GODZINY_FINANSOWANIA`),
więc walk-forward `--kind perp` ten koszt zobaczy — pod warunkiem, że stawki
będą z giełdy docelowej, nie z Bybit global.

**Czy model 4h pasuje do perpetuali.** Cechy liczone są ze świec spot Bybit
global (~1000× wolumen bybit.eu); perp Krakena śledzi indeks z bazą rzędu
pojedynczych pb, więc *ceny* pasują lepiej niż bybit.eu (rozjazd mediana 5,9 pb,
p90 28 pb, docs/08). Nie pasuje: (a) wariant perp mierzono wyłącznie **ze
shortami** (27.09: −8,43 pp zwrotu wobec spotu) — long-only perp nikt nie
symulował; (b) kolejka i głębokość księgi Krakena są inne, więc niewypełnienie
trzeba zmierzyć od zera; (c) brak adaptera giełdy w kodzie — nowa integracja
(poza tym PR). Z-1 na to pozwala: to hipoteza **pomiaru kosztu**, nie strategii.

### 5.2 Akcje USA (Alpaca)

| składnik | wartość | status |
|---|---|---|
| prowizja | 0 / 0 | [D] `CENNIK["stock"]` |
| SEC Section 31 | 20,60 USD / mln = **0,206 pb**, tylko noga sprzedaży, od 4.04.2026 | [D] sec.gov/rules-regulations/fee-rate-advisories/2026-2 |
| FINRA TAF | 0,000195 USD / akcję, max 9,79 USD/transakcję (od 1.01.2026) → 0,02 pb przy 100 USD, 0,2 pb przy 10 USD | [D] finra.org Information Notice 2026-03-17 (źródło wtórne dla stawki TAF) |
| spread | **nie zmierzono** — feed IEX daje artefakt (mediana 41 pb, JNJ 479 pb; docs/08) | [N] |
| niewypełnienie | nie zmierzono (silnik equity: limit, `--egzekucja gielda`) | [N] |
| konto dla JDG | indywidualne non-US: tak, bez minimum, wpłaty tylko USD przelewem; **firmowe: beta tylko podmioty USA, min. 30 000 USD** → handel jako osoba fizyczna, skutek podatkowy [N] | [D] alpaca.markets/support/non-us-business |

Brama 0 dla toru equity jest **bliżej niż dla krypto o rząd wielkości**: składnik
prowizyjny to ~0,3 pb, a spready NBBO dużych spółek USA to typowo 1–3 pb [A].
Ale to nie jest wąskie gardło equity: przebieg 11.10 daje **koszt progowy
−41,5 pb przy prowizji zero** (log: „KOSZT PROGOWY (z brutto): -41.53 pb — UJEMNY",
`degraded` 5/5, 17 % okien dodatnich). Strategia equity przegrywa przed kosztami;
tanie wykonanie nic tu nie zmienia. Brama 0 equity zostaje NIEROZSTRZYGNIĘTA
(spread, opłaty regulacyjne z `exec.fill`, niewypełnienie), a jej otwarcie nie
odblokowałoby niczego, dopóki tor nie ma przewagi.

---

## 6. Werdykt

1. **Crypto spot: wynik negatywny, ostateczny na poziomie cennika.** Żadna
   giełda spot dostępna rezydentowi PL (MiCA, paszport) nie schodzi poniżej
   15 pb samej prowizji; bybit.eu to 50 pb. Brama 0 jest ZAMKNIĘTA dla każdej
   z nich niezależnie od spreadu i niewypełnienia. To zamyka tor
   `ensemble 4h / spot` jako **wynik negatywny projektu** — zgodnie z README
   („wynik negatywny jest realnym rezultatem") i z H-9, która 18.10 najpewniej
   da STOP na tym samym cenniku.
2. **Jedyna droga z zapasem: perpetuale long-only pod MiFID** (Kraken
   Payward Europe Digital Solutions; OKX X-Perps jako rezerwa). 4 pb prowizji
   + 2 pb spreadu, 6 pb zapasu na niewypełnienie. **Nie przechodzi** bramy
   „z zapasem" — jest NIEROZSTRZYGNIĘTA, bo o wyniku decyduje składnik, którego
   z zewnątrz zmierzyć się nie da, a jedyna liczba, jaką mamy (40 pb z symulacji
   na obcych świecach), zamyka ją pięciokrotnie. Ryzyka tej drogi: kontrahent
   MiFID na Cyprze (CySEC) zamiast CASP; finansowanie poza wzorem (2–3 pb
   bazowo, do 25 pb w hossie); reklasyfikacja perp jako CFD (bez skutku dla 1×,
   ze skutkiem dla dostępności produktu); nowa integracja i **nowy zegar demo**
   (D-2: 16 tygodni, ≥ 150 wejść) i walk-forward od zera (H-9 § STOP).
3. **Equity:** Brama 0 numerycznie prawie na pewno otwarta (~0,3 pb prowizji
   i opłat), ale tor nie ma przewagi przed kosztami (−41,5 pb). Tu brama nie
   jest pytaniem.

**Rekomendacja.** Nie otwierać nowej integracji „bo cennik pozwala". Zamknąć
tor spot jako wynik negatywny (po H-9 18.10, formalnie). Dla perpetuali
zapisać jedną hipotezę pomiaru **B-0** (docs/hipotezy.md) z decyzją
właściciela do **2026-10-25** i pomiarem do **2026-11-29**: albo demo na
Kraken Futures (bez pieniędzy, osobne konto demo, adapter tylko do pomiaru
niewypełnienia i spreadu — bez modelu, bez decyzji), albo koniec toru crypto.
Kryterium GO/STOP w rejestrze, przed pomiarem.

---

## 7. Źródła (URL, data odczytu 2026-10-11)

- Bybit EU stawka: `/v5/account/fee-rate` api-demo.bybit.eu (2026-10-05, 135 par,
  `mozliwosci.py --stawki`); cryptoticker.io/en/comparison/bybit-vs-okx (flat 0,25 %
  od 5.10.2026; brak derywatów dla detalu EEA).
- Kraken: kraken.com/features/fee-schedule (tabela poziomów Level 1…Pro 5);
  support.kraken.com/.../cross-platform-fee-tier-changes (od 9.07.2026);
  kraken.com/pro/futures-eea-fee-waiver i kraken.com/legal/eea-futures-fee-waiver-promotion-terms
  (0/0 do 10 mln USD, nowi klienci, koniec 19.07.2026);
  support.kraken.com/gb/articles/derivatives-eligibility-requirements-eea;
  businesswire.com 2025-05-20 (start perp w EEA, CySEC 342/17);
  docs.kraken.com/api/docs/guides/futures-introduction (demo-futures.kraken.com).
- OKX: okx.com/en-eu/help/trading-fee-rules-faq-eea (bez liczb; odsyła na
  okx.com/en-eu/fees — nie odczytane przez robota); cryptoast.fr/frais-okx
  i airdropalert.com (0,08/0,10 % z kontem X-Perps, 0,20/0,35 % bez);
  cointelegraph.com „okx-europex-perps-regulated-crypto-derivatives";
  fintechfutures.com (X-Perps, 10×, 30 krajów EEA); thefintechtimes.com (Simple
  Mode 28.07.2026); theblock.co/post/306067 (MiCA Malta).
- Bitvavo: bitvavo.com/en/fees (403); capitalo.at, freenance.io (0,15/0,25 %,
  MiCA AFM 06.2025).
- Coinbase: brokerchooser.com, conductatlas.com (Intro 1: 0,60/1,20 %; Coinbase
  nie publikuje pełnej tabeli); theblock.co/post/392797, decrypt.co/360420,
  invezz.com 2026-03-09 (futures EU, CySEC 374/19, 26 krajów, „od 0,02 %").
- Bitstamp: cryptoadventure.com „Bitstamp Review 2026" (12 poziomów; 0/0 do 1 000 USD).
- KuCoin EU: trendingtopics.eu (MiCA FMA; start niepotwierdzony), datawallet.com (0,1/0,1 %, KCS −20 %).
- Crypto.com: capitalo.at, dailycoin.com (0,075/0,075 %; MiCA MFSA 27.01.2025).
- Binance: parkiet.com/kryptowaluty/art44692131, unchainedcrypto.com (brak MiCA, 1.07.2026).
- Bitget: tangem.com/en/learning-hub/post/is-bitget-mica-authorised.
- Gemini: gemini.com/fees/derivative-fees-table (0,02/0,07 %); datawallet.com
  (zamknięcie kont EEA 6.04.2026); coindesk.com 2025-05-09 (MiFID MFSA).
- One Trading: support.onetrading.com art. 21953902771473 (403; tabela przez
  wyszukiwarkę); thetradenews.com (detal DE/NL/AT; BTC/EUR, ETH/EUR).
- Deribit: support.deribit.com/hc/en-us/articles/25944487427741 (Restricted
  Jurisdictions); datawallet.com (DRB Panama dla detalu); blockeden.xyz 2026-04-17
  (1,5/3,5 pb od 08.2026).
- ESMA: esma.europa.eu/node/228326 (ESMA35-243228190-8024, 24.02.2026);
  finreg.aoshearman.com; regulationtomorrow.com 2026-02.
- Polska/MiCA: comparic.pl (weto 12.02.2026; stanowisko KNF, 1.07.2026);
  bankier.pl „Polska w ogonie Europy"; pap.pl.
- Alpaca: alpaca.markets/support/non-us-business; forum.alpaca.markets
  „getting-started-alpaca-securities-llc" (konta indywidualne non-US, bez minimum).
- SEC: sec.gov/rules-regulations/fee-rate-advisories/2026-2 (20,60 USD/mln od 4.04.2026).
- FINRA TAF: finra.org/rules-guidance/notices/information-notice-20260317
  (Section 31); brokerage-review.com (TAF 0,000195 USD/akcję, max 9,79 USD, od 1.01.2026).
- Spready: publiczne endpointy api.bybit.eu, api.bybit.com, api.kraken.com,
  futures.kraken.com, okx.com, api.binance.com, api.bitvavo.com,
  api.exchange.coinbase.com, api.kucoin.com, deribit.com, api.bitget.com,
  api.crypto.com — 2026-10-11 17:48 UTC, 12 próbek co 2 s.
- Log trenera: Job `trader-trainer-29861440` (2026-10-11), odczyt `kubectl logs`.
