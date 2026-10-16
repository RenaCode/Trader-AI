"""Hasło roli bazy idzie w `PGPASSWORD`, nie w łańcuchu połączenia.

CO BYŁO ZŁE (renacode-infra: docs/projekty/trader/08-przed-go-live.md brama 3, audyt Fable F-9.4)

`DATABASE_URL` składał się w charcie podstawieniem Kubernetesa:
`postgresql://user:$(ROLA_DB_PASSWORD)@host:5432/db`. Kodowanie procentowe jest
tam niemożliwe, więc hasło ze znakiem `/`, `@`, `?` albo `#` rozcina URL —
a komunikat mówi o nierozwiązanym hoście i porcie, nie o haśle. Cztery role
działały przez przypadek: ich hasła nie trafiły na żaden z tych znaków.

Teraz URL nie zawiera hasła wcale, a libpq bierze je ze zmiennej `PGPASSWORD`
(ta sama zasada, którą kopia zapasowa stosuje od początku). Kod się nie zmienia:
`psycopg.connect(url)` i pula połączeń idą przez libpq, który czyta `PGPASSWORD`,
gdy w łańcuchu hasła nie ma.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
import yaml

KORZEN = Path(__file__).resolve().parents[1]
SZABLONY = KORZEN / "charts" / "trader" / "templates"

# `$(ZMIENNA)` wewnątrz wartości env - podstawienie Kubernetesa.
PODSTAWIENIE = re.compile(r"\$\([A-Z_]+\)")


def test_zaden_szablon_nie_sklada_hasla_w_url():
    for plik in sorted(SZABLONY.glob("*")):
        tresc = plik.read_text(encoding="utf-8")
        for linia in tresc.splitlines():
            if linia.lstrip().startswith(("#", "{{/*")):
                continue
            if "postgresql://" in linia:
                assert not PODSTAWIENIE.search(linia), (
                    f"{plik.name}: hasło podstawiane w URL — znak `/` w haśle "
                    f"rozetnie połączenie:\n{linia.strip()}")


bez_helma = pytest.mark.skipif(shutil.which("helm") is None,
                               reason="helm niedostępny")


@bez_helma
def test_RENDER_kazdy_kontener_z_url_ma_PGPASSWORD_z_sekretu():
    """Na tym, co zobaczy klaster: każdy kontener z `DATABASE_URL` dostaje
    `PGPASSWORD` WPROST z sekretu (bez podstawień), a URL nie ma hasła."""
    wynik = subprocess.run(["helm", "template", str(KORZEN / "charts" / "trader")],
                           capture_output=True, text=True)
    assert wynik.returncode == 0, wynik.stderr
    kontenery = []
    for d in yaml.safe_load_all(wynik.stdout):
        if not d or d.get("kind") not in ("Deployment", "CronJob"):
            continue
        spec = d["spec"]
        if d["kind"] == "CronJob":
            spec = spec["jobTemplate"]["spec"]
        pod = spec["template"]["spec"]
        for c in (pod.get("initContainers") or []) + pod["containers"]:
            kontenery.append((d["metadata"]["name"], c))

    z_url = 0
    for nazwa, c in kontenery:
        env = {e["name"]: e for e in c.get("env") or []}
        if "DATABASE_URL" not in env:
            continue
        z_url += 1
        url = env["DATABASE_URL"]["value"]
        uzytkownik = url.split("://", 1)[1].split("@", 1)[0]
        assert ":" not in uzytkownik, f"{nazwa}/{c['name']}: hasło w URL ({url})"
        assert "PGPASSWORD" in env, f"{nazwa}/{c['name']}: brak PGPASSWORD"
        assert "secretKeyRef" in env["PGPASSWORD"].get("valueFrom", {}), (
            f"{nazwa}/{c['name']}: PGPASSWORD nie idzie wprost z sekretu")
    assert z_url >= 10, f"render dał tylko {z_url} kontenerów z bazą"


DB = os.environ.get("TEST_DB_URL")


@pytest.mark.skipif(not DB, reason="ustaw TEST_DB_URL na jednorazowa baze")
def test_haslo_ze_znakami_URL_laczy_sie_przez_PGPASSWORD(monkeypatch):
    """Na prawdziwym Postgresie: rola z hasłem `a/b@c?d#e` łączy się URL-em
    bez hasła i `PGPASSWORD` — zarówno `psycopg.connect`, jak i pulą API."""
    import asyncio

    import psycopg

    rola = f"test_haslo_{uuid.uuid4().hex[:8]}"
    haslo = "a/b@c?d#e:f%"
    with psycopg.connect(DB, autocommit=True) as c:
        c.execute(f"CREATE ROLE {rola} LOGIN PASSWORD '{haslo}'")
    try:
        czesci = psycopg.conninfo.conninfo_to_dict(DB)
        url = (f"postgresql://{rola}@{czesci.get('host', 'localhost')}:"
               f"{czesci.get('port', 5432)}/{czesci['dbname']}")
        monkeypatch.setenv("PGPASSWORD", haslo)
        with psycopg.connect(url) as c:
            assert c.execute("SELECT current_user").fetchone()[0] == rola

        monkeypatch.setenv("DATABASE_URL", url)
        from services.api import db as api_db

        async def przez_pule():
            await api_db.open_pool()
            try:
                return await api_db.fetch_one("SELECT current_user AS u")
            finally:
                await api_db.close_pool()

        assert asyncio.run(przez_pule())["u"] == rola
    finally:
        with psycopg.connect(DB, autocommit=True) as c:
            c.execute(f"DROP ROLE IF EXISTS {rola}")
