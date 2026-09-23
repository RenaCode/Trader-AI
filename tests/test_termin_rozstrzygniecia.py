"""Kiedy fakt z giełdy trafia do dziennika — i dlaczego backtest czeka dalej.

DLACZEGO TEN PLIK ISTNIEJE

`termin_rozstrzygniecia` każe planowi czekać na zamknięcie świecy. Dla
rachunku papierowego jest to JEDYNA poprawna odpowiedź: pytanie brzmi tam
„czy limit został dotknięty", a odpowie na nie wyłącznie świeca domknięta
(`portfolio._wypelnione` czyta `high`/`low`). Zaglądanie do świecy trwającej
byłoby przeciekiem przyszłości.

Na giełdzie tego pytania nie zadajemy — giełda zlecenie WYKONAŁA i podaje
`orderStatus`. Czekanie jest tam przetrzymywaniem faktu, który już mamy,
przez czas do długości świecy: zmierzone 2 h 37 min na `exec.order` 48
(ETHUSDC sell, wypełnione ~13:26, `closed_at` 16:03:42, `valid_until` 16:00).
Przez ten czas risk kernel liczy ekspozycję z pozycji, której nie ma,
uzgodnienie SŁUSZNIE krzyczy co 10 minut, a CronJob czujki żywotności kończy
się kodem niezerowym.

TEN PLIK PILNUJE OBU STRON NARAZ. Zmiana dotyczy wyłącznie ścieżki giełdowej;
gdyby dotknęła wspólnej funkcji albo rachunku papierowego, backtest i produkcja
oglądałyby inny wycinek świecy — a różnica byłaby niewidoczna, bo obie wersje
wyglądałyby na poprawne.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from services.engine.gielda_exec import (
    STANY_ROZSTRZYGNIETE,
    gielda_juz_rozstrzygnela,
)
from services.engine.horizons import HORIZONS
from services.engine.loop import termin_rozstrzygniecia
from services.exchanges.base import ExchangeError, StanZlecenia

H4H = HORIZONS["4h"]          # silnik chodzi z `--bar 4h` (args deploymentu)
# Plan 36 z produkcji: świeca 08:00, rozstrzygająca 12:00, `valid_until` 16:00.
BAR = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)
PLAN = {"id": 36, "symbol": "ETHUSDC",
        "trigger": {"rodzaj": "wyjscie", "bar": BAR.isoformat()},
        "valid_until": datetime(2026, 9, 23, 16, 0, tzinfo=timezone.utc)}
# Chwila, w ktorej gielda naprawde wypelnila zlecenie 48.
WYPELNIONE_O = datetime(2026, 9, 23, 13, 26, 39, tzinfo=timezone.utc)


class Gielda:
    def __init__(self, stan=None, blad=None):
        self._stan, self._blad = stan, blad
        self.pytania = []

    async def stan_zlecenia(self, id_wlasne, kategoria="spot"):
        self.pytania.append(id_wlasne)
        if self._blad:
            raise self._blad
        return self._stan


def _stan(status, wypelniono=D("0.3395")):
    return StanZlecenia(id_wlasne="tai-p36", id_gieldy="G1", status=status,
                        ilosc=D("0.3395"), wypelniono=wypelniono,
                        srednia_cena=D("2716.26"), prowizja=D("0.92217027"),
                        prowizja_w=None)


def _pyta(stan=None, blad=None, plan=PLAN):
    g = Gielda(stan, blad)
    wynik = asyncio.run(gielda_juz_rozstrzygnela(g, plan, kategoria="spot"))
    return wynik, g


# ============================ wspolna funkcja zostaje NIETKNIETA (warunek 2)

def test_RACHUNEK_PAPIEROWY_czeka_dokladnie_tak_jak_czekal():
    """Warunek konieczny całej zmiany: semantyka wspólnej funkcji CO DO JOTY.

    Dla `now` z przedziału 13:26–16:00 — czyli przez cały czas, o który spór —
    `czekamy` ma być `True`. Symulacja nie ma innego źródła odpowiedzi niż
    domknięta świeca i mieć go nie może.
    """
    sprawdzana, czekamy = termin_rozstrzygniecia(PLAN, H4H, WYPELNIONE_O)
    assert sprawdzana == datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    assert czekamy is True, (
        "ścieżka giełdowa przeciekła do wspólnej funkcji — backtest zacząłby "
        "oglądać inny wycinek świecy niż produkcja")

    # Cala doba co 10 minut: ani jedna chwila przed 16:00 nie przestaje czekac.
    t = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    while t < PLAN["valid_until"]:
        assert termin_rozstrzygniecia(PLAN, H4H, t)[1] is True, t
        t += timedelta(minutes=10)
    assert termin_rozstrzygniecia(PLAN, H4H, PLAN["valid_until"])[1] is False


def test_wspolna_funkcja_NIE_PYTA_GIELDY_o_nic():
    """`termin_rozstrzygniecia` jest CZYSTĄ FUNKCJĄ i ma nią zostać.

    Gdyby odczyt z giełdy wszedł do niej, `loop.rozstrzygnij_plany` — czyli
    rachunek papierowy i backtest — zaczęłoby zależeć od giełdy. To jest
    ten sam argument, który trzyma `rozjazdy` i `co_z_wypelnieniem` z dala
    od sieci: test takiej logiki wymagałby wtedy giełdy, a giełda w teście
    to już nie jest test tej logiki.
    """
    import inspect
    zrodlo = inspect.getsource(termin_rozstrzygniecia)
    for zakazane in ("await", "adapter", "stan_zlecenia", "async"):
        assert zakazane not in zrodlo, (
            f"`termin_rozstrzygniecia` przestała być czysta ({zakazane!r})")


def test_PAPIEROWA_petla_nadal_wola_wspolna_funkcje_i_na_tym_konczy():
    """Kanarek strukturalny: `loop.rozstrzygnij_plany` nie dostało własnego
    wyjątku od czekania. Sprawdzamy ŹRÓDŁO, bo zachowanie tej pętli wymaga
    świec w bazie, a wtedy test mierzyłby atrapę świec, nie regułę."""
    import inspect

    from services.engine.loop import rozstrzygnij_plany

    zrodlo = inspect.getsource(rozstrzygnij_plany)
    assert "if czekamy:" in zrodlo and "st[\"czekaja\"] += 1" in zrodlo
    assert "gielda_juz_rozstrzygnela" not in zrodlo, (
        "ścieżka giełdowa weszła do rachunku papierowego")


# ============================ sciezka gieldowa: fakt dokonany nie czeka

@pytest.mark.parametrize("status", STANY_ROZSTRZYGNIETE)
def test_stan_KONCOWY_na_gieldzie_konczy_czekanie(status):
    """Sedno Z8. Giełda zamknęła sprawę — nie ma na co czekać.

    Dotyczy to tak samo `filled`, jak `cancelled` i `rejected`: w każdym z tych
    stanów kolejny cykl przeczyta dokładnie to samo, a plan pozostający
    `pending` trzyma dziennik w stanie znanym-nieprawdziwym.
    """
    wynik, g = _pyta(_stan(status))
    assert wynik is True
    assert g.pytania == ["tai-p36"], "pytamy po NASZYM identyfikatorze zlecenia"


def test_kanarek_wypelnienie_CZESCIOWE_NADAL_CZEKA():
    """Kanarek na rozluźnienie reguły egzekucji zamiast chwili zapisu.

    Zlecenie częściowo wypełnione wciąż leży w arkuszu i może się dopełnić do
    końca świecy. Wcześniejsze rozstrzygnięcie przyjęłoby to, co weszło,
    i ANULOWAŁO resztę (`egzekucja.co_z_wypelnieniem`) — czyli systematycznie
    zmniejszało pozycję względem planu. Zmieniamy chwilę zapisu FAKTU
    DOKONANEGO, nie regułę egzekucji.
    """
    assert "partial" not in STANY_ROZSTRZYGNIETE
    wynik, _ = _pyta(_stan("partial", wypelniono=D("0.1")))
    assert wynik is False


def test_kanarek_zlecenie_NIEWYPELNIONE_nadal_czeka_na_swoja_swiece():
    """Bez tego kanarka „nie czekamy" przeszłoby także dla zmiany, która nie
    czeka NIGDY — a zlecenie z limitem musi mieć szansę się wypełnić."""
    wynik, _ = _pyta(_stan("new", wypelniono=D("0")))
    assert wynik is False


def test_gielda_ktora_NIE_ZNA_zlecenia_nie_konczy_czekania():
    """`None` znaczy „giełda go nie zna". To nie jest rozstrzygnięcie — i tak
    samo czyta to `rozstrzygnij_plan`, który dopiero na swoim terminie wygasza
    taki plan."""
    wynik, _ = _pyta(None)
    assert wynik is False


def test_BLAD_ODCZYTU_zostawia_zachowanie_dotychczasowe():
    """Niewiedza nie może rozstrzygać planu. Gdy giełdy nie da się zapytać,
    czekamy tak jak dotąd — a nie przyjmujemy, że skoro nie wiemy, to pewnie
    weszło (ani odwrotnie)."""
    wynik, _ = _pyta(blad=ExchangeError("timeout", code="500", retryable=True))
    assert wynik is False


def test_awaria_WYMAGAJACA_CZLOWIEKA_idzie_w_gore_natychmiast():
    """Wyjątek od reguły wyżej. Zignorowany filtr po `orderLinkId` znaczy, że
    odczyt nie mówi nic o NASZYM zleceniu — żaden restart tego nie naprawi.
    Wyciszenie tego na całą świecę byłoby dokładnie tą ciszą, przed którą
    ostrzega `stan_zlecenia`."""
    with pytest.raises(ExchangeError):
        _pyta(blad=ExchangeError("cudze orderLinkId", code="tozsamosc-zlecenia",
                                 wymaga_czlowieka=True))


def test_okno_wady_to_PELNA_SWIECA_przy_H4H():
    """Miara wady w liczbach, a nie w przymiotnikach.

    Plan czeka do `min(koniec świecy następnej, valid_until)`. Dla H4H
    (`bar_minutes` = 240) wypełnienie tuż po otwarciu świecy rozstrzygającej
    leżało w dzienniku niezapisane przez niemal pełne 4 h; na produkcji
    2026-09-23 było to 2 h 37 min, bo wypełnienie przyszło o 13:26.
    """
    assert H4H.bar_minutes == 240
    rozstrzygajaca = BAR + timedelta(minutes=H4H.bar_minutes)      # 12:00
    assert (PLAN["valid_until"] - rozstrzygajaca
            == timedelta(minutes=H4H.bar_minutes))
    assert PLAN["valid_until"] - WYPELNIONE_O == timedelta(
        hours=2, minutes=33, seconds=21)
