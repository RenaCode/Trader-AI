# Trader-AI

Autonomiczny system tradingowy z własnym dashboardem, uruchamiany na VPS RenaCode.
**Dwa niezależne silniki** — krypto (Bybit) i giełda tradycyjna (Interactive Brokers) —
z osobnymi modelami, osobnym ryzykiem i osobnymi zakładkami w UI.

> **Faza demo.** System handluje wyłącznie na kontach demonstracyjnych. Przejście na
> live wymaga spełnienia kryteriów opisanych w `docs/00-research.md` §9.2 i osobnego,
> jawnego potwierdzenia w interfejsie. Projekt nie zakłada rentowności — sześć miesięcy
> demo to test tej hipotezy, a wynik negatywny jest realnym rezultatem.

## Dokumentacja

| Dokument | Zawartość |
|---|---|
| [`docs/00-research.md`](docs/00-research.md) | Wybór giełd, architektura, warstwa uczenia, ryzyko, plan wdrożenia |
| [`docs/01-data-architecture.md`](docs/01-data-architecture.md) | Wybór bazy, bitemporalność, feature store, rejestr modeli, retencja |

## Stan projektu

- [x] Research i architektura
- [x] Schemat bazy (10 schematów, 8 migracji, hypertables + pgvector)
- [x] API odczytu (FastAPI) i dashboard (React + Vite)
- [x] Chart Helm, obrazy Docker, CI
- [ ] Uwierzytelnianie: hasło + TOTP, szyfrowane klucze API giełd
- [ ] Adapter Bybit demo + ingest danych rynkowych
- [ ] Worker RSS + analiza newsów przez Claude
- [ ] Silnik sygnałów i risk kernel
- [ ] Warstwa uczenia: walk-forward, detekcja reżimu
- [ ] Adapter IBKR + silnik equity

## Architektura w skrócie

```
web (nginx + React)  ──► api (FastAPI) ──┬──► PostgreSQL 17 + TimescaleDB + pgvector
   trader.renacode.com                    ├──► Redis
                                          └──► Anthropic API (claude-opus-5)
       engine-crypto ─┐
       engine-equity ─┴──► wspólny rdzeń: dane, newsy, cechy, risk kernel, audyt
```

Trzy decyzje, które niosą resztę projektu:

1. **Bitemporalność.** Każdy fakt ma `event_time` (kiedy zaszło) i `ingested_at` (kiedy
   system się dowiedział). Odczyt do treningu idzie wyłącznie przez `feat.as_of()`, która
   filtruje po obu. Bez tego model uczyłby się na newsach z przyszłości.
2. **Dziennik decyzji jest append-only** (wymuszone triggerem) i zapisywany *przed*
   poznaniem wyniku, razem ze snapshotem cech. To jednocześnie odpowiedź na „dlaczego"
   w UI i dataset wolny od leakage'u.
3. **Metryki tylko out-of-sample**, z licznikiem przetestowanych konfiguracji. Nie ma
   gdzie wpisać „wyniku backtestu", bo świetny backtest jest sygnałem przeuczenia.

## Uruchomienie lokalne

```bash
cp .env.example .env
# wygeneruj klucz szyfrowania sekretów:
python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"   # -> SECRETS_KEY

docker compose up -d              # PostgreSQL + TimescaleDB + pgvector, Redis
python -m venv .venv && .venv/bin/pip install -r services/api/requirements.txt

set -a; . ./.env; set +a
.venv/bin/python db/migrate.py            # migracje
.venv/bin/python services/seed_demo.py --reset   # dane demonstracyjne (opcjonalnie)

.venv/bin/uvicorn services.api.main:app --port 8099 &
cd web && npm install && npm run dev      # http://localhost:5174
```

`seed_demo.py` generuje atrapę o właściwym *kształcie* — nie jest backtestem ani
symulacją strategii. Liczby nie znaczą nic poza tym, że wykresy mają co rysować.

## Baza danych

| Schemat | Zawartość |
|---|---|
| `ref` | Giełdy, instrumenty, kalendarz sesji, akcje korporacyjne |
| `market` | Świece (hypertable), orderbook, funding — plus ciągłe agregaty 5m/1h/1d |
| `news` | Kanały RSS, artykuły, analiza Claude, embeddingi (pgvector + HNSW) |
| `feat` | Wersjonowane definicje cech, wartości, reżim, funkcja `as_of()` |
| `sig` | Definicje i wartości sygnałów |
| `journal` | **Append-only** dziennik decyzji, rozbicie na wkłady, zlecenia planowane |
| `exec` | Zlecenia, wypełnienia, pozycje, krzywa kapitału |
| `learn` | Rejestr modeli, walk-forward, kalibracja, dryf, licznik hipotez |
| `risk` | Limity, wykorzystanie, naruszenia, kill-switch, heartbeat |
| `app` | Użytkownicy, TOTP, WebAuthn, sesje, zaszyfrowane klucze API, audyt |

Migracje są **niemutowalne** — runner porównuje sumy kontrolne i odmawia startu, jeśli
zastosowany plik został zmieniony. Poprawki idą jako nowy plik.

## Wdrożenie

GitOps: push do `main` → GitHub Actions buduje obrazy do `ghcr.io` i podbija tagi
w `charts/trader/values.yaml` → ArgoCD na VPS synchronizuje klaster.

Sekrety **nie** trafiają do repozytorium — tworzone raz, ręcznie:

```bash
kubectl create secret generic trader-secret \
  --from-literal=POSTGRES_PASSWORD='...' \
  --from-literal=SECRETS_KEY='...' \
  --from-literal=ANTHROPIC_API_KEY='...'
```

TLS: cert-manager (`letsencrypt-prod`) + Traefik, tak jak reszta usług na VPS.
Kontener nginx serwuje czysty HTTP — certyfikat żyje wyłącznie na krawędzi.
