# Trader-AI — research przedwdrożeniowy

Data: 2026-08-27. Dokument źródłowy dla decyzji architektonicznych. Wszystko poniżej
zweryfikowane wyszukiwaniem w sierpniu 2026 — linki na końcu.

## 1. Zakres

Autonomiczny system tradingowy na VPS, dostępny przez własną stronę (login + 2FA).
**Dwa niezależne silniki**, docelowo dwie zakładki w dashboardzie:

| Silnik | Rynek | Charakterystyka |
|---|---|---|
| `crypto` | giełdy kryptowalut | 24/7, brak sesji, wysoka zmienność, natychmiastowy settlement |
| `equity` | giełda tradycyjna (akcje/ETF) | sesje giełdowe, luki nocne, halty, dywidendy, splity, T+1 |

To są na tyle różne domeny (kalendarz, mikrostruktura, ryzyko, koszty), że wspólny silnik
byłby kompromisem w obie strony. Wspólne pozostają: warstwa danych, feed newsów, warstwa
uczenia (te same algorytmy, osobne modele i osobne stany), UI, auth, audyt.

Faza 1 (~6 miesięcy): **wyłącznie demo/paper**. Faza 2: live, po spełnieniu kryteriów
przejścia (sekcja 9).

## 2. Wybór giełd i brokerów

### 2.1 Crypto — rekomendacja: Bybit Demo Trading

Trzy realne opcje:

| | Bybit Demo | Binance Testnet | OKX Demo |
|---|---|---|---|
| Ceny | **zsynchronizowane z live** | izolowany orderbook (własne ceny) | zsynchronizowane z live |
| Dostęp API dla botów | tak | **testnet tak, "Mock Trading" w apce tylko ręcznie** | tak |
| Rate limit | 120 req / 5 s | weight-based, 1200 wagi/min | 60 req / 2 s (najostrzej) |
| WebSocket | < 50 ms, unified API | osobne API per produkt | unified |
| Wirtualny kapitał | do 1 000 000 USD | — | — |

**Bybit** wygrywa, bo demo chodzi po **realnych cenach z live** — to jedyny sposób, żeby
sześciomiesięczna faza demo cokolwiek znaczyła. Binance testnet ma własny, płytki orderbook:
świetny do testu *kodu*, bezużyteczny do testu *strategii*.

Szczegóły techniczne Bybit demo (v5):
- REST: `https://api-demo.bybit.com`, WS prywatny: `wss://stream-demo.bybit.com`
- WS publiczny (ceny) — normalny `wss://stream.bybit.com`, demo go nie duplikuje
- **klucz API trzeba wygenerować na koncie produkcyjnym w trybie Demo Trading**, nie na testnecie
- w klientach (np. `pybit`) flaga `testnet=False` + osobny host demo — częsty błąd konfiguracji

### 2.2 Warstwa abstrakcji: CCXT

CCXT (~103 giełdy, unified API, Python/TS/Go) daje `set_sandbox_mode(True)` i jednolity
interfejs. Używamy go jako **domyślnego adaptera**, ale z własnym interfejsem `ExchangeAdapter`
nad nim — CCXT wygładza różnice w REST, ale nie w prywatnych WebSocketach ani w niuansach
marginu. Dla Bybit dodatkowo natywny klient do streamu prywatnego.

Zysk: zmiana giełdy = nowa implementacja adaptera, nie przepisywanie silnika.

### 2.3 Giełda tradycyjna — rekomendacja: Alpaca (paper), alternatywa IBKR

| | Alpaca | Interactive Brokers | XTB |
|---|---|---|---|
| Paper trading API | **tak, konto domyślnie paper, $100k** | tak (paper account) | demo tak |
| Rynki | US equities, ETF, opcje, crypto | globalne, w tym GPW | CFD (nie akcje kasowe) |
| Jakość API | REST + WS, najlepszy DX | najbogatsze, ale ciężkie (TWS Gateway / Client Portal) | — |
| Onboarding | klucze w kilka minut, bez depozytu | wymaga wniosku i utrzymania sesji TWS | — |
| Blocker | brak GPW / rynków PL | operacyjnie cięższe (gateway musi żyć w kontenerze) | **publiczne API `xapi.xtb.com` / `ws.xtb.com` wyłączone od 2025-03-14** |

**XTB odpada** — publiczny dostęp do xStation5 API został wyłączony w marcu 2025, więc nie
jest to ścieżka do budowania nowej integracji.

Rekomendacja: **Alpaca na start** (najniższe tarcie: paper od pierwszego dnia, żywe ceny,
czysty REST/WS), z adapterem napisanym tak, żeby **IBKR dało się dołożyć później** jeśli
zależy Ci na GPW/Europie. Ta decyzja wymaga Twojego potwierdzenia — patrz pytania na końcu.

## 3. Architektura

```
                         ┌──────────────────────────────┐
      przeglądarka ──────│  Web (Next.js/React)         │
        login+2FA        │  tab: CRYPTO | tab: EQUITY   │
                         └───────────┬──────────────────┘
                                     │ HTTPS (Traefik, renacode.com)
                         ┌───────────▼──────────────────┐
                         │  API (FastAPI)               │
                         │  auth+TOTP, RBAC, audit log  │
                         └───────────┬──────────────────┘
                                     │
        ┌────────────────────────────┼────────────────────────────┐
        │                            │                            │
┌───────▼────────┐          ┌────────▼────────┐          ┌────────▼────────┐
│ engine-crypto  │          │  shared core    │          │  engine-equity  │
│  (osobny       │          │ ─────────────── │          │  (osobny        │
│   proces)      │          │ market data     │          │   proces)       │
│                │          │ news/RSS        │          │                 │
│ Bybit adapter  │          │ feature store   │          │ Alpaca adapter  │
│ 24/7 loop      │          │ learning engine │          │ session-aware   │
│ risk: crypto   │          │ risk kernel     │          │ risk: equity    │
└───────┬────────┘          │ audit/journal   │          └────────┬────────┘
        │                   └────────┬────────┘                   │
        └────────────────────────────┼────────────────────────────┘
                                     │
                    ┌────────────────▼─────────────────┐
                    │ PostgreSQL + TimescaleDB  │ Redis │
                    │ (świece, sygnały,         │ (kolejki,
                    │  decyzje, transakcje,     │  cache,
                    │  metryki modelu, audyt)   │  locki)
                    └───────────────────────────┴───────┘
                                     │
                    ┌────────────────▼─────────────────┐
                    │ Anthropic API (claude-opus-5)    │
                    │ analiza newsów, uzasadnienia,    │
                    │ post-mortem transakcji           │
                    └──────────────────────────────────┘
```

Kluczowe zasady:

1. **Silniki to osobne procesy/kontenery.** Padnięcie crypto nie zatrzymuje equity.
2. **Risk kernel jest wspólny i twardy** — limity egzekwowane w kodzie, nie w promptcie.
   Model może chcieć wejść w pozycję; kernel decyduje, czy wolno.
3. **Wszystko jest zapisywane.** Każda decyzja: wejście, sygnały, features, wagi, źródła
   newsów, uzasadnienie, kontrfaktyczny wynik. To jest jednocześnie dashboard i dataset.
4. **Anthropic (`claude-opus-5`) nie podejmuje decyzji tradingowych bezpośrednio.**
   Robi to, w czym LLM jest naprawdę dobry: czyta newsy, ekstrahuje ustrukturyzowane sygnały
   (structured outputs, JSON schema), pisze uzasadnienia i post-mortemy. Sygnał z newsa wchodzi
   jako *jedna z cech* do modelu, z własną wagą, którą system uczy się kalibrować.

## 4. Warstwa "uczenia się" — realistycznie

Tu muszę być bezpośredni, bo to jest miejsce, gdzie ten projekt może się cicho wyłożyć.

Research jest jednoznaczny co do tego, co **nie** działa:

- **Backtest overfitting.** Wysokie wyniki w backteście są osiągalne po przetestowaniu
  stosunkowo niewielu konfiguracji strategii — czyli świetny backtest to domyślnie sygnał
  przeuczenia, nie alfy.
- **Deep RL przeucza się gorzej niż uczenie nadzorowane.** Agent RL zapamiętuje nie wzorce,
  a konkretne sekwencje zwrotów w okresie treningowym; degradacja out-of-sample jest szybsza
  niż dla modeli nadzorowanych, bo przeucza się do *dynamiki*, nie do przekroju.
- **Zwykła k-fold cross-walidacja jest nieważna** — nie wolno tasować szeregu czasowego.

Co robimy zamiast tego:

### 4.1 Walk-forward jako jedyny protokół oceny
Rolling window: trenuj na oknie N, waliduj na kolejnym oknie M, przesuń, powtórz.
Strategia musi udowadniać się wielokrotnie, w różnych reżimach rynkowych. Żadna metryka
z pojedynczego backtestu nie trafia do dashboardu jako "wynik".

### 4.2 Interpretowalny ensemble zamiast czarnej skrzynki
Zbiór hipotez-sygnałów, każdy z osobna prosty i wytłumaczalny (momentum, mean reversion,
zmienność, przepływ orderbooka, sentyment newsowy, sezonowość sesyjna dla equity).
Uczenie = **kalibracja wag i progów** tych sygnałów per reżim rynkowy, nie odkrywanie
sygnałów od zera. Dzięki temu dashboard może uczciwie pokazać "dlaczego" — bo naprawdę wiadomo.

### 4.3 Detekcja reżimu
Osobny, prosty klasyfikator reżimu (trend / zakres / wysoka zmienność / szok newsowy).
Wagi sygnałów są per-reżim. To bezpośrednio adresuje "małe przesunięcia dynamiki rynku
niszczą wydajność".

### 4.4 Metryki, które trafiają na dashboard
- **Out-of-sample** Sharpe / Sortino / max drawdown, per okno walk-forward
- Hit rate i średni R na transakcję, per sygnał i per reżim
- **Deflated Sharpe / krotność testowanych konfiguracji** — jawny licznik, ile hipotez
  przetestowaliśmy, żeby nie oszukiwać się selekcją
- Kalibracja: czy sygnał o pewności 70% trafia w ~70% przypadków (reliability diagram)
- Dryf: rozjazd rozkładu cech treningowych vs bieżących

To jest ten "stopień, jak się uczy algorytm", o który prosiłeś — ale wyrażony w liczbach,
które nie kłamią, a nie w pasku postępu.

### 4.5 Czego ten system nie obiecuje
Nie ma podstaw, żeby zakładać rentowność. Sześć miesięcy demo to jest właśnie test tej
hipotezy, i wynik "strategia nie ma alfy" jest realnym, prawdopodobnym rezultatem —
i powinien zablokować przejście na live. Kryteria przejścia w sekcji 9.

## 5. Newsy i analiza przez Claude

### 5.1 Źródła (RSS, darmowe, bez rate-limitów komercyjnych)
Crypto: CoinDesk, Cointelegraph, Decrypt, TheBlock, Bitcoin Magazine, NewsBTC, CryptoPotato.
Makro/equity: Reuters Business, Bloomberg (RSS ograniczony), MarketWatch, SEC EDGAR filings,
Fed/ECB/NBP komunikaty, kalendarz makro.
Dodatkowo: Fear & Greed Index, dane on-chain (opcjonalnie, faza 2).

Dashboard pokazuje **dokładnie z których źródeł i którego artykułu** wziął się sygnał —
to był Twój explicit wymóg ("gdzie i z jakich stron korzysta").

### 5.2 Pipeline
```
RSS poll (5 min) → dedup (hash tytułu+URL) → filtr istotności (tanie: Haiku 4.5)
   → analiza (claude-opus-5, structured outputs) → sygnał do feature store
```

Dwustopniowość jest celowa: 95% artykułów to szum. Tani model filtruje, drogi analizuje
tylko to, co przeszło. Prompt caching na stabilnym prefiksie (instrukcje + kontekst rynku)
obcina koszt ~10x przy powtarzalnych wywołaniach.

Output analizy — wymuszony schemat JSON:
```json
{
  "instruments": ["BTC", "ETH"],
  "direction": "bullish|bearish|neutral",
  "magnitude": 0.0-1.0,
  "horizon": "minutes|hours|days|weeks",
  "confidence": 0.0-1.0,
  "novelty": 0.0-1.0,
  "reasoning": "...",
  "source_url": "..."
}
```
`novelty` jest krytyczna — news, który rynek już wycenił, ma wartość zero.

### 5.3 Post-mortem
Raz dziennie Claude dostaje zamknięte transakcje z pełnym kontekstem decyzji i pisze analizę:
co zadziałało, co nie, jakie wzorce błędów. To trafia do dashboardu jako czytelny dziennik
i do warstwy uczenia jako sygnał jakościowy — **nie** jako automatyczna zmiana wag
(to byłaby pętla sprzężenia zwrotnego na anegdotach).

## 6. Uwierzytelnianie

- Hasło: Argon2id
- 2FA: **TOTP (RFC 6238)** przez `pyotp`, sekret szyfrowany at-rest, `valid_window=1`
- Kody zapasowe (jednorazowe, hashowane)
- Sesje: HttpOnly + Secure + SameSite=Strict cookies, krótki TTL, rotacja
- Rate limiting na login i weryfikację TOTP, lockout po N próbach
- **Rozważ WebAuthn/passkey** — dla nowej aplikacji to mocniejszy standard niż TOTP i można
  go dołożyć obok. Rekomendacja: TOTP w fazie 1, WebAuthn dodane przed przejściem na live.

Osobno i ważne: **klucze API do giełd**. Podasz je przez UI (zgodnie z Twoją prośbą), więc:
szyfrowane symetrycznie kluczem z env/secretu (nigdy w bazie plaintext), nigdy nie zwracane
przez API (tylko maska `••••1234`), osobne uprawnienia — klucz do demo nie ma prawa wypłaty,
klucz live docelowo **bez uprawnień withdrawal** i z whitelistą IP VPS-a.

## 7. Dashboard — co konkretnie

Dwie zakładki (CRYPTO / EQUITY), w każdej te same sekcje:

1. **Stan** — equity curve, aktualne pozycje, P&L (dzienny/skumulowany), ekspozycja,
   wykorzystanie limitów ryzyka
2. **Stopień nauki** — metryki z 4.4: out-of-sample Sharpe per okno walk-forward, krzywa
   kalibracji, hit rate per sygnał, wykryty reżim, liczba przetestowanych konfiguracji,
   wskaźnik dryfu. Plus jawny status: `WARMUP` / `LEARNING` / `VALIDATED` / `DEGRADED`
3. **Źródła** — lista feedów RSS ze statusem (ostatni poll, liczba artykułów, ile przeszło
   filtr), oraz per-decyzja: które artykuły ją współtworzyły, z linkami
4. **Ruchy** — dziennik: wykonane transakcje (co, kiedy, po ile, dlaczego — z rozbiciem na
   wkład każdego sygnału) oraz **planowane** (oczekujące warunki wejścia, poziomy triggerów,
   co musi się stać, żeby zlecenie poszło)
5. **Ryzyko** — limity, ile z nich zużyte, historia naruszeń i zadziałań kill-switcha
6. **Ustawienia** — klucze API, tryb (demo/live), limity, włączanie/wyłączanie sygnałów

## 8. Deployment

Zgodnie z resztą VPS-a. Dwie opcje, obie sensowne:

- **k3s + ArgoCD + Helm chart w repo** — jak `dietetyk`, `ogrodnik`, `renacode-website`
  (`argocd-apps.yaml` w `renacode-infra`). GitOps, self-heal, spójne z resztą.
- **Docker Compose** — jak Home Assistant. Prostsze, ale poza pętlą GitOps.

Rekomendacja: **k3s + ArgoCD**, bo tak działa reszta aplikacji i dostajesz auto-sync
oraz self-heal za darmo. Ingress przez Traefik na subdomenie `renacode.com`.

Kontenery: `web`, `api`, `engine-crypto`, `engine-equity`, `worker-news`, `postgres+timescale`,
`redis`. Sekrety przez k8s Secret (docelowo sealed-secrets — w repo `renacode-infra` masz już
historię wycieku tokenów HA, więc plaintext w gicie odpada kategorycznie).

## 9. Ryzyko i kryteria przejścia demo → live

### 9.1 Twarde zabezpieczenia (w kodzie, od dnia pierwszego)
- Max strata dzienna → auto-stop do końca doby
- Max drawdown od szczytu → kill-switch, wymaga ręcznego odblokowania
- Max ekspozycja na instrument i łącznie
- Max liczba transakcji / godzinę (ochrona przed pętlą błędu)
- Sanity check zlecenia przed wysłaniem (cena w rozsądnym paśmie, wielkość w limicie)
- Dead-man switch: brak heartbeatu silnika → zamknięcie pozycji / wstrzymanie
- **Tryb live wymaga osobnego, jawnego potwierdzenia w UI + 2FA re-auth**

### 9.2 Kryteria wyjścia z demo (propozycja — do ustalenia)
Po 6 miesiącach przechodzimy na live **tylko jeśli**:
- dodatni wynik out-of-sample w ≥ 70% okien walk-forward
- max drawdown poniżej ustalonego progu
- brak nierozwiązanych incydentów operacyjnych (rozjazd stanu, zawieszone zlecenia)
- kalibracja pewności w rozsądnym zakresie (nie: model pewny w 90%, trafia w 45%)
- co najmniej jeden pełny cykl: szok rynkowy przetrwany bez naruszenia limitów

Jeśli kryteria nie są spełnione — przedłużamy demo albo kończymy. To jest wynik projektu,
nie porażka wdrożenia.

### 9.3 Kwestie formalne
Handel algorytmiczny na własnym rachunku jest legalny; obowiązki podatkowe (PIT-38 / ryczałt
od zysków kapitałowych) i ewentualne wymogi raportowe leżą po Twojej stronie — system będzie
prowadził pełny, eksportowalny dziennik transakcji, żeby to ułatwić.

## 10. Plan wdrożenia

| Faza | Zakres | Efekt |
|---|---|---|
| **0** | szkielet: repo, Docker, CI, Postgres+Timescale, Helm chart | deploy pustej apki na VPS |
| **1** | auth + 2FA, dashboard shell z dwiema zakładkami, zarządzanie kluczami API | **logujesz się i podajesz poświadczenia** |
| **2** | adapter Bybit demo + ingest danych rynkowych + paper execution | pierwsze transakcje demo widoczne w UI |
| **3** | worker RSS + analiza Claude + panel źródeł | dashboard pokazuje newsy i sygnały z nich |
| **4** | silnik sygnałów, risk kernel, dziennik decyzji (ruchy zrobione i planowane) | pełna pętla crypto |
| **5** | warstwa uczenia: walk-forward, detekcja reżimu, metryki nauki | zakładka "stopień nauki" na żywo |
| **6** | adapter Alpaca/IBKR + silnik equity | druga zakładka aktywna |
| **7** | 6 miesięcy obserwacji, tuning, WebAuthn, przygotowanie do live | decyzja go/no-go |

Faza 1 kończy się dokładnie tam, gdzie chciałeś: aplikacja dostępna, Ty podajesz
poświadczenia w UI.

---

## Źródła

- [Bybit Demo Trading Service — API v5](https://bybit-exchange.github.io/docs/v5/demo)
- [Bybit API — Introduction](https://bybit-exchange.github.io/docs/v5/intro)
- [Best Crypto Exchange for Bots 2026: Bybit vs Binance](https://trendrider.net/blog/best-crypto-exchange-for-bot-trading-2026)
- [Crypto Exchange API Comparison for Traders 2026](https://voiceofchain.com/academy/crypto-exchange-api-comparison)
- [CCXT — GitHub](https://github.com/ccxt/ccxt) · [CCXT Manual](https://docs.ccxt.com/docs/manual)
- [Alpaca — Paper Trading docs](https://docs.alpaca.markets/us/docs/paper-trading)
- [Alpaca — Algorithmic Trading API](https://alpaca.markets/algotrading)
- [Best Brokers for Algorithmic Trading in 2026](https://brokerchooser.com/best-brokers/best-brokers-for-algo-trading)
- [xapi-node — xStation5 API (informacja o wyłączeniu publicznego API)](https://github.com/peterszombati/xapi-node)
- [PyOTP — dokumentacja](https://pyauth.github.io/pyotp/) · [PyOTP GitHub](https://github.com/pyauth/pyotp)
- [Two-factor Authentication (2FA) in FastAPI and Python](https://codevoweb.com/two-factor-authentication-2fa-in-fastapi-and-python/)
- [Walk-Forward Validation, Risk Controls & Paper Trading](https://medium.com/@conniezhou678/machine-learning-for-algorithmic-trading-part-21-from-research-to-reality-walk-forward-31756db6c044)
- [A Rigorous Walk-Forward Validation Framework for Market Microstructure Signals (arXiv)](https://arxiv.org/html/2512.12924v1)
- [The GT-Score: Reducing Overfitting in Data-Driven Trading Strategies (arXiv)](https://arxiv.org/pdf/2602.00080)
- [Deep Reinforcement Learning for Cryptocurrency Trading (arXiv)](https://arxiv.org/pdf/2209.05559)
- [Backtest overfitting in the machine learning era (ScienceDirect)](https://www.sciencedirect.com/science/article/abs/pii/S0950705124011110)
- [Machine Learning for Trading — repo referencyjne](https://github.com/stefan-jansen/machine-learning-for-trading)
- [Best Cryptocurrency RSS Feeds (2026)](https://rss.feedspot.com/cryptocurrency_rss_feeds/)
