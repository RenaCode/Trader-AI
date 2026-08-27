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
from .routers import auth, learning, moves, overview, risk, sources


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

_protected = [overview.router, learning.router, moves.router, sources.router, risk.router]
for r in _protected:
    app.include_router(r, dependencies=[Depends(current_user)])


@app.get("/api/health")
async def health():
    row = await fetch_one("SELECT count(*) AS n FROM schema_migrations")
    return {"status": "ok", "migrations": row["n"] if row else 0}


@app.get("/api/engines", dependencies=[Depends(current_user)])
async def engines():
    """Lista silnikow = zakladki w UI. Zrodlem prawdy jest baza, nie stala w kodzie."""
    rows = await fetch_one(
        """
        SELECT jsonb_agg(x ORDER BY x->>'engine') AS engines FROM (
            SELECT jsonb_build_object(
                'engine', v.engine,
                'venue', v.name,
                'venue_code', v.code,
                'is_24_7', v.is_24_7,
                'instruments', (SELECT count(*) FROM ref.instrument i
                                WHERE i.venue_id = v.id AND i.active)
            ) AS x
            FROM ref.venue v
        ) s
        """
    )
    return rows["engines"] if rows and rows["engines"] else []
