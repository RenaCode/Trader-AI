# Trader-AI — architektura warstwy danych i uczenia

Data: 2026-08-27. Dokument decyzyjny. Poprzedza go `00-research.md`.

## 1. Wybór silnika bazy: PostgreSQL 17 + TimescaleDB + pgvector

### Kandydaci

| | TimescaleDB (Postgres) | QuestDB | ClickHouse |
|---|---|---|---|
| Ingest | dobry | **~4M wierszy/s, do 4.8× szybszy od ClickHouse** | bardzo dobry, kompresja 10:1 |
| Zapytania analityczne | dobre | najlepsze na lastpoint (27× ClickHouse) i krótkie zakresy | najlepsze na miliardach wierszy |
| Model relacyjny, FK, transakcje | **pełny Postgres** | ograniczony | ograniczony, brak klasycznych FK |
| Wektory / embeddingi | **pgvector w tym samym silniku** | brak | brak natywnego |
| Ekosystem | **cały ekosystem Postgresa** | wąski | szeroki, ale osobny |

### Decyzja: TimescaleDB

Uzasadnienie oparte na **rzeczywistej skali tego projektu**, nie na benchmarkach:

- 3–5 instrumentów, świece 1m + snapshoty orderbooka co kilka sekund to rząd **10⁷ wierszy/rok**.
  QuestDB przy 4M wierszy/s rozwiązuje problem, którego tu nie ma. Płacilibyśmy złożonością
  za przewagę niemierzalną w tej skali.
- Ten system to w ~70% dane **relacyjne i transakcyjne**, nie szereg czasowy: decyzje, zlecenia,
  pozycje, wersje modeli, okna walk-forward, użytkownicy, klucze API, audyt. Postgres to robi
  natywnie z prawdziwymi kluczami obcymi i transakcjami. QuestDB i ClickHouse — nie.
- **Jedna baza zamiast trzech.** pgvector obsługuje embeddingi newsów (dedup + `novelty`)
  w tym samym silniku, z tymi samymi transakcjami. Research jest tu jednoznaczny: pgvector jest
  produkcyjnie gotowy do ~50–100 mln wektorów z latencją poniżej 10 ms na HNSW; przechodzi się
  na dedykowaną bazę wektorową dopiero gdy potrafisz wskazać konkretne wąskie gardło.
  My będziemy mieć rzędu 10⁵ artykułów rocznie.
- Jeden StatefulSet w k3s, jeden backup, jeden punkt spójności.

Próg rewizji tej decyzji: **> 100 mln wierszy w `market.candle` lub potrzeba skanów całego rynku
(50+ instrumentów, tick-by-tick)**. Wtedy dokładamy QuestDB/ClickHouse jako warstwę ingestu
obok Postgresa, nie zamiast niego.

Obraz: `timescale/timescaledb-ha:pg17` — zawiera TimescaleDB i pgvector w jednym.

## 2. Zasada nadrzędna: bitemporalność

**To jest najważniejsza decyzja w całym dokumencie.** Bez niej warstwa uczenia będzie
produkować fałszywe wyniki, i nie zorientujemy się przez wiele miesięcy.

Każda tabela faktów ma dwa znaczniki czasu:

| Kolumna | Znaczenie |
|---|---|
| `event_time` | kiedy zdarzenie zaszło w świecie/na rynku |
| `ingested_at` | kiedy **nasz system** się o nim dowiedział |

Przykład, który to uzasadnia: artykuł opublikowany o 10:00:00 trafia do nas o 10:07:23,
bo tyle wynosi cykl pollingu RSS + analiza. Decyzja podjęta o 10:03 **nie mogła** go znać.
Naiwne zapytanie treningowe filtrujące po `published_at` wciągnęłoby go do cech tej decyzji
i model nauczyłby się „przewidywać" newsy z przyszłości. Backtest wyszedłby świetnie,
a live rozjechałby się natychmiast.

Dlatego każde zapytanie treningowe filtruje po **obu**:

```sql
WHERE event_time <= :as_of AND ingested_at <= :as_of
```

Wymuszamy to jedną funkcją (`feat.as_of_features`), a nie dyscypliną programisty.

## 3. Feature store — własny, w Postgresie, nie Feast

Feast rozwiązuje trzy problemy: **point-in-time correctness**, **eliminację training/serving
skew** i **reużycie cech między modelami**. Pierwsze dwa są tu krytyczne. Trzeci — nie, bo
mamy dwa modele, nie dwieście.

Feast wymagałby osobnego offline store (kolumnowego), online store (Redis), rejestru i serwera
cech. To cztery komponenty i osobny model wdrożeniowy, żeby dostać dwie właściwości, które
w naszej skali realizuje jedna tabela i jedna funkcja SQL.

Implementujemy więc **lean feature store** o tych samych gwarancjach:

- `feat.feature_def` — **wersjonowana definicja cechy**. Zmiana logiki = nowa wersja, nigdy
  nadpisanie. Model zapisuje, na których `feature_def_id` był trenowany.
- `feat.feature_value` — hypertable, bitemporalna, jedna wartość na (definicja, instrument, czas).
- **Jedna ścieżka kodu liczy cechy dla treningu i dla live.** Silnik live nie ma własnej
  implementacji „na szybko" — to jest dokładnie mechanizm powstawania training/serving skew.
  Backfill historyczny i obliczenie bieżące wołają tę samą funkcję.

## 4. Rejestr modeli — w bazie, MLflow opcjonalnie

Research: MLflow ma sens dla zespołów poniżej ~100 eksperymentów miesięcznie i wtedy nie warto
migrować gdzie indziej; ClearML wchodzi dopiero gdy SQLite MLflow dławi się przy współbieżnych
zapisach. My mieścimy się grubo poniżej progu, ale mamy inny wymóg, którego MLflow nie pokrywa:
**wersja modelu musi być kluczem obcym przy każdej decyzji tradingowej**, żeby dało się
odtworzyć „która wersja podjęła tę transakcję". To musi żyć w tej samej bazie co decyzje.

Dlatego rejestr (`learn.model`, `learn.model_version`) trzymamy w Postgresie, a MLflow
zostawiamy jako opcjonalny dodatek do wizualizacji eksperymentów (backend: ten sam Postgres).

## 5. Dziennik decyzji — append-only

Tabela `dec.decision` jest **niemutowalna** (wymuszone triggerem). Każdy wiersz zawiera:

- co system postanowił i z jaką pewnością
- **snapshot wektora cech, który widział w momencie decyzji** (`feature_snapshot` jsonb)
- wykryty reżim i wersję modelu
- werdykt risk kernela (`allow` / `block` / `downsize`) z powodem
- uzasadnienie tekstowe

oraz rozbicie w `dec.decision_contribution`: **wkład każdej pojedynczej cechy, sygnału i newsa
w tę decyzję** (waga × wartość). To jednocześnie:

- zakładka „Ruchy" w dashboardzie odpowiada na „dlaczego" bez zgadywania,
- dataset do uczenia jest wolny od leakage'u z definicji, bo zapisany *przed* poznaniem wyniku,
- audyt regulacyjny/podatkowy ma pełny ślad.

`dec.planned_order` trzyma zlecenia **planowane** — warunki wejścia, poziomy triggerów, termin
ważności. To jest źródło sekcji „co planuje" w UI.

## 6. Uczciwe metryki — `learn.hypothesis_trial`

Research jest bezlitosny: wysokie wyniki backtestu są osiągalne po przetestowaniu **niewielu**
konfiguracji, więc świetny backtest jest domyślnie sygnałem przeuczenia. Standardowa k-fold
cross-walidacja jest nieważna na szeregach czasowych, a agenci RL przeuczają się do
konkretnych *sekwencji* zwrotów, nie do wzorców.

Odpowiedź w schemacie:

- `learn.wf_window` + `learn.wf_metric` — metryki **wyłącznie out-of-sample**, per okno
  walk-forward. Nie ma miejsca na zapis „wyniku backtestu" jako liczby do pokazania.
- `learn.hypothesis_trial` — **licznik wszystkich przetestowanych konfiguracji**, z hashem
  konfiguracji. Bez niego deflated Sharpe jest nieobliczalny, a my sami się oszukamy selekcją.
- `learn.calibration_bin` — czy pewność 70% oznacza trafność ~70%.
- `learn.drift_metric` — PSI / KS między rozkładem treningowym a bieżącym.
- `learn.learning_state` — jawny status silnika: `warmup` → `learning` → `validated` → `degraded`.
  To jest „stopień, jak się uczy algorytm" z Twojego wymagania, wyrażony tak, żeby nie kłamał.

## 7. Retencja i kompresja

| Dane | Retencja | Polityka |
|---|---|---|
| `market.candle` 1m | bezterminowo | kompresja Timescale po 7 dniach |
| `market.book_snapshot` | 90 dni | kompresja po 1 dniu, potem drop |
| `feat.feature_value` | bezterminowo | kompresja po 30 dniach |
| `dec.*`, `exec.*`, `learn.*`, `app.audit_log` | **bezterminowo, bez kompresji** | to jest zapis audytowy i dataset |

Ciągłe agregaty (`continuous aggregate`) dla świec 5m/15m/1h/1d liczone z 1m — jedno źródło
prawdy, brak rozjazdu między timeframe'ami.

## 8. Bezpieczeństwo w warstwie danych

- `app.api_credential` — klucze giełdowe **wyłącznie zaszyfrowane** (AES-GCM, klucz z k8s Secret,
  nigdy w bazie). Kolumny `*_enc` typu `bytea`. API nigdy nie zwraca odszyfrowanej wartości,
  tylko `key_hint` (4 ostatnie znaki).
- `app.audit_log` — każda zmiana ustawień, kluczy, limitów i każde przejście demo→live.
- Rola aplikacyjna `trader_app` bez `SUPERUSER`; osobna rola read-only `trader_ro` dla dashboardu
  analitycznego.

## 9. Uwaga operacyjna: IBKR

Wybrany broker equity to Interactive Brokers. Konsekwencja, którą trzeba zaplanować:
dostęp do Web API/TWS API wymaga **działającego IB Gateway**, a automatyzacja logowania jest
możliwa tylko przy uwierzytelnianiu tokenowym (OAuth) — klienci indywidualni używający
CP Gateway muszą wykonać ręczne logowanie. Praktyczne rozwiązanie: kontener IB Gateway
w trybie headless + **IBeam** do podtrzymywania i odnawiania sesji, z alertem gdy sesja padnie.
To jest jedyny komponent systemu, który nie jest w pełni bezobsługowy — warto o tym wiedzieć
przed fazą 6.

## Źródła

- [ClickHouse vs QuestDB vs TimescaleDB — porównanie 2026](https://www.index.dev/skill-vs-skill/database-timescaledb-vs-clickhouse-vs-questdb)
- [QuestDB vs ClickHouse — benchmark](https://questdb.com/blog/clickhouse-vs-questdb-comparison/)
- [TimescaleDB vs QuestDB — benchmark 2026](https://questdb.com/blog/timescaledb-vs-questdb-comparison/)
- [Best Time-Series Databases 2026](https://selfhost.dev/blog/best-time-series-databases/)
- [pgvector w produkcji 2026](https://devstarsj.github.io/2026/06/22/pgvector-postgres-vector-database-production-2026/)
- [pgvector vs Qdrant (2026)](https://open-techstack.com/blog/pgvector-vs-qdrant-2026/)
- [Feast — dokumentacja](https://docs.feast.dev/) · [Training-serving skew a Feast](https://medium.com/@scoopnisker/solving-the-training-serving-skew-problem-with-feast-feature-store-3719b47e23a2)
- [Czym jest online feature store — architektura](https://tacnode.io/post/what-is-an-online-feature-store-definition-architecture-use-cases)
- [MLflow alternatives — kiedy migrować](https://www.zenml.io/blog/mlflow-alternatives)
- [Backtest overfitting in the ML era (ScienceDirect)](https://www.sciencedirect.com/science/article/abs/pii/S0950705124011110)
- [Walk-forward validation framework (arXiv)](https://arxiv.org/html/2512.12924v1)
- [IBKR Web API — dokumentacja](https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-doc/)
- [Launching and Authenticating the Gateway (IBKR)](https://www.interactivebrokers.com/campus/trading-lessons/launching-and-authenticating-the-gateway/)
