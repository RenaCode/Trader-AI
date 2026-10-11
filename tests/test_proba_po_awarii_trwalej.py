"""Odciecie po awarii TRWALEJ zdejmuje udana proba, nie tylko uplyw 6 h (audyt 2026-10-10).

Brak kredytu odcinal dostawce na `ZWARCIE_TRWALE_S` = 6 h. Brak kredytu konczy
czlowiek, ktory doladowuje konto - i po doladowaniu czekal do szesciu godzin
albo restartowal poda. Teraz w trakcie odciecia przechodzi pojedyncza proba co
`PROBA_PO_S` (rosnaco do `PROBA_MAX_S`), a udana zdejmuje odciecie od razu.
"""
import asyncio

import httpx
import pytest

from services.ingest import llm
from services.ingest.llm import (
    BrakDostawcy, Dostawca, DostawcaAnthropic, DostawcaMieszany, Wyczerpany,
)

ODMOWA_KREDYTOWA = {
    "type": "error",
    "error": {"type": "invalid_request_error",
              "message": "Your credit balance is too low to access the Anthropic API."},
}


def _blad_kredytu():
    import anthropic
    odp = httpx.Response(400, json=ODMOWA_KREDYTOWA,
                         request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    return anthropic.BadRequestError(ODMOWA_KREDYTOWA["error"]["message"],
                                     response=odp, body=ODMOWA_KREDYTOWA)


class _Odp:
    class usage:
        input_tokens, output_tokens, cache_read_input_tokens = 10, 5, 0

    class _Blok:
        type, text = "text", '{"ok": true}'

    stop_reason, content, model = "end_turn", [_Blok()], "claude-haiku-4-5"


def _anthropic(skrypt: list):
    """Dostawca, ktorego klient kolejno: podnosi blad kredytu (`"kredyt"`) albo
    oddaje odpowiedz (`"ok"`). Ostatni element powtarza sie bez konca."""

    class _Klient:
        def __init__(self):
            self.wywolan = 0
            self.messages = self

        async def create(self, **kw):
            krok = skrypt[min(self.wywolan, len(skrypt) - 1)]
            self.wywolan += 1
            if krok == "kredyt":
                raise _blad_kredytu()
            return _Odp()

    d = DostawcaAnthropic("klucz-testowy")
    d._c = _Klient()
    return d, d._c


@pytest.fixture
def zegar(monkeypatch):
    stan = {"t": 1000.0}
    monkeypatch.setattr(llm.time, "monotonic", lambda: stan["t"])
    return stan


def _pytaj(d, model="claude-haiku-4-5"):
    return asyncio.run(d.zapytaj(model=model, system="s", user="u",
                                 schema={"type": "object"}, max_tokens=16))


def test_DOLADOWANE_KONTO_wraca_po_probie_a_nie_po_szesciu_godzinach(zegar):
    d, klient = _anthropic(["kredyt", "ok"])
    with pytest.raises(Wyczerpany):
        _pytaj(d)
    assert not d.czy_ma_czym_pytac()

    zegar["t"] += d.PROBA_PO_S + 1
    assert d.czy_ma_czym_pytac(), "po PROBA_PO_S dostawca ma dostac probe"
    _pytaj(d)                                   # proba - konto juz doladowane
    assert klient.wywolan == 2

    # Odciecie zdjete: kolejne zapytania ida normalnie, bez czekania do 6 h.
    _pytaj(d)
    _pytaj(d)
    assert klient.wywolan == 4
    assert d.czy_ma_czym_pytac()


def test_przed_terminem_proby_nie_ma_zadnego_wywolania(zegar):
    d, klient = _anthropic(["kredyt"])
    with pytest.raises(Wyczerpany):
        _pytaj(d)
    zegar["t"] += d.PROBA_PO_S - 5
    with pytest.raises(BrakDostawcy):
        _pytaj(d)
    assert klient.wywolan == 1


def test_NIEUDANA_proba_to_jedno_wywolanie_i_dluzszy_odstep(zegar):
    d, klient = _anthropic(["kredyt"])
    with pytest.raises(Wyczerpany):
        _pytaj(d)
    zegar["t"] += d.PROBA_PO_S + 1
    with pytest.raises(Wyczerpany):
        _pytaj(d)                               # proba - nadal bez srodkow
    assert klient.wywolan == 2
    # Nastepny artykul w tej samej petli NIE dostaje drugiej proby.
    with pytest.raises(BrakDostawcy):
        _pytaj(d)
    zegar["t"] += d.PROBA_PO_S + 1              # pierwszy odstep juz nie wystarcza
    with pytest.raises(BrakDostawcy):
        _pytaj(d)
    assert klient.wywolan == 2
    zegar["t"] += d.PROBA_PO_S                  # razem 2 x PROBA_PO_S
    with pytest.raises(Wyczerpany):
        _pytaj(d)
    assert klient.wywolan == 3


def test_odstep_prob_ma_sufit(zegar):
    d, klient = _anthropic(["kredyt"])
    with pytest.raises(Wyczerpany):
        _pytaj(d)
    for _ in range(10):
        zegar["t"] += d.PROBA_MAX_S + 1
        with pytest.raises(Wyczerpany):
            _pytaj(d)
    assert klient.wywolan == 11, "po kazdym PROBA_MAX_S ma isc dokladnie jedna proba"


def test_limit_tempa_NIE_dostaje_prob(zegar):
    """Limit tempa i dobowy mijaja same - proba tylko zjadlaby prog dostawcy."""
    d = Dostawca()
    d.nazwa = "x"
    d.odetnij(3600.0, "limit DOBOWY")
    zegar["t"] += d.PROBA_PO_S + 1
    assert not d.czy_ma_czym_pytac()
    with pytest.raises(BrakDostawcy):
        d._sprawdz_dostepnosc()


class _Atrapa(Dostawca):
    def __init__(self, nazwa, triaz, analiza):
        self.nazwa, self.model_triazu, self.model_analizy = nazwa, triaz, analiza
        self.pytania = []

    async def zapytaj(self, *, model, **kw):
        self.pytania.append(model)
        return {"ok": True}, {"input": 0, "output": 0, "cache_read": 0}


def test_MIESZANKA_po_doladowaniu_wraca_z_analiza_do_anthropica(zegar):
    """W mieszance analiza po odcieciu Anthropica schodzi na Gemini - i bez
    proby zostawala tam szesc godzin po doladowaniu konta."""
    a, klient = _anthropic(["kredyt", "ok"])
    a.model_triazu, a.model_analizy = "claude-haiku-4-5", "claude-opus-5"
    g = _Atrapa("gemini", "flash-lite", "flash")
    m = DostawcaMieszany(triaz=g, analiza=a)

    def analiza():
        return asyncio.run(m.zapytaj(model="claude-opus-5", system="s", user="u",
                                     schema={"type": "object"}, max_tokens=16))

    analiza()                                   # kredyt -> zejscie na gemini
    assert g.pytania == ["flash"] and klient.wywolan == 1
    analiza()                                   # odciety -> gemini bez pytania
    assert klient.wywolan == 1

    zegar["t"] += a.PROBA_PO_S + 1
    _, usage = analiza()                        # proba u Anthropica - udana
    assert klient.wywolan == 2
    assert usage["model_uzyty"] == "claude-haiku-4-5"   # model z odpowiedzi atrapy
    analiza()
    assert klient.wywolan == 3, "po udanej probie analiza zostaje u Anthropica"
    assert g.pytania == ["flash", "flash"]


def test_przejecie_stanu_przenosi_harmonogram_prob(zegar):
    """Dostawca jest przebudowywany co cykl przy tych samych kluczach - bez
    przeniesienia proba przychodzilaby albo nigdy, albo co cykl."""
    a1, _ = _anthropic(["kredyt"])
    a1.model_triazu, a1.model_analizy = "claude-haiku-4-5", "claude-opus-5"
    m1 = DostawcaMieszany(triaz=_Atrapa("gemini", "flash-lite", "flash"), analiza=a1)
    asyncio.run(m1.zapytaj(model="claude-opus-5", system="s", user="u",
                           schema={"type": "object"}, max_tokens=16))

    a2, _ = _anthropic(["ok"])
    a2.model_triazu, a2.model_analizy = "claude-haiku-4-5", "claude-opus-5"
    m2 = DostawcaMieszany(triaz=_Atrapa("gemini", "flash-lite", "flash"), analiza=a2)
    m2.przejmij_stan(m1)
    assert not a2.czy_ma_czym_pytac()
    zegar["t"] += a2.PROBA_PO_S + 1
    assert a2.czy_ma_czym_pytac()
