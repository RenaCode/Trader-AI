"""Trader-AI API.

Faza 1: warstwa odczytu dla dashboardu. Auth (haslo + TOTP) i zapis kluczy API
dochodza w kolejnym kroku - endpointy sa juz rozdzielone per silnik, zeby
dolozenie ich nie wymagalo przeprojektowania sciezek.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from fastapi import Depends

from .auth import current_user
from .db import close_pool, fetch_one, open_pool
from .security import SecretBox
from .routers import (auth, account, credentials, eksport, kapital, kapital_laczny,
                      learning, moves,
                      notifications,
                      overview, risk, settings, sources)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast: brak klucza szyfrujacego ma zatrzymac start, a nie wybuchnac
    # dopiero przy pierwszej konfiguracji 2FA albo zapisie klucza API gieldy.
    SecretBox()
    await open_pool()
    yield
    await close_pool()


app = FastAPI(title="Trader-AI API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "http://localhost:5174").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auth jest jedynym routerem bez ochrony - reszta wymaga waznej sesji.
# Ochrona jest deklarowana TUTAJ, a nie w kazdym routerze osobno: dodanie nowego
# routera z danymi bez tej linijki nie przejdzie code review, bo lista jest w
# jednym miejscu i widac, czego brakuje.
app.include_router(auth.router)

_protected = [overview.router, learning.router, moves.router, sources.router,
              risk.router, credentials.router, settings.router,
              notifications.router, account.router, kapital.router,
              kapital_laczny.router, eksport.router]
for r in _protected:
    app.include_router(r, dependencies=[Depends(current_user)])


@app.get("/api/health")
async def health():
    """GOTOWOSC: czy ten proces moze obsluzyc zapytanie - razem z baza.

    Zaleznosc od bazy jest tu CELOWA i ma zostac. Pod, ktory nie dosiega bazy,
    nie ma czego oddac dashboardowi, wiec `readinessProbe` (charts/trader/
    templates/api.yaml) ma go wypisac z Service.

    NIE JEST to sonda zywotnosci - od tego jest `/api/zywy` nizej. Restart
    procesu nie naprawia wolnej ani zatrzymanej bazy, a zabija jedyne miejsce,
    w ktorym widac, co sie dzieje.
    """
    row = await fetch_one("SELECT count(*) AS n FROM schema_migrations")
    return {"status": "ok", "migrations": row["n"] if row else 0}


@app.get("/api/zywy")
async def zywy():
    """ZYWOTNOSC: czy proces zyje i obsluguje petle zdarzen. Nic wiecej.

    DLACZEGO OSOBNY ENDPOINT, A NIE `/api/health`

    Do 2026-09-22 `livenessProbe` pytala o `/api/health`, czyli o zapytanie do
    bazy, z domyslnym `timeoutSeconds: 1`. Trzy wolne odpowiedzi z rzedu -
    a okno obciazenia jest znane, kopia o 03:17 i trener o 04:40 - i kubelet
    zabijal poda API. Restart nie naprawia wolnej bazy, za to kontener startowy
    `migrate` wchodzi w backoff, wiec dwuminutowa przerwa bazy (db.yaml,
    strategia `Recreate`) zamieniala sie w kilkuminutowa niedostepnosc JEDYNEGO
    miejsca, w ktorym widac stan systemu.

    Zaleznosc zewnetrzna nalezy do GOTOWOSCI, nie do zywotnosci: brak bazy ma
    wypisac poda z Service, a nie go zabic. Ta funkcja nie dotyka wiec ani
    bazy, ani Redisa, ani zadnego procesu poza wlasnym - jesli odpowiada,
    znaczy to dokladnie tyle, ze proces zyje i przyjmuje polaczenia.

    PUBLICZNY bez uwierzytelnienia, bo kubelet nie ma czym sie zalogowac,
    i celowo nie zwraca ZADNEJ informacji o stanie systemu.
    """
    return {"status": "zywy"}


@app.get("/api/engines", dependencies=[Depends(current_user)])
async def engines():
    """Lista silnikow = zakladki w UI. Zrodlem prawdy jest baza, nie stala w kodzie.

    JEDEN WPIS NA SILNIK, nie na gielde. Do 2026-09-22 zapytanie szlo po
    `ref.venue` bez grupowania, wiec dopisanie drugiej gieldy equity (alpaca
    obok placeholderu ibkr) dalo DWIE zakladki "Gielda". Gielda silnika to ta,
    z ktora umiemy rozmawiac (`fabryka.OBSLUGIWANE`) - ta sama lista, ktora
    filtruje formularz kluczy - a nie pierwszy wiersz z brzegu.
    """
    from services.exchanges.fabryka import OBSLUGIWANE
    rows = await fetch_one(
        """
        SELECT jsonb_agg(x ORDER BY x->>'engine') AS engines FROM (
            SELECT DISTINCT ON (v.engine) jsonb_build_object(
                'engine', v.engine,
                'venue', v.name,
                'venue_code', v.code,
                'is_24_7', v.is_24_7,
                'instruments', (SELECT count(*) FROM ref.instrument i
                                WHERE i.venue_id = v.id AND i.active)
            ) AS x
            FROM ref.venue v
            ORDER BY v.engine, (v.code = ANY(%s)) DESC, v.id
        ) s
        """,
        (list(OBSLUGIWANE),),
    )
    return rows["engines"] if rows and rows["engines"] else []
