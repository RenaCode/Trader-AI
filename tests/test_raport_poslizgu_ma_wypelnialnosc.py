"""`dozor --co poslizg` wypisuje wypełnialność z rozbiciem przepadłych (audyt 3 T2).

DO 2026-10-03 `sprawdz_poslizg` wołał `opis(pomiar(wiersze))` bez
`wypelnialnosc(...)`. Pełna wersja była tylko w `poslizg.main`, którego nikt nie
uruchamia, a raport czytany codziennie w `zywotnosc` dwa razy odsyłał do
„wypełnialności z `exec.order` niżej" — której niżej nie było. Wypełnialność to
jedyna miara efektu E1–E5 (cena PostOnly z arkusza tuż przed wysyłką).
"""
from __future__ import annotations

import asyncio

from services.engine import dozor
from services.engine.poslizg import wypelnialnosc


def _fetch_all(zlecenia):
    async def fetch_all(sql, params=None):
        if "FROM exec.order o\n WHERE" in sql or "SELECT o.status" in sql:
            return zlecenia
        return []
    return fetch_all


def test_raport_dozoru_zawiera_wypelnialnosc(capsys):
    zlecenia = [{"status": "filled", "powod_odmowy": None},
                {"status": "rejected", "powod_odmowy": "postonly"}]
    assert asyncio.run(dozor.sprawdz_poslizg(_fetch_all(zlecenia))) == 0
    out = capsys.readouterr().out
    assert "wypełnialność: 1 z 2" in out, out
    assert "odrzucone 1 (w tym PostOnly 1)" in out, out


def test_rozbicie_przepadlych_rozdziela_postonly_od_innych_odmow():
    w = wypelnialnosc([
        {"status": "filled"}, {"status": "partial"},
        {"status": "rejected", "powod_odmowy": "postonly"},
        {"status": "rejected", "powod_odmowy": "saldo"},
        {"status": "cancelled"}, {"status": "cancelled"},
        {"status": "expired"}, {"status": "new"}])
    assert w["niewypelnionych"] == 5
    assert (w["odrzuconych"], w["odrzuconych_postonly"],
            w["anulowanych"], w["wygaslych"]) == (2, 1, 2, 1)
    assert w["nierozstrzygnietych"] == 1


# ---------------------------------------------- T10: minimum próby odrzuceń

def _stan(przyjete, odrzucone):
    return dict(przyjete=przyjete, odrzucone=odrzucone, nieznane=0,
                id_nieznanych=[], powody=["PostOnly"], bez_kontaktu=0,
                wyjscia_bez_skutku=[], zawieszone_plany=0)


def test_jedno_odrzucenie_przy_jednym_przyjeciu_nie_zapala_zadania():
    """Job z 02.10 09:40: „Więcej odrzuconych (1) niż przyjętych (1)"."""
    assert dozor.ocen_egzekucje(_stan(1, 1)) == []


def test_polowa_odrzucona_przy_wiekszej_probie_alarmuje_poprawnym_tekstem():
    (_, tytul, _), = dozor.ocen_egzekucje(_stan(2, 2))
    assert "co najmniej połowa" in tytul and "2 z 4" in tytul
