"""Log dozorcy ma opisywać stan, a nie zalewać go jedną linią.

STAN ZMIERZONY NA PRODUKCJI 2026-09-12

    $ kubectl logs deploy/trader-dozorca --tail=100000 | wc -l
    618
    $ ... | sed 's/[0-9]//g' | sort | uniq -c | sort -rn | head -2
        615   id= Kapitał . spadł do progu .: min_equity ma regułę ręczną — …
          1   [::] stan konta: block — Kill-switch aktywny od …

Sześćset piętnaście z sześciuset osiemnastu linii to jedno zdanie powtórzone.
`wznow()` woła dozorca co 60 s, a kill-switch z regułą ręczną trwa, dopóki nie
dotknie go człowiek — tu 75 godzin. Sam dozorca tłumił już wypis werdyktu
(„zmiana stanu jest zdarzeniem; stan niezmieniony nie jest"), ale `wznow()`
wypisywało swoje linie bezwarunkowo i to ono zalewało bufor.

DLACZEGO TO NIE JEST KOSMETYKA. To ta sama wada, którą ten projekt tropi
w kanale powiadomień i w kodzie wyjścia czujek: SYGNAŁ, KTÓRY PRZYCHODZI
ZAWSZE, PRZESTAJE COKOLWIEK ZNACZYĆ. Log zalany jedną linią nie jest logiem
bogatszym — jest logiem, w którym nie widać niczego innego: ani wyjątku z cyklu
dozorcy, ani chwili, w której stan się zmienił. Przy 1440 liniach na dobę bufor
poda wypycha przy tym wszystko starsze.

CZEGO TEN PLIK PILNUJE PO OBU STRONACH

Tłumienie ma nie zgubić ani jednej z trzech rzeczy: że stan JEST, że TRWA,
i JAK DŁUGO. Cisza całkowita byłaby tu błędem gorszym niż zalew, bo stan trwale
zły jest wtedy nieodróżnialny od spokoju — dokładnie ta cisza, którą reszta
tego systemu likwiduje, nie produkuje.
"""
from __future__ import annotations

import asyncio

from services.engine.wznowienie import Dziennik, wznow


class Zegar:
    """Sterowany zegar. Rytm liczony na `time.monotonic()` byłby w teście
    zakładem o czas wykonania, a nie pomiarem."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def przesun(self, sekund: float) -> None:
        self.t += sekund


class Baza:
    """Atrapa bazy: JEDNO aktywne zatrzymanie na `min_equity` z regułą ręczną.

    Odwzorowuje stan produkcji z 2026-09-12, łącznie z tym, co go maskowało:
    `wiek_min` rośnie z każdym przebiegiem, więc opis stanu zmienia się co cykl,
    choć sam stan stoi. Tłumienie po samym tekście nie stłumiłoby tu niczego.
    """

    def __init__(self, wznowienie: str = "reczne",
                 limit_name: str = "min_equity") -> None:
        self.wznowienie = wznowienie
        self.limit_name = limit_name
        self.wiek_min = 4490.0
        self.zwolnione: list[int] = []

    async def fetch_all(self, sql, params=None):
        if "risk.killswitch_event" in sql and "released_at IS NULL" in sql:
            if self.zwolnione:
                return []
            return [{"id": 2, "reason": "Kapitał 100063.09 spadł do progu 100563.00",
                     "limit_name": self.limit_name, "triggered_at": None,
                     "wiek_min": self.wiek_min}]
        if 'app."user"' in sql:
            return [{"id": 1}]
        raise AssertionError(f"nieoczekiwane zapytanie: {sql}")

    async def fetch_one(self, sql, params=None):
        if sql.startswith("UPDATE risk.killswitch_event"):
            # Zwolnienie idzie przez UPDATE ... RETURNING (N2-1).
            self.zwolnione.append(params[1])
            return {"id": params[1]}
        if "risk.limit_def" in sql:
            return {"value": 10, "wznowienie": self.wznowienie, "max_wznowien": 3}
        if "released_auto" in sql:
            return {"n": 0}
        # stan_kapitalu.PLAN — kapitał znacznie powyżej progu, żeby warunek
        # ustania był spełniony wszędzie tam, gdzie reguła na to pozwala.
        return {"equity": 100063.09, "nav": 1.0, "peak": 100563.0,
                "day_ago": 100063.09, "szczyt": 1.0, "wplaty_netto": 100000.0,
                "event_time": None}

    async def execute(self, sql, params=None):
        if "released_at = now()" in sql:
            self.zwolnione.append(params[1])

    async def notify(self, *a, **k):
        pass


def przebieg(baza, dz, ile: int, zegar: Zegar, co_s: float = 60.0) -> None:
    """`ile` cykli dozorcy, po jednym na `co_s` sekund."""
    for _ in range(ile):
        asyncio.run(wznow(baza.fetch_all, baza.fetch_one, baza.execute,
                          baza.notify, dziennik=dz))
        baza.wiek_min += co_s / 60
        zegar.przesun(co_s)


def linie(capsys) -> list[str]:
    return [l for l in capsys.readouterr().out.splitlines() if l.strip()]


# ---------------------------------------------------------------- zalew

def test_KANAREK_trwaly_stan_NIE_zalewa_logu(capsys):
    """SEDNO. Godzina pracy dozorcy to 60 przebiegów — i jedna linia o stanie,
    który się nie zmienił, a nie sześćdziesiąt."""
    zegar = Zegar()
    baza, dz = Baza(), Dziennik(przypomnienie_s=3600, zegar=zegar)
    przebieg(baza, dz, 55, zegar)

    reczne = [l for l in linie(capsys) if "regułę ręczną" in l]
    assert len(reczne) == 1, (
        f"55 przebiegów dało {len(reczne)} identycznych linii o tym samym "
        f"stanie — log zalany jedną linią nie pokaże niczego innego")


def test_KANAREK_drgajace_liczby_w_opisie_NIE_omijaja_tlumienia(capsys):
    """Najprostszy sposób zepsucia tej zmiany: porównywać po tekście.

    `wiek_min` rośnie co cykl, więc opis stanu jest za każdym razem inny.
    Tożsamością stanu jest `znacznik`, a nie renderowana linia.
    """
    zegar = Zegar()
    baza, dz = Baza(), Dziennik(przypomnienie_s=3600, zegar=zegar)
    przebieg(baza, dz, 10, zegar)

    assert len([l for l in linie(capsys) if "regułę ręczną" in l]) == 1


# ------------------------------------------------ informacja NIE ginie

def test_pierwszy_przebieg_MOWI_od_razu(capsys):
    """Tłumienie nie może opóźnić pierwszego opisu. Dozorca po restarcie ma
    powiedzieć, w jakim stanie zastał system, a nie za godzinę."""
    zegar = Zegar()
    przebieg(Baza(), Dziennik(przypomnienie_s=3600, zegar=zegar), 1, zegar)
    assert any("regułę ręczną" in l for l in linie(capsys))


def test_przypomnienie_NIESIE_ile_stan_trwa_i_ile_pominieto(capsys):
    """Rytm tłumi POWTÓRKĘ, nie informację. Przypomnienie ma powiedzieć to,
    czego w logu nie widać: że stan stoi nieprzerwanie i ile przebiegów go
    potwierdziło."""
    zegar = Zegar()
    baza, dz = Baza(), Dziennik(przypomnienie_s=3600, zegar=zegar)
    przebieg(baza, dz, 121, zegar)

    tekst = "\n".join(linie(capsys))
    assert tekst.count("regułę ręczną") == 3, (
        "dwie godziny pracy to pierwszy opis i dwa przypomnienia")
    assert "stan TRWA" in tekst
    assert "59 identycznych przebiegów pominięto" in tekst, (
        f"przypomnienie nie mówi, ilu przebiegów nie widać w logu:\n{tekst}")
    assert "nieprzerwanie od 60 min" in tekst


def test_ZMIANA_stanu_idzie_do_logu_NATYCHMIAST(capsys):
    """Stan zmieniony w minucie 5 nie może czekać do końca rytmu — to jest
    jedyna chwila, w której log ma coś nowego do powiedzenia."""
    zegar = Zegar()
    baza = Baza(limit_name="max_drawdown")
    dz = Dziennik(przypomnienie_s=3600, zegar=zegar)
    przebieg(baza, dz, 5, zegar)
    capsys.readouterr()

    baza.wznowienie = "automatyczne"          # człowiek zdjął regułę ręczną
    przebieg(baza, dz, 1, zegar)

    assert any("WZNAWIAM" in l for l in linie(capsys)), (
        "zmiana stanu przepadła w rytmie tłumienia poprzedniego stanu")


def test_stan_ktory_WROCIL_wypisuje_sie_od_nowa(capsys):
    """Po zdarzeniu pamięć stanu jest kasowana. Inaczej nawrót tego samego
    stanu wpadałby w rytm sprzed zdarzenia i milczał — a to jest osobny
    epizod, nie ciąg dalszy poprzedniego."""
    zegar = Zegar()
    baza = Baza(wznowienie="automatyczne", limit_name="max_drawdown")
    dz = Dziennik(przypomnienie_s=3600, zegar=zegar)
    przebieg(baza, dz, 1, zegar)            # WZNAWIAM — zdarzenie
    assert baza.zwolnione == [2]
    capsys.readouterr()

    baza.zwolnione.clear()                   # ten sam limit strzela ponownie
    baza.wznowienie = "reczne"
    zegar.przesun(120)
    przebieg(baza, dz, 1, zegar)

    assert any("regułę ręczną" in l for l in linie(capsys)), (
        "nawrót stanu uciszony rytmem sprzed zdarzenia")


def test_NOWY_proces_wypisuje_komplet(capsys):
    """Pamięć siedzi w procesie, więc restart poda zaczyna log od nowa —
    i musi. `kubectl logs` po restarcie odpowiada na pytanie o stan tylko
    wtedy, gdy pierwszy przebieg wypisał komplet."""
    zegar = Zegar()
    baza = Baza()
    przebieg(baza, Dziennik(przypomnienie_s=3600, zegar=zegar), 30, zegar)
    capsys.readouterr()

    przebieg(baza, Dziennik(przypomnienie_s=3600, zegar=zegar), 1, zegar)
    assert any("regułę ręczną" in l for l in linie(capsys))


def test_brak_zatrzyman_tez_nie_zalewa_ale_JEST_widoczny(capsys):
    """„Brak aktywnych kill-switchy" to też stan i też szedł co 60 s. Ma być
    widoczny raz, a nie 1440 razy na dobę — i nie ma zniknąć zupełnie, bo wtedy
    log milczy o tym, że dozorca w ogóle przeszedł tę ścieżkę."""
    zegar = Zegar()
    baza, dz = Baza(), Dziennik(przypomnienie_s=3600, zegar=zegar)
    baza.zwolnione.append(2)                 # nic aktywnego
    przebieg(baza, dz, 30, zegar)

    brak = [l for l in linie(capsys) if "brak aktywnych kill-switchy" in l]
    assert len(brak) == 1, f"{len(brak)} linii o braku zatrzymań"
