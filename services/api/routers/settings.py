"""Sekrety aplikacji (klucze dostawcow analizy) i portfele obserwowane."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth import audit, current_user, require_csrf, wymagaj_wlasciciela
from ..db import execute, fetch_all, fetch_one
from ..security import SecretBox

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Rejestr znanych sekretow. Zamkniety zbior, zeby przez to API nie dalo sie
# wstawic dowolnego klucza do bazy.
KNOWN_SECRETS = {
    "anthropic_api_key": {
        "label": "Klucz API Anthropic",
        "description": "Skaner wiadomości. Gdy ustawione są OBA klucze, pracują RAZEM: "
                       "Anthropic analizuje artykuły, które przeszły filtr, i jest też "
                       "ścieżką zapasową filtra, gdy Gemini wyczerpie limit. "
                       "Sam Anthropic robi jedno i drugie.",
        "pattern": r"^sk-ant-[A-Za-z0-9_\-]{20,}$",
        "pattern_hint": "Klucz Anthropic zaczyna się od 'sk-ant-'.",
        "testable": True,
    },
    # Sciezka zapasowa na czas oczekiwania na klucz Anthropica. Wzorzec obejmuje
    # oba formaty kluczy Google: starszy `AIza...` i nowszy `AQ....`.
    "gemini_api_key": {
        "label": "Klucz API Google Gemini",
        "description": "Skaner wiadomości. Gdy ustawione są OBA klucze, Gemini FILTRUJE "
                       "artykuły — to 94% wywołań i tam jest 5,5× tańszy od Anthropica. "
                       "Analizę zostawia Anthropicowi. Sam Gemini robi jedno i drugie.",
        "pattern": r"^(AIza[A-Za-z0-9_\-]{30,}|AQ\.[A-Za-z0-9_\-]{20,})$",
        "pattern_hint": "Klucz Google zaczyna się od 'AIza' albo 'AQ.'.",
        "testable": True,
    },
}

# Wzorce adresow. Celowo luzne - chodzi o wylapanie oczywistej pomylki
# (np. wklejonej frazy odzyskiwania), nie o pelna walidacje kryptograficzna.
ADDRESS_PATTERNS = {
    "bitcoin": r"^(bc1[a-z0-9]{25,90}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})$",
    "ethereum": r"^0x[a-fA-F0-9]{40}$",
}
XPUB_PATTERN = r"^([xyz]pub[1-9A-HJ-NP-Za-km-z]{100,120})$"

# Fraza odzyskiwania to 12-24 slowa. Jesli ktos wklei ja w pole adresu, musimy
# to wylapac i odmowic - zapisanie jej oznaczaloby oddanie kontroli nad srodkami.
SEED_PHRASE = re.compile(r"^(\s*[a-z]{3,8}\s+){11,23}[a-z]{3,8}\s*$", re.I)


class SecretBody(BaseModel):
    value: str = Field(min_length=8, max_length=512)


class WalletBody(BaseModel):
    chain: str = Field(pattern="^(bitcoin|ethereum)$")
    label: str = Field(min_length=1, max_length=64)
    identifier: str = Field(min_length=10, max_length=200)


@router.get("/secrets")
async def list_secrets(user: dict = Depends(current_user)):
    rows = await fetch_all("SELECT name, hint, updated_at FROM app.secret")
    have = {r["name"]: r for r in rows}
    return [
        {
            "name": name,
            "label": meta["label"],
            "description": meta["description"],
            "is_set": name in have,
            "hint": have[name]["hint"] if name in have else None,
            "updated_at": have[name]["updated_at"].isoformat() if name in have else None,
        }
        for name, meta in KNOWN_SECRETS.items()
    ]


@router.get("/analiza-tryb")
async def tryb_analizy(user: dict = Depends(current_user)):
    """Kto NAPRAWDE filtruje, a kto analizuje — przy obecnych kluczach.

    Interfejs opisywal dotad oba klucze tak, jakby drugi byl tylko zapasem
    ("uzywany tylko wtedy, gdy klucz Anthropic nie jest ustawiony"), podczas gdy
    silnik przy dwoch kluczach buduje MIESZANKE. Wlasciciel wpisal oba klucze
    i z opisu wynikalo, ze jeden lezy nieuzywany - a on przez caly czas
    wykonywal 94% wywolan.

    Zrodlem prawdy jest tu ta sama funkcja, ktorej uzywa worker
    (`zbuduj_dostawce`), a nie druga kopia reguly. Kopia rozjechalaby sie przy
    pierwszej zmianie i to wlasnie ten ekran zapewnialby wtedy, ze jest inaczej.
    """
    from services.ingest.llm import zbuduj_dostawce

    rows = await fetch_all("SELECT name FROM app.secret")
    ustawione = {r["name"] for r in rows}
    # Wartosci sa nieistotne - `zbuduj_dostawce` decyduje po OBECNOSCI kluczy,
    # a odszyfrowywanie ich tylko po to, zeby narysowac ekran, byloby
    # wystawieniem sekretu bez powodu.
    d = zbuduj_dostawce({
        "anthropic": "x" if "anthropic_api_key" in ustawione else None,
        "gemini": "x" if "gemini_api_key" in ustawione else None,
    })
    if d is None:
        return {"aktywny": False,
                "opis": "Brak klucza — skaner pobiera artykuły, ale ich nie analizuje."}
    # DOSTAWCA ZBUDOWANY TU MUSI TU ZGINAC. `zbuduj_dostawce` nie jest odczytem
    # konfiguracji - kazdy zwrocony obiekt otwiera pule polaczen (httpx u Gemini,
    # klient SDK u Anthropica). Worker domyka je od 2026-09-10, ten ekran nie
    # domykal ich wcale, a jest gorszym miejscem na taki wyciek: proces API zyje
    # tygodniami, a `analiza-tryb` jest wolane przy kazdym wejsciu w ustawienia
    # I po kazdym zapisie sekretu (`Settings.jsx`), wiec pule przybywaja bez
    # zadnego ograniczenia.
    #
    # Do narysowania ekranu potrzebne sa same nazwy, wiec przepisujemy je, zanim
    # obiekt pojdzie do zamkniecia - odpowiedz nie ma go prawa przezyc.
    try:
        return {
            "aktywny": True,
            "dostawca": d.nazwa,
            "model_triazu": d.model_triazu,
            "model_analizy": d.model_analizy,
            "mieszanka": "+" in d.nazwa,
        }
    finally:
        # Cisza przy bledzie, tak samo jak w `news_worker.zamknij_dostawce`:
        # nieudane domkniecie gniazda nie ma prawa zamienic odczytu ekranu
        # ustawien w blad 500.
        try:
            await d.zamknij()
        except Exception as e:                               # noqa: BLE001
            print(f"[uwaga] nie udało się zamknąć dostawcy {d.nazwa}: "
                  f"{type(e).__name__}: {e}")


@router.put("/secrets/{name}",
            dependencies=[Depends(require_csrf), Depends(wymagaj_wlasciciela)])
async def set_secret(name: str, body: SecretBody, request: Request,
                     user: dict = Depends(current_user)):
    meta = KNOWN_SECRETS.get(name)
    if not meta:
        raise HTTPException(404, f"Nieznany sekret: {name}")

    value = body.value.strip()
    if meta.get("pattern") and not re.match(meta["pattern"], value):
        raise HTTPException(400, meta["pattern_hint"])

    await execute(
        """
        INSERT INTO app.secret (name, value_enc, hint, description, updated_by)
        VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (name) DO UPDATE
          SET value_enc = EXCLUDED.value_enc, hint = EXCLUDED.hint,
              updated_at = now(), updated_by = EXCLUDED.updated_by
        """,
        (name, SecretBox().encrypt(value), "••••" + value[-4:], meta["description"], user["id"]),
    )
    # Do audytu trafia sam FAKT zmiany, nigdy wartosc.
    await audit("secret.set", user_id=user["id"], entity="app.secret", entity_id=name,
                request=request)
    return {"ok": True, "hint": "••••" + value[-4:]}


@router.delete("/secrets/{name}",
               dependencies=[Depends(require_csrf), Depends(wymagaj_wlasciciela)])
async def delete_secret(name: str, request: Request, user: dict = Depends(current_user)):
    if name not in KNOWN_SECRETS:
        raise HTTPException(404, f"Nieznany sekret: {name}")
    await execute("DELETE FROM app.secret WHERE name = %s", (name,))
    await audit("secret.delete", user_id=user["id"], entity="app.secret", entity_id=name,
                request=request)
    return {"ok": True}


# ------------------------------------------------------------------ portfele
@router.get("/wallets")
async def list_wallets(user: dict = Depends(current_user)):
    rows = await fetch_all(
        "SELECT id, chain, label, address, xpub, balance, balance_at, last_error, created_at"
        " FROM app.wallet WHERE user_id = %s ORDER BY chain, label",
        (user["id"],),
    )
    return [
        {
            "id": r["id"], "chain": r["chain"], "label": r["label"],
            # xpub pokazujemy skrocony - pelny pozwala wyliczyc wszystkie adresy,
            # co jest problemem prywatnosci, choc nie bezpieczenstwa srodkow.
            "identifier": r["address"] or (r["xpub"][:14] + "…" + r["xpub"][-6:] if r["xpub"] else ""),
            "kind": "adres" if r["address"] else "xpub",
            "balance": float(r["balance"]) if r["balance"] is not None else None,
            "balance_at": r["balance_at"].isoformat() if r["balance_at"] else None,
            "last_error": r["last_error"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


@router.post("/wallets",
             dependencies=[Depends(require_csrf), Depends(wymagaj_wlasciciela)])
async def add_wallet(body: WalletBody, request: Request, user: dict = Depends(current_user)):
    ident = body.identifier.strip()

    # Najwazniejsza kontrola w tym module. Fraza odzyskiwania daje pelna kontrole
    # nad srodkami - nie moze trafic do bazy aplikacji webowej pod zadnym pozorem.
    if SEED_PHRASE.match(ident):
        raise HTTPException(
            400,
            "To wygląda na frazę odzyskiwania. Ten system NIGDY nie przechowuje fraz "
            "odzyskiwania ani kluczy prywatnych — wyciek bazy oznaczałby nieodwracalną "
            "utratę środków. Podaj wyłącznie adres portfela albo klucz publiczny (xpub); "
            "do śledzenia salda to w zupełności wystarczy.",
        )
    if re.match(r"^[5KL][1-9A-HJ-NP-Za-km-z]{50,51}$", ident) or re.match(r"^(0x)?[a-fA-F0-9]{64}$", ident):
        raise HTTPException(
            400,
            "To wygląda na klucz prywatny. Ten system przechowuje wyłącznie dane "
            "pozwalające OBSERWOWAĆ portfel — adres albo xpub. Klucz prywatny nie jest "
            "potrzebny i nie zostanie zapisany.",
        )

    is_xpub = bool(re.match(XPUB_PATTERN, ident))
    is_address = bool(re.match(ADDRESS_PATTERNS[body.chain], ident))
    if not (is_xpub or is_address):
        raise HTTPException(
            400,
            f"To nie wygląda na adres {body.chain} ani na klucz publiczny xpub. "
            "Sprawdź, czy nie ma literówki.",
        )

    try:
        await execute(
            "INSERT INTO app.wallet (user_id, chain, label, address, xpub)"
            " VALUES (%s,%s,%s,%s,%s)",
            (user["id"], body.chain, body.label.strip(),
             None if is_xpub else ident, ident if is_xpub else None),
        )
    except Exception as e:                                      # noqa: BLE001
        if "wallet_user_id_chain_label_key" in str(e):
            raise HTTPException(409, "Portfel o tej etykiecie już istnieje") from e
        raise

    await audit("wallet.add", user_id=user["id"], entity="app.wallet",
                entity_id=body.label, request=request)
    return {"ok": True, "kind": "xpub" if is_xpub else "adres"}


@router.delete("/wallets/{wallet_id}",
               dependencies=[Depends(require_csrf), Depends(wymagaj_wlasciciela)])
async def delete_wallet(wallet_id: int, request: Request, user: dict = Depends(current_user)):
    row = await fetch_one("SELECT label FROM app.wallet WHERE id = %s AND user_id = %s",
                          (wallet_id, user["id"]))
    if not row:
        raise HTTPException(404, "Nie ma takiego portfela")
    await execute("DELETE FROM app.wallet WHERE id = %s", (wallet_id,))
    await audit("wallet.delete", user_id=user["id"], entity="app.wallet",
                entity_id=row["label"], request=request)
    return {"ok": True}


@router.post("/secrets/{name}/test",
             dependencies=[Depends(require_csrf), Depends(wymagaj_wlasciciela)])
async def test_secret(name: str, user: dict = Depends(current_user)):
    """Sprawdza zapisany klucz PRAWDZIWYM wywolaniem API.

    Poprawny format nic nie dowodzi - klucz moze byc odwolany, z wyczerpanym
    limitem albo z innej organizacji. Jedynym wiarygodnym testem jest zapytanie.
    Uzywamy najtanszego modelu i jednego tokenu, zeby test kosztowal grosze.
    """
    if name not in KNOWN_SECRETS:
        raise HTTPException(404, f"Nieznany sekret: {name}")
    row = await fetch_one("SELECT value_enc FROM app.secret WHERE name = %s", (name,))
    if not row:
        raise HTTPException(404, "Klucz nie jest jeszcze zapisany")

    key = SecretBox().decrypt(bytes(row["value_enc"]))

    if name == "anthropic_api_key":
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=key)
        try:
            models = await client.models.list()
            names = [m.id for m in models.data][:6]
            return {"ok": True,
                    "message": f"Klucz działa. Dostępne modele: {', '.join(names)}.",
                    "models": names}
        except Exception as e:                                  # noqa: BLE001
            detail = str(e)
            if "authentication" in detail.lower() or "401" in detail:
                msg = ("Klucz odrzucony przez Anthropic. Sprawdź, czy nie został odwołany "
                       "i czy skopiowałeś go w całości.")
            elif "credit" in detail.lower() or "billing" in detail.lower() or "402" in detail:
                msg = ("Klucz jest poprawny, ale organizacja nie ma środków. Doładuj konto "
                       "w Console → Billing.")
            else:
                msg = f"Nie udało się zweryfikować: {type(e).__name__}: {detail[:200]}"
            return {"ok": False, "message": msg}

    if name == "gemini_api_key":
        import httpx
        try:
            async with httpx.AsyncClient(timeout=20.0) as http:
                r = await http.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    headers={"x-goog-api-key": key})
            if r.status_code in (401, 403):
                return {"ok": False, "message": (
                    "Klucz odrzucony przez Google. Sprawdź, czy nie został usunięty "
                    "i czy projekt ma włączone Generative Language API.")}
            r.raise_for_status()
            names = [m["name"].removeprefix("models/") for m in r.json().get("models", [])
                     if "generateContent" in m.get("supportedGenerationMethods", [])][:6]
            if not names:
                return {"ok": False, "message":
                        "Klucz działa, ale nie widzi żadnego modelu do generowania treści."}

            # PROBNE WYGENEROWANIE, nie samo wypisanie modeli.
            #
            # Lista modeli NIE ZUZYWA limitu generowania, wiec przy wyczerpanym
            # limicie dobowym ten test meldowal "Klucz dziala" - i slusznie,
            # bo klucz dziala. Tyle ze skaner i tak nie mogl nic zrobic,
            # a wlasciciel mial czarno na bialym, ze wszystko gra. Zdarzylo sie
            # 2026-09-08: 500 zapytan na dobe wyczerpane, potok stal,
            # a test klucza byl zielony.
            from services.ingest.llm import DostawcaGemini

            model = min(names, key=len)
            async with httpx.AsyncClient(timeout=20.0) as http:
                proba = await http.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model}:generateContent",
                    headers={"x-goog-api-key": key},
                    json={"contents": [{"role": "user", "parts": [{"text": "ping"}]}],
                          "generationConfig": {"maxOutputTokens": 8}})
            if proba.status_code == 429:
                dobowy = DostawcaGemini.limit_dobowy(proba)
                return {"ok": False, "models": names, "message": (
                    "Klucz jest poprawny, ale WYCZERPAŁ LIMIT "
                    + ("DOBOWY — odnowi się następnej doby. "
                       if dobowy else "tempa zapytań. ")
                    + "Skaner nadal działa: filtrowanie przechodzi wtedy na "
                      "Anthropica, jeśli jego klucz jest ustawiony.")}
            if proba.status_code >= 400:
                return {"ok": False, "models": names, "message": (
                    f"Klucz widzi modele, ale próbne wywołanie zwróciło "
                    f"HTTP {proba.status_code}.")}
            return {"ok": True,
                    "message": f"Klucz działa, próbne wywołanie przeszło. "
                               f"Dostępne modele: {', '.join(names)}.",
                    "models": names}
        except Exception as e:                                  # noqa: BLE001
            return {"ok": False,
                    "message": f"Nie udało się zweryfikować: {type(e).__name__}: {str(e)[:200]}"}

    raise HTTPException(501, "Test dla tego sekretu nie jest zaimplementowany")
