# Ochrona kapitału: czy rachunek może wyjść na minus

Pytanie właściciela, 2026-09-07: *wpłacam 10 000 USD i nie chcę, żeby rachunek
poszedł na minus.* Postawione jako go/no-go dla całego projektu.

To pytanie zawiera **dwa różne pytania** i mają one dwie różne odpowiedzi.

---

## 1. Czy rachunek może zejść poniżej zera, czyli czy mogę być winien pieniądze?

**Nie. Jest to niemożliwe strukturalnie, nie regulaminowo.** Zweryfikowane
w kodzie, nie zadeklarowane:

| warunek | gdzie | stan |
|---|---|---|
| brak dźwigni | podmiot unijny nie oferuje kontraktów wieczystych (`docs/00`, tabela bybit.eu) | 0 perpetuali |
| brak krótkich pozycji | `koszty.py`: `spot_eu` ma `krotka_pozycja=False` | wymuszone |
| krótka pozycja odrzucana w produkcji | `loop.py:779` — `enter_short` zamienia się w `hold` | wymuszone |
| krótka pozycja odrzucana w symulacji | `portfolio.py`: `wolno_short`, test `test_na_spocie_nie_otwieramy_krotkich_pozycji` | wymuszone |
| brak pożyczki marżowej | nigdzie w kodzie nie ma ścieżki marży | — |

Handel wyłącznie długi na spocie oznacza, że najgorsze, co może się stać, to
spadek wartości kupionych aktywów do zera. Traci się to, co zaangażowane —
nigdy więcej. **Dług jest niemożliwy.**

## 2. Czy rachunek może zejść poniżej wpłaty?

**Tak. I żaden system tego nie wyklucza.** Strategia, która nie może stracić,
nie może też zarobić — to ta sama zmienność. Każdy, kto obiecuje inaczej, albo
się myli, albo nie mówi o handlu.

Da się natomiast **ograniczyć stratę i zmierzyć, ile realnie wynosiła.**

---

## Zmierzona ścieżka rachunku 10 000 USD

Odtworzona ze zwrotów 39 okien walk-forward (3,2 roku, horyzont 4 h,
`spot_eu` ze zleceniami z limitem, konfiguracja produkcyjna):

| | wielkości pozycji stałe | skalowane kapitałem |
|---|---|---|
| stan końcowy | **$10 957** | $10 996 |
| najniższy punkt | **$9 997** | $9 997 |
| największe obsunięcie od szczytu | −2,19% | −2,44% |
| okien poniżej wpłaty | **1 z 39** | 1 z 39 |

Dodatkowo: najdłuższa seria stratnych okien to **3**, najgorsze pojedyncze okno
**−1,15%**, a najgorsze obsunięcie *wewnątrz* okna (czego powyższa ścieżka nie
widzi, bo składa się z końców okien) **−2,85%**, czyli około **−285 USD**.

Rachunek zszedł poniżej wpłaty raz, o trzy dolary.

---

## BLOKADA: te liczby NIE DOTYCZĄ rachunku 10 000 USD

To jest właściwa odpowiedź na go/no-go i powód, dla którego dziś brzmi ona
**NIE — do czasu jednej zmiany.**

Wszystkie powyższe pomiary powstały na rachunku **100 000 USD**
(`risk_rules.START_EQUITY`). Trzy z pięciu limitów ryzyka są zapisane
w bazie jako **kwoty**, nie procenty (`risk.limit_def`, `unit = 'quote_ccy'`),
i nie skalują się z rachunkiem:

| limit | USD | % ze 100 000 | **% z 10 000** |
|---|---|---|---|
| `max_position_notional` | 5 000 | 5,0% | **50,0%** |
| `max_total_exposure` | 15 000 | 15,0% | **150,0%** |
| budżet na pozycję (limit ÷ 10 instrumentów) | 1 500 | 1,5% | **15,0%** |

Konsekwencje dla rachunku 10 000 USD z niezmienioną konfiguracją:

1. **Limit łącznej ekspozycji przestaje cokolwiek ograniczać.** 15 000 to 150%
   rachunku, a na spocie bez dźwigni nie da się przekroczyć 100%. Limit, który
   trzyma całą konstrukcję, nigdy się nie aktywuje.
2. **Ryzyko rośnie dziesięciokrotnie.** Średnia ekspozycja 1,29% → **12,9%**.
   Najgorsze obsunięcie okna −2,85% → **−28,5%**.
3. **Kill-switch obsunięcia zadziała — po stracie 1 000 USD.** Stoi na 10%
   i zatrzyma handel, ale dopiero wtedy. Zamiast ochrony dostajemy system,
   który raz traci dziesiątą część wpłaty i przestaje działać.

Żadna z tych rzeczy nie objawiłaby się awarią. Wszystko działałoby dalej,
raportowało zielone statusy i handlowało dziesięciokrotnie większym ryzykiem,
niż to, na którym mierzono wynik.

---

## Co musi się zmienić, żeby było GO

1. **Limity kwotowe przeliczane na ułamek kapitału.** `max_position_notional`
   i `max_total_exposure` mają być wyrażone w procentach rachunku, a nie
   w dolarach. Wtedy zmiana wpłaty nie zmienia profilu ryzyka.
2. **Próg kapitału jako osobny limit** (`min_equity`). Obsunięcie liczy się od
   SZCZYTU, a szczyt rośnie razem z rachunkiem — to dobre po wzroście, ale na
   starcie, gdy szczyt równa się wpłacie, dopuszcza stratę 10% wpłaconych
   pieniędzy. Próg kapitału wyraża wprost „poniżej tej kwoty nie schodzimy",
   w tych samych jednostkach, w których się wpłaca.
3. **Ponowny pomiar po przeliczeniu.** Nie „powinno wyjść tak samo" — przebieg
   walk-forward na nowej konfiguracji, z porównaniem do liczb powyżej.

## Czego nie da się zagwarantować nigdy

- **Że rachunek nie zejdzie poniżej wpłaty.** Można wybrać, gdzie postawić
  podłogę; nie można jej postawić na zero straty.
- **Że próg zadziała dokładnie na progu.** Kill-switch sprawdza się raz na cykl
  decyzyjny (5 minut), więc między sprawdzeniami cena może zejść niżej. Przy
  ekspozycji ograniczonej do kilku procent kapitału luka jest mała, ale jest.
- **Że przyszłość będzie jak przeszłość.** −285 USD to najgorszy punkt
  **zmierzony na czterech latach historii**. Nie jest to gwarancja, tylko
  najlepszy dostępny szacunek — i taki, który sam projekt traktuje jako
  hipotezę do dalszego falsyfikowania.
