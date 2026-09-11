"""Testy warstwy dostawcy modelu jezykowego.

Nie wolaja zadnego API - sprawdzaja to, co przy wywolaniu prawdziwego API
byloby najtrudniejsze do zauwazenia: ze koszt jest policzony z WSZYSTKICH
platnych tokenow, ze schemat wychodzi w postaci, ktora dostawca przyjmie,
i ze odpowiedz uciecie na limicie nie udaje wyniku.
"""
import asyncio
import json

import httpx
import pytest

from services.ingest.llm import (
    BladOdpowiedzi, BrakDostawcy, Dostawca, DostawcaGemini, Odmowa, Przeciazony,
    Wyczerpany, _bez_additional_properties, limit_dobowy_usd,
)
from services.ingest.news_worker import ANALYSIS_SCHEMA, TRIAGE_SCHEMA


def gemini(odpowiedz: dict, status: int = 200) -> DostawcaGemini:
    """Dostawca z podstawionym transportem - bez sieci."""
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(status, json=odpowiedz)))
    return DostawcaGemini("klucz-testowy", http=http)


def uruchom(d: DostawcaGemini):
    """asyncio.run zamiast markera pytest-asyncio: kilka testow nie jest powodem,
    zeby dokladac zaleznosc do obrazu."""
    return asyncio.run(d.zapytaj(model="m", system="s", user="u",
                                 schema=TRIAGE_SCHEMA, max_tokens=100))


def odp(tekst: str, finish: str = "STOP", usage: dict | None = None) -> dict:
    return {
        "candidates": [{"finishReason": finish,
                        "content": {"parts": [{"text": tekst}]}}],
        "usageMetadata": usage or {"promptTokenCount": 100, "candidatesTokenCount": 50},
    }


# --------------------------------------------------------------- schemat

def test_schemat_traci_additionalProperties_ale_nic_wiecej():
    """`responseSchema` Gemini to podzbior OpenAPI - `additionalProperties`
    konczy sie bledem 400. Reszta schematu MUSI przejsc bez zmian, bo to ona
    wymusza pola, ktorych oczekuje zapis do bazy."""
    czysty = _bez_additional_properties(ANALYSIS_SCHEMA)
    assert "additionalProperties" not in czysty
    assert czysty["required"] == ANALYSIS_SCHEMA["required"]
    assert set(czysty["properties"]) == set(ANALYSIS_SCHEMA["properties"])
    assert czysty["properties"]["direction"]["enum"] == ["bullish", "bearish", "neutral"]


def test_czyszczenie_schematu_siega_w_glab():
    zagniezdzony = {"type": "object", "additionalProperties": False,
                    "properties": {"a": {"type": "object", "additionalProperties": False,
                                         "properties": {"b": {"type": "string"}}}},
                    "anyOf": [{"additionalProperties": True}]}
    czysty = _bez_additional_properties(zagniezdzony)
    assert json.dumps(czysty).count("additionalProperties") == 0
    assert czysty["properties"]["a"]["properties"]["b"]["type"] == "string"


def test_czyszczenie_nie_rusza_oryginalu():
    """Gdyby ciecie szlo w miejscu, sciezka Anthropica dostalaby schemat
    okrojony pod Gemini - i to bez zadnego bledu, po prostu slabiej wymuszony."""
    przed = json.dumps(TRIAGE_SCHEMA, sort_keys=True)
    _bez_additional_properties(TRIAGE_SCHEMA)
    assert json.dumps(TRIAGE_SCHEMA, sort_keys=True) == przed


# --------------------------------------------------------------- koszt

def test_tokeny_rozumowania_wchodza_do_kosztu():
    """KANAREK NA NAJDROZSZY BLAD TEJ WARSTWY.

    Modele 3.x placa za tokeny rozumowania stawka wyjscia, a raportuja je
    w OSOBNYM polu. Zmierzone na gemini-3.5-flash: 732 tokeny myslenia przy
    121 tokenach odpowiedzi. Liczenie samego `candidatesTokenCount` zanizaloby
    koszt siedmiokrotnie - a limit dobowy jest jedynym bezpiecznikiem przed
    petla, ktora analizuje w kolko to samo.
    """
    u = DostawcaGemini._uzycie({"promptTokenCount": 458,
                                "candidatesTokenCount": 121,
                                "thoughtsTokenCount": 732})
    assert u["output"] == 121 + 732, "tokeny rozumowania wypadly z rachunku"
    assert u["input"] == 458


def test_brak_pola_rozumowania_nie_wywraca_rachunku():
    u = DostawcaGemini._uzycie({"promptTokenCount": 56, "candidatesTokenCount": 60})
    assert u == {"input": 56, "output": 60, "cache_read": 0}


def test_nieznany_model_wyceniamy_najdrozej_ze_znanych():
    """Pomylka w te strone zatrzymuje potok na limicie. Pomylka w druga
    pozwala mu wydac wielokrotnosc limitu, zanim ktokolwiek to zauwazy."""
    d = DostawcaGemini("k")
    u = {"input": 1_000_000, "output": 1_000_000, "cache_read": 0}
    nieznany = d.koszt("gemini-99-turbo", u)
    assert nieznany >= max(d.koszt(m, u) for m in d.CENNIK)


def test_odczyt_z_cache_jest_tanszy_od_swiezego_wejscia():
    d = DostawcaGemini("k")
    swieze = d.koszt("gemini-3.5-flash", {"input": 100_000, "output": 0, "cache_read": 0})
    z_cache = d.koszt("gemini-3.5-flash", {"input": 100_000, "output": 0, "cache_read": 100_000})
    assert 0 < z_cache < swieze


def test_limit_dobowy_czyta_stara_nazwe_zmiennej(monkeypatch):
    """Na klastrze stoi juz ANTHROPIC_DAILY_USD. Gdyby nowa nazwa go nie
    honorowala, limit po cichu wrocilby do wartosci domyslnej."""
    monkeypatch.delenv("LLM_DAILY_USD", raising=False)
    monkeypatch.setenv("ANTHROPIC_DAILY_USD", "1.25")
    assert limit_dobowy_usd() == 1.25
    monkeypatch.setenv("LLM_DAILY_USD", "3.5")
    assert limit_dobowy_usd() == 3.5, "nowa nazwa ma miec pierwszenstwo"


# --------------------------------------------------------------- odpowiedzi

def test_poprawna_odpowiedz_wraca_jako_slownik():
    d = gemini(odp('{"worth_analyzing": true, "reason": "decyzja regulatora"}'))
    dane, usage = uruchom(d)
    assert dane["worth_analyzing"] is True
    assert usage["input"] == 100


def test_odpowiedz_ucieta_na_limicie_NIE_jest_wynikiem():
    """Bez tego warunku `json.loads` rzucalby blad skladni, a w logu widnialoby
    'cykl przerwany: JSONDecodeError' - komunikat, ktory nie mowi, co sie stalo,
    i kieruje szukanie w zla strone."""
    d = gemini(odp('{"worth_analyz', finish="MAX_TOKENS"))
    with pytest.raises(RuntimeError, match="ucięta na limicie"):
        uruchom(d)


def test_odmowa_jest_odpowiedzia_a_nie_awaria():
    d = gemini(odp("", finish="SAFETY"))
    with pytest.raises(Odmowa):
        uruchom(d)


def test_odmowa_niesie_zuzycie_tokenow():
    """Odrzucone zapytanie tez kosztuje. Gdyby zuzycie przepadalo, licznik
    wydatkow rozjechalby sie z rachunkiem u dostawcy dokladnie wtedy, gdy
    cos idzie nie tak - czyli gdy zapytan jest najwiecej."""
    d = gemini({"promptFeedback": {"blockReason": "SAFETY"},
                "usageMetadata": {"promptTokenCount": 77, "candidatesTokenCount": 0}})
    with pytest.raises(Odmowa) as e:
        uruchom(d)
    assert e.value.usage["input"] == 77


def test_pusta_odpowiedz_nie_udaje_wyniku():
    d = gemini(odp("   "))
    with pytest.raises(RuntimeError, match="pusta odpowiedź"):
        uruchom(d)


# ------------------------------------------------- koszt wywolan nieudanych

def test_blad_odpowiedzi_niesie_zuzycie_do_zapisu():
    """Wywolanie ucięte na limicie kosztuje tyle samo, co udane - i wraca
    przy kazdym cyklu nadrabiania zaleglosci, bo artykul dalej nie ma analizy.
    Bez zuzycia w wyjatku limit dobowy nie ma czego policzyc."""
    d = gemini(odp('{"worth', finish="MAX_TOKENS",
                   usage={"promptTokenCount": 460, "candidatesTokenCount": 4000}))
    with pytest.raises(BladOdpowiedzi) as e:
        uruchom(d)
    assert e.value.usage["input"] == 460
    assert e.value.usage["output"] == 4000
    assert d.koszt("gemini-3.5-flash", e.value.usage) > 0


def test_blad_bez_zuzycia_ma_zerowy_licznik_zamiast_None():
    """Zerowy slownik zamiast None: strona wolajaca liczy koszt bezwarunkowo,
    a `None['input']` byloby awaria w obsludze awarii."""
    b = BladOdpowiedzi("sieć padła")
    assert b.usage == {"input": 0, "output": 0, "cache_read": 0}


def test_odmowa_jest_bledem_odpowiedzi():
    """Wspolna nadklasa, zeby strona wolajaca miala jeden warunek na
    'wywolanie kosztowalo, a nic nie dalo'."""
    assert issubclass(Odmowa, BladOdpowiedzi)


# ------------------------------------------------------ limit tempa i ponawianie
#
# Zmierzone na produkcji 2026-09-07, pierwszy cykl po wpieciu klucza Gemini:
# jedna analiza przeszla, siedem kolejnych zapytan dostalo 429. W logu widnialo
# "przeanalizowanych 0, bledow 7" i proces szedl dalej - potok wygladal na
# dzialajacy i milczal.

def gemini_sekwencja(odpowiedzi: list, odstep: float = 0.0) -> tuple:
    """Dostawca oddajacy kolejne odpowiedzi z listy. Zwraca (dostawca, licznik)."""
    licznik = {"n": 0}

    def handler(req):
        i = min(licznik["n"], len(odpowiedzi) - 1)
        licznik["n"] += 1
        status, tresc, naglowki = odpowiedzi[i]
        return httpx.Response(status, json=tresc, headers=naglowki or {})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    d = DostawcaGemini("klucz-testowy", http=http)
    d.ODSTEP_S = odstep          # bez czekania w tescie
    return d, licznik


def bez_czekania(monkeypatch):
    """Usuwa realne spanie, zeby test odporny na 429 nie trwal minuty."""
    async def natychmiast(_s):
        return None
    monkeypatch.setattr(asyncio, "sleep", natychmiast)


DOBRA = (200, {"candidates": [{"finishReason": "STOP",
                               "content": {"parts": [{"text": '{"worth_analyzing": true, "reason": "x"}'}]}}],
               "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}, None)
LIMIT = (429, {"error": {"code": 429, "message": "Too Many Requests"}}, None)


def test_zapytanie_po_limicie_jest_PONAWIANE(monkeypatch):
    """Kanarek na blad, ktory wlasnie zdarzyl sie na produkcji.

    Bez ponawiania pierwsze 429 konczylo caly artykul i szlo dalej - a limit
    tempa jest stanem PRZEJSCIOWYM, nie odmowa.
    """
    bez_czekania(monkeypatch)
    d, licznik = gemini_sekwencja([LIMIT, LIMIT, DOBRA])
    dane, _ = asyncio.run(d.zapytaj(model="m", system="s", user="u",
                                    schema=TRIAGE_SCHEMA, max_tokens=100))
    assert dane["worth_analyzing"] is True
    assert licznik["n"] == 3, f"powinny byc trzy proby, bylo {licznik['n']}"


def test_wyczerpane_proby_mowia_ZE_TO_LIMIT_a_nie_zly_klucz(monkeypatch):
    """Sam kod 429 w logu wyglada jak blad konfiguracji i kieruje szukanie
    w zla strone. Komunikat musi nazwac rzecz po imieniu."""
    bez_czekania(monkeypatch)
    d, licznik = gemini_sekwencja([LIMIT])
    with pytest.raises(RuntimeError, match="limit tempa albo dobowy limit klucza"):
        asyncio.run(d.zapytaj(model="m", system="s", user="u",
                              schema=TRIAGE_SCHEMA, max_tokens=100))
    assert licznik["n"] == d.PROBY, "ma probowac dokladnie PROBY razy, nie w nieskonczonosc"


def test_blad_klienta_NIE_jest_ponawiany(monkeypatch):
    """Kanarek do poprzednich. Zly klucz (401) albo zly schemat (400) to nie
    stan przejsciowy - ponawianie ich tylko opoznia komunikat o prawdziwej
    przyczynie i zuzywa limit.

    401 to przy tym awaria TRWALA: sam nie minie, wiec ma odciac dostawce,
    a nie wracac przy kazdym artykule (patrz `Wyczerpany`).
    """
    bez_czekania(monkeypatch)
    d, licznik = gemini_sekwencja([(401, {"error": {"message": "bad key"}}, None)])
    with pytest.raises(Wyczerpany):
        asyncio.run(d.zapytaj(model="m", system="s", user="u",
                              schema=TRIAGE_SCHEMA, max_tokens=100))
    assert licznik["n"] == 1, f"401 ma isc bez ponawiania, prob bylo {licznik['n']}"


def test_ZLY_SCHEMAT_zostaje_glosny_a_nie_udaje_awarii_dostawcy(monkeypatch):
    """Druga granica do poprzedniego testu. 400 dostajemy ZAROWNO za martwy
    klucz, JAK I za nasz wlasny blad w schemacie (`additionalProperties`).
    Gdyby oba szly jako awaria dostawcy, blad w kodzie zamieniłby się w „dostawca
    chwilowo niedostepny" i czekalibysmy na cud zamiast poprawic schemat."""
    bez_czekania(monkeypatch)
    d, licznik = gemini_sekwencja([(400, {"error": {"message": "Invalid JSON payload "
                                                   "received. Unknown name "
                                                   "\"additionalProperties\""}}, None)])
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(d.zapytaj(model="m", system="s", user="u",
                              schema=TRIAGE_SCHEMA, max_tokens=100))
    assert licznik["n"] == 1
    assert d.czy_ma_czym_pytac(), "nasz blad nie moze odcinac dostawcy"


def test_retry_after_od_dostawcy_bije_nasze_zgadywanie():
    """On wie, kiedy okno limitu sie odnowi, my nie."""
    r = httpx.Response(429, headers={"Retry-After": "12"})
    assert DostawcaGemini._ile_czekac(r, proba=1) == 12.0
    # Bez naglowka - rosnace odstepy, z sufitem.
    goly = httpx.Response(429)
    kroki = [DostawcaGemini._ile_czekac(goly, p) for p in (1, 2, 3, 9)]
    assert kroki[0] < kroki[1] < kroki[2], f"odstepy maja rosnac, sa {kroki}"
    assert kroki[3] <= 60.0, "bez sufitu jedno zapytanie blokowaloby cykl na godziny"


def test_przepustnica_rozklada_zapytania_w_czasie(monkeypatch):
    """Ponawianie ratuje pojedyncze zapytanie; przepustnica sprawia, ze limit
    nie jest przekraczany w ogole. Bez niej cykl z 20 zaleglosciami wystrzeliwuje
    20 zapytan w sekunde i pierwsze kilkanascie wraca z 429."""
    spane = []

    async def zapisz(s):
        spane.append(s)

    monkeypatch.setattr(asyncio, "sleep", zapisz)
    d, _ = gemini_sekwencja([DOBRA], odstep=4.0)
    asyncio.run(d.zapytaj(model="m", system="s", user="u",
                          schema=TRIAGE_SCHEMA, max_tokens=100))
    asyncio.run(d.zapytaj(model="m", system="s", user="u",
                          schema=TRIAGE_SCHEMA, max_tokens=100))
    assert spane, "drugie zapytanie ma odczekac, pierwsze nie"
    assert 0 < spane[-1] <= 4.0


# ------------------------------------------------------------- mieszanka

class _Fałszywy(Dostawca):
    """Dostawca-atrapa: zapisuje, o co go pytano, i nic nie wysyła."""

    def __init__(self, nazwa, triaz, analiza, cena):
        self.nazwa, self.model_triazu, self.model_analizy = nazwa, triaz, analiza
        self.CENNIK = {triaz: cena, analiza: cena}
        self.CENNIK_DOMYSLNY = cena
        self.pytania, self.zamkniety = [], False

    async def zapytaj(self, *, model, system, user, schema, max_tokens):
        self.pytania.append(model)
        return {"ok": True}, {"input": 0, "output": 0, "cache_read": 0}

    def koszt(self, model, usage):
        return 42.0 if self.nazwa == "drogi" else 1.0

    async def zamknij(self):
        self.zamkniety = True


def _mieszanka():
    from services.ingest.llm import DostawcaMieszany
    tani = _Fałszywy("tani", "flash-lite", "flash", (0.3, 2.5))
    drogi = _Fałszywy("drogi", "haiku", "opus", (5.0, 25.0))
    return DostawcaMieszany(triaz=tani, analiza=drogi), tani, drogi


def test_triaz_idzie_do_taniego_a_analiza_do_mocnego():
    """Triaż to 94% wywołań i tam decyduje cena; analiza kosztuje u obu tyle
    samo, więc tam decyduje jakość. Odwrotny przydział oszczędziłby grosze
    i wstawił uskok w środek cechy `news_sent`."""
    import asyncio

    m, tani, drogi = _mieszanka()
    asyncio.run(m.zapytaj(model=m.model_triazu, system="", user="",
                          schema={}, max_tokens=1))
    asyncio.run(m.zapytaj(model=m.model_analizy, system="", user="",
                          schema={}, max_tokens=1))
    assert tani.pytania == ["flash-lite"]
    assert drogi.pytania == ["opus"]


def test_koszt_liczy_dostawca_KTORY_WYKONAL_wywolanie():
    """Wspólny cennik dawałby liczbę wyglądającą poprawnie i błędną —
    a limit dobowy opiera się właśnie na niej."""
    m, _, _ = _mieszanka()
    u = {"input": 0, "output": 0, "cache_read": 0}
    assert m.koszt(m.model_triazu, u) == 1.0
    assert m.koszt(m.model_analizy, u) == 42.0


def test_model_spoza_mieszanki_nie_trafia_nigdzie_po_cichu():
    """Milczący wybór „pierwszy z brzegu" trafiałby raz tu, raz tam — i przy
    liczeniu kosztu użyłby cudzego cennika."""
    m, _, _ = _mieszanka()
    with pytest.raises(ValueError):
        m.koszt("model-ktorego-nie-ma", {"input": 0, "output": 0, "cache_read": 0})


def test_identyczne_modele_sa_odrzucane_a_nie_rozstrzygane_domyslem():
    from services.ingest.llm import DostawcaMieszany

    ten_sam = _Fałszywy("x", "jeden", "jeden", (1.0, 1.0))
    with pytest.raises(ValueError):
        DostawcaMieszany(triaz=ten_sam, analiza=ten_sam)


def test_zamkniecie_obejmuje_OBU_dostawcow():
    """Niedomknięty klient httpx zostawia otwarte gniazda na każdym cyklu."""
    import asyncio

    m, tani, drogi = _mieszanka()
    asyncio.run(m.zamknij())
    assert tani.zamkniety and drogi.zamkniety


def test_oba_klucze_daja_mieszanke_a_jeden_pojedynczego_dostawce():
    """Wybieranie jednego dostawcy do wszystkiego, gdy dostępni są obaj, byłoby
    gorsze pod każdym względem naraz: drożej niż mieszanka i słabiej."""
    from services.ingest.llm import (
        DostawcaAnthropic, DostawcaGemini, DostawcaMieszany,
    )
    from services.ingest.news_worker import zbuduj_dostawce

    assert zbuduj_dostawce({}) is None
    assert isinstance(zbuduj_dostawce({"gemini": "g"}), DostawcaGemini)
    assert isinstance(zbuduj_dostawce({"anthropic": "a"}), DostawcaAnthropic)
    mieszany = zbuduj_dostawce({"anthropic": "a", "gemini": "g"})
    assert isinstance(mieszany, DostawcaMieszany)
    assert "gemini" in mieszany.model_triazu
    assert "claude" in mieszany.model_analizy


def test_triaz_schodzi_na_zapasowego_przy_LIMICIE():
    """Darmowy klucz Gemini odbił 94% wywołań i cały potok stanął — triaż,
    który padł, zatrzymuje CAŁĄ analizę.

    Przerzucenie triażu jest bezpieczne, bo on WYBIERA artykuły, a nie ocenia
    je: zmiana modelu zmienia próbkę, nie wartość cechy.
    """
    import asyncio

    from services.ingest.llm import DostawcaMieszany, Przeciazony

    class Zajety(_Fałszywy):
        async def zapytaj(self, **kw):
            raise Przeciazony("429")

    tani = Zajety("tani", "flash-lite", "flash", (0.3, 2.5))
    drogi = _Fałszywy("drogi", "haiku", "opus", (5.0, 25.0))
    m = DostawcaMieszany(triaz=tani, analiza=drogi)

    _, usage = asyncio.run(m.zapytaj(model=m.model_triazu, system="", user="",
                                     schema={}, max_tokens=1))
    assert drogi.pytania == ["haiku"]
    assert usage["model_uzyty"] == "haiku"
    # Koszt liczy dostawca zapasowy, bo to on wykonal prace.
    assert m.koszt(m.model_triazu, usage) == 42.0


def test_ANALIZA_nie_ma_zejscia_awaryjnego():
    """Analiza wytwarza wartość cechy `news_sent`. Podmiana modelu w połowie
    doby wstawiłaby w środek szeregu uskok, którego kalibracja wagi nie zakłada.

    Brakująca analiza to tylko brakujący wiersz, który widać. Uskok jest gorszy
    niż dziura.
    """
    import asyncio

    from services.ingest.llm import DostawcaMieszany, Przeciazony

    class ZajetyNaAnalizie(_Fałszywy):
        """Zapisuje, o co go pytano, ZANIM odmówi.

        To jest sedno testu: bez zapisu nie da się odróżnić „nie ponowiono"
        od „ponowiono i też się nie udało" — a pierwsza wersja tego testu
        przechodziła w obu przypadkach.
        """

        async def zapytaj(self, *, model, **kw):
            self.pytania.append(model)
            raise Przeciazony("429")

    drogi = ZajetyNaAnalizie("drogi", "haiku", "opus", (5.0, 25.0))
    m = DostawcaMieszany(triaz=_Fałszywy("tani", "flash-lite", "flash", (0.3, 2.5)),
                         analiza=drogi)
    with pytest.raises(Przeciazony):
        asyncio.run(m.zapytaj(model=m.model_analizy, system="", user="",
                              schema={}, max_tokens=1))

    # ANI RAZU modelem triażu. Ponowienie analizy modelem `haiku` byłoby cichą
    # podmianą: wartość cechy wytworzyłby słabszy model, a dziennik pokazałby
    # analizę jak każdą inną.
    assert drogi.pytania == ["opus"]


def test_zwykly_blad_NIE_uruchamia_dostawcy_zapasowego():
    """Łapanie gołego RuntimeError zamiotłoby każdy błąd w naszym kodzie —
    i zamiast awarii dałoby ciche zejście na droższego dostawcę."""
    import asyncio

    from services.ingest.llm import DostawcaMieszany

    class Zepsuty(_Fałszywy):
        async def zapytaj(self, **kw):
            raise RuntimeError("blad w naszym kodzie")

    tani = Zepsuty("tani", "flash-lite", "flash", (0.3, 2.5))
    drogi = _Fałszywy("drogi", "haiku", "opus", (5.0, 25.0))
    m = DostawcaMieszany(triaz=tani, analiza=drogi)
    with pytest.raises(RuntimeError):
        asyncio.run(m.zapytaj(model=m.model_triazu, system="", user="",
                              schema={}, max_tokens=1))
    assert drogi.pytania == []


# ============================== bezpiecznik na wyczerpany klucz triazu

class _Odmawiajacy(Dostawca):
    """Dostawca, ktory odrzuca KAZDE zapytanie - jak wyczerpany klucz."""
    nazwa = "odmawiajacy"
    model_analizy = "duzy"
    model_triazu = "maly"

    def __init__(self):
        self.wywolan = 0

    async def zapytaj(self, **kw):
        from services.ingest.llm import Przeciazony
        self.wywolan += 1
        raise Przeciazony("HTTP 429")

    def koszt(self, model, usage):
        return 0.0

    async def zamknij(self):
        return None


class _Dzialajacy(Dostawca):
    nazwa = "dzialajacy"
    model_analizy = "opus"
    model_triazu = "haiku"

    def __init__(self):
        self.wywolan = 0

    async def zapytaj(self, **kw):
        self.wywolan += 1
        return {"ok": True}, {"input": 1, "output": 1, "cache_read": 0}

    def koszt(self, model, usage):
        return 0.0

    async def zamknij(self):
        return None


def _mieszany():
    from services.ingest.llm import DostawcaMieszany
    triaz, analiza = _Odmawiajacy(), _Dzialajacy()
    return DostawcaMieszany(triaz, analiza), triaz, analiza


def _pytaj(ai, model):
    return asyncio.run(ai.zapytaj(model=model, system="s", user="u",
                                  schema={"type": "object"}, max_tokens=16))


def test_wyczerpany_dostawca_triazu_PRZESTAJE_BYC_PYTANY():
    """Zejście awaryjne działało, ale włączało się DOPIERO po pięciu
    ponowieniach — około 80 sekund na artykuł. Przy kluczu wyczerpanym
    całkowicie każde zapytanie kończy się 429 w 0,2 s, więc te 80 sekund to
    czysta kara, płacona od nowa przy KAŻDYM artykule.

    Zmierzone na produkcji 2026-09-08: dwie decyzje triażu na pięć minut, cykl
    nie kończył się nigdy, puls milczał, a czujka żywotności meldowała martwego
    workera — który przez cały czas pracował.
    """
    ai, triaz, analiza = _mieszany()
    for _ in range(5):
        _pytaj(ai, ai.model_triazu)
    assert triaz.wywolan == ai.PROG_ZWARCIA, (
        f"po {ai.PROG_ZWARCIA} odmowach z rzędu dostawca ma nie być pytany, "
        f"a dostał {triaz.wywolan} zapytań")
    assert analiza.wywolan == 5, "wszystkie zapytania mają trafić do zapasowego"


def test_POJEDYNCZA_odmowa_nie_zmienia_modelu_triazu():
    """Jedno 429 to zwykły limit tempa, który załatwiają ponowienia. Zwarcie
    przy pierwszej odmowie zmieniałoby model — czyli próbkę artykułów —
    z powodu, który mija sam."""
    ai, triaz, _ = _mieszany()
    _pytaj(ai, ai.model_triazu)
    assert not ai._zwarcie_otwarte(), "jedna odmowa nie może odciąć dostawcy"


def test_zwarcie_jest_CZASOWE_a_nie_trwale():
    """Limity dostawców są okresowe. Trwałe odcięcie zamieniłoby chwilowy limit
    w trwałą zmianę modelu triażu — cichą zmianę próbki artykułów."""
    ai, triaz, _ = _mieszany()
    for _ in range(ai.PROG_ZWARCIA):
        _pytaj(ai, ai.model_triazu)
    assert ai._zwarcie_otwarte()
    ai._triaz._niedostepny_do = 0.0           # symulacja uplywu ZWARCIE_S
    _pytaj(ai, ai.model_triazu)
    assert triaz.wywolan == ai.PROG_ZWARCIA + 1, (
        "po odczekaniu dostawca musi dostać kolejną szansę")


def test_UDANE_zapytanie_zeruje_licznik_odmow():
    """Bez zerowania odmowy rozsiane po całej dobie sumowałyby się i w końcu
    odcięłyby dostawcę, który działa poprawnie."""
    from services.ingest.llm import Przeciazony

    ai, triaz, _ = _mieszany()
    _pytaj(ai, ai.model_triazu)
    assert ai._triaz._odmowy_pod_rzad == 1

    async def raz_dobrze(**kw):
        return {"ok": True}, {"input": 1, "output": 1, "cache_read": 0}
    triaz.zapytaj = raz_dobrze
    _pytaj(ai, ai.model_triazu)
    assert ai._triaz._odmowy_pod_rzad == 0


def test_ANALIZA_nadal_nie_ma_zejscia_awaryjnego():
    """Analiza WYTWARZA wartość cechy `news_sent`. Podmiana modelu w połowie
    doby wstawiłaby w środek szeregu uskok, którego kalibracja wagi nie zakłada
    — a brakująca analiza to tylko brakujący wiersz, który widać. Bezpiecznik
    nie może tego przypadkiem poluzować."""
    from services.ingest.llm import Przeciazony

    ai, _, analiza = _mieszany()
    analiza.zapytaj = _Odmawiajacy().zapytaj
    with pytest.raises(Przeciazony):
        _pytaj(ai, ai.model_analizy)


# =========================== limit DOBOWY to nie to samo co limit TEMPA

# Odpowiedz zapisana z produkcji 2026-09-08 (skrocona). Kluczowe jest `quotaId`:
# Google rozroznia limit dobowy od limitu tempa DOPIERO w tresci, bo kod HTTP
# obu jest ten sam.
ODPOWIEDZ_DOBOWA = {
    "error": {
        "code": 429,
        "message": ("You exceeded your current quota. Quota exceeded for metric: "
                    "generate_content_free_tier_requests, limit: 500, "
                    "model: gemini-3.5-flash-lite. Please retry in 30.9s."),
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{
                 "quotaMetric": "generativelanguage.googleapis.com/"
                                "generate_content_free_tier_requests",
                 "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                 "quotaValue": "500"}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo",
             "retryDelay": "30s"},
        ],
    }
}

ODPOWIEDZ_TEMPO = {
    "error": {"code": 429, "message": "Too many requests", "status": "RESOURCE_EXHAUSTED",
              "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                           "violations": [{"quotaId":
                                           "GenerateRequestsPerMinutePerProject"}]}]}
}


def _odpowiedz(tresc):
    return httpx.Response(429, json=tresc,
                          request=httpx.Request("POST", "https://x/y"))


def test_limit_DOBOWY_jest_odrozniany_od_limitu_TEMPA():
    """Oba maja kod 429 i tylko tresc je rozroznia. Skutek jest praktyczny:
    limit tempa mija w sekundy i ponawianie ma sens, dobowy nie minie do
    nastepnej doby i kazde ponowienie to strata czasu."""
    assert DostawcaGemini.limit_dobowy(_odpowiedz(ODPOWIEDZ_DOBOWA)) is True
    assert DostawcaGemini.limit_dobowy(_odpowiedz(ODPOWIEDZ_TEMPO)) is False


def test_limit_DOBOWY_NIE_JEST_ponawiany():
    """Piec prob z narastajacym odstepem kosztowalo 80 sekund NA ARTYKUL —
    zmierzone na produkcji. Przy limicie dobowym wszystkie pieć jest z gory
    stracone."""
    wywolan = {"n": 0}

    async def atrapa_post(*a, **k):
        wywolan["n"] += 1
        return _odpowiedz(ODPOWIEDZ_DOBOWA)

    g = DostawcaGemini("klucz")
    g.ODSTEP_S = 0.0
    g._http.post = atrapa_post
    with pytest.raises(Przeciazony) as exc:
        asyncio.run(g._poslij("gemini-3.5-flash-lite", {}))
    assert wywolan["n"] == 1, f"limit dobowy ponowiony {wywolan['n']} razy"
    assert exc.value.dobowy is True


def test_limit_TEMPA_nadal_jest_ponawiany():
    """Druga granica: gdyby ponawianie zniknelo dla wszystkich 429, chwilowy
    limit tempa zatrzymywalby potok tak samo jak dobowy."""
    wywolan = {"n": 0}

    async def atrapa_post(*a, **k):
        wywolan["n"] += 1
        return _odpowiedz(ODPOWIEDZ_TEMPO)

    g = DostawcaGemini("klucz")
    g.ODSTEP_S = 0.0
    g._ile_czekac = staticmethod(lambda r, p: 0.0)
    g._http.post = atrapa_post
    with pytest.raises(Przeciazony) as exc:
        asyncio.run(g._poslij("gemini-3.5-flash-lite", {}))
    assert wywolan["n"] == g.PROBY
    assert exc.value.dobowy is False


def test_limit_DOBOWY_zwiera_od_razu_i_NA_DLUZEJ():
    """Przy dziesieciu minutach potok co dziesiec minut odkrywalby od nowa, ze
    limit dobowy jest wyczerpany — przez cala dobe, 144 razy."""
    from services.ingest.llm import DostawcaMieszany

    class Dobowy(_Odmawiajacy):
        async def zapytaj(self, **kw):
            self.wywolan += 1
            raise Przeciazony("limit dobowy", dobowy=True)

    triaz, analiza = Dobowy(), _Dzialajacy()
    ai = DostawcaMieszany(triaz, analiza)
    _pytaj(ai, ai.model_triazu)
    assert ai._zwarcie_otwarte(), "limit dobowy ma zewrzeć po PIERWSZEJ odmowie"
    assert ai.ZWARCIE_DOBOWE_S > ai.ZWARCIE_S


def test_OBA_klucze_znacza_mieszanke_a_nie_zapas():
    """Interfejs opisywal dotad drugi klucz jako uzywany „tylko wtedy, gdy
    pierwszy nie jest ustawiony" — i wlasciciel slusznie uznal, ze jeden klucz
    lezy nieuzywany. Silnik przez caly czas robil co innego."""
    from services.ingest.llm import zbuduj_dostawce

    oba = zbuduj_dostawce({"anthropic": "a", "gemini": "g"})
    assert "+" in oba.nazwa
    assert oba.model_triazu.startswith("gemini"), "filtrowanie ma iść na tańszy model"
    assert oba.model_analizy.startswith("claude"), "analiza ma zostać na mocniejszym"


# ============ awaria TRWALA to nie to samo co awaria PRZEJSCIOWA ============
#
# Log produkcyjny (poda trader-news, 2026-09-10), powtorzony 52-62 razy
# w KAZDYM cyklu, cykl co piec minut — okolo 15 tysiecy bezuzytecznych wywolan
# API na dobe:
#
#   BŁĄD triaż gemini-3.5-flash-lite: BadRequestError: Error code: 400 -
#   {'type': 'error', 'error': {'type': 'invalid_request_error', 'message':
#   'Your credit balance is too low to access the Anthropic API. Please go to
#   Plans & Billing to upgrade or purchase credits.'}}
#
# Wyczerpany kredyt wracal jako HTTP 400, wiec nie byl `Przeciazony`, wiec
# zaden bezpiecznik nie mial czego zewrzec i kazdy artykul kupowal sobie wlasna
# odmowe.

ODMOWA_KREDYTOWA = {
    "type": "error",
    "error": {"type": "invalid_request_error",
              "message": ("Your credit balance is too low to access the Anthropic "
                          "API. Please go to Plans & Billing to upgrade or "
                          "purchase credits.")},
}


def blad_sdk(kod: int, tresc: dict):
    """PRAWDZIWY wyjatek SDK, a nie atrapa.

    Atrapa z wygodnie ustawionymi polami udowodnilaby tylko, ze test i kod
    zgadzaja sie co do wlasnej konwencji. Klasyfikacja czyta `status_code`
    i `body`, wiec musi zobaczyc to, co SDK naprawde wypelnia.
    """
    import anthropic

    odpowiedz = httpx.Response(
        kod, json=tresc,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    klasa = {400: anthropic.BadRequestError,
             401: anthropic.AuthenticationError,
             403: anthropic.PermissionDeniedError,
             429: anthropic.RateLimitError}[kod]
    return klasa(tresc["error"]["message"], response=odpowiedz, body=tresc)


def anthropic_odmawiajacy(blad):
    """DostawcaAnthropic z podstawionym klientem — bez sieci i bez klucza."""
    from services.ingest.llm import DostawcaAnthropic

    class _Klient:
        def __init__(self):
            self.wywolan = 0
            self.messages = self

        async def create(self, **kw):
            self.wywolan += 1
            raise blad

    d = DostawcaAnthropic("klucz-testowy")
    d._c = _Klient()
    return d, d._c


def _pytaj_anthropic(d, model="claude-haiku-4-5"):
    return asyncio.run(d.zapytaj(model=model, system="s", user="u",
                                 schema={"type": "object"}, max_tokens=16))


def test_WYCZERPANY_KREDYT_jest_awaria_TRWALA_a_nie_zwyklym_bledem():
    """Kanarek na blad z produkcji 2026-09-10.

    400 z komunikatem o kredycie NIE JEST bledem w naszym zapytaniu — jest
    odmowa dostawcy, ktora nie minie sama. Zanim to rozroznienie powstalo,
    wyjatek leciał jako goly `BadRequestError`: nikt go nie klasyfikowal, wiec
    nic sie po nim nie zmienialo i nastepny artykul szedl do tego samego
    martwego dostawcy.
    """
    d, klient = anthropic_odmawiajacy(blad_sdk(400, ODMOWA_KREDYTOWA))
    with pytest.raises(Wyczerpany) as e:
        _pytaj_anthropic(d)
    assert klient.wywolan == 1
    assert "srodkow" in str(e.value) or "środków" in str(e.value)
    assert not d.czy_ma_czym_pytac(), (
        "po odmowie rozliczeniowej dostawca ma byc odciety, a nie czekac "
        "na kolejny artykul")


def test_400_ZE_ZLEGO_SCHEMATU_zostaje_naszym_bledem():
    """Druga granica. 400 to u Anthropica ZAROWNO wyczerpany kredyt, JAK I nasz
    wlasny blad w zapytaniu. Gdyby oba szly jako awaria dostawcy, blad w kodzie
    zamilkłby na szesc godzin zamiast krzyknac przy pierwszym artykule."""
    import anthropic

    zly_schemat = {"type": "error",
                   "error": {"type": "invalid_request_error",
                             "message": "output_config.format.schema: unknown field"}}
    d, klient = anthropic_odmawiajacy(blad_sdk(400, zly_schemat))
    with pytest.raises(anthropic.BadRequestError):
        _pytaj_anthropic(d)
    assert d.czy_ma_czym_pytac(), "nasz blad nie moze odcinac dostawcy"


def test_LIMIT_TEMPA_zostaje_awaria_PRZEJSCIOWA():
    """Trzecia granica. 429 mija w sekundy — gdyby wpadl do jednego worka
    z brakiem kredytu, chwilowy limit tempa odcinalby dostawce na szesc godzin."""
    tempo = {"type": "error", "error": {"type": "rate_limit_error",
                                        "message": "Number of requests has exceeded "
                                                   "your rate limit"}}
    d, _ = anthropic_odmawiajacy(blad_sdk(429, tempo))
    with pytest.raises(Przeciazony) as e:
        _pytaj_anthropic(d)
    assert not isinstance(e.value, Wyczerpany), "limit tempa nie jest awaria trwala"
    assert d.czy_ma_czym_pytac()


def test_MARTWY_KLUCZ_jest_awaria_TRWALA():
    """401 i 403 nie mijaja same tak samo jak brak kredytu: uniewazniony klucz
    i brak uprawnien do modelu czekaja na czlowieka, nie na zegar."""
    for kod in (401, 403):
        tresc = {"type": "error", "error": {"type": "authentication_error",
                                            "message": "invalid x-api-key"}}
        d, klient = anthropic_odmawiajacy(blad_sdk(kod, tresc))
        with pytest.raises(Wyczerpany):
            _pytaj_anthropic(d)
        assert not d.czy_ma_czym_pytac(), f"HTTP {kod} ma odcinac dostawce"


def test_awaria_TRWALA_jest_podklasa_PRZECIAZONEGO():
    """To nie jest szczegol implementacji, tylko warunek dzialania zejscia
    awaryjnego. `Przeciazony` znaczy „dostawca odmawia, a nie my sie mylimy"
    i jest jedynym wyjatkiem, po ktorym wolno siegnac po drugiego dostawce
    w triazu. Martwy klucz spelnia ten warunek tak samo jak limit tempa —
    a osobna galaz wymagalaby drugiego `except` w kazdym takim miejscu."""
    assert issubclass(Wyczerpany, Przeciazony)
    assert not issubclass(BrakDostawcy, Przeciazony), (
        "po `BrakDostawcy` nie ma juz do kogo zejsc — lapanie go razem "
        "z `Przeciazony` wrocilo by do wolania martwego dostawcy")


def test_po_awarii_TRWALEJ_dostawca_NIE_JEST_JUZ_PYTANY():
    """SEDNO BLEDU Z 2026-09-10, policzone wprost.

    Cykl idzie po ~55 artykulach. Przed poprawka kazdy z nich wysylal wlasne
    wywolanie do dostawcy z zerowym saldem — 52-62 odmowy na cykl, cykl co piec
    minut, okolo 15 tysiecy wywolan na dobe. Po poprawce pierwsza odmowa odcina
    dostawce, a reszta cyklu nie dotyka juz sieci.
    """
    d, klient = anthropic_odmawiajacy(blad_sdk(400, ODMOWA_KREDYTOWA))
    with pytest.raises(Wyczerpany):
        _pytaj_anthropic(d)
    for _ in range(20):
        with pytest.raises(BrakDostawcy):
            _pytaj_anthropic(d)
    assert klient.wywolan == 1, (
        f"dostawca z zerowym saldem dostal {klient.wywolan} wywolan zamiast "
        f"jednego — bezpiecznik nie dziala")


def test_odciecie_po_awarii_TRWALEJ_jest_CZASOWE_a_nie_dozywotnie():
    """Konto bywa doladowane. Trwale odciecie zamienialoby brak kredytu
    w trwale wylaczenie analizy — i wymagaloby restartu poda, zeby wrocic."""
    d, klient = anthropic_odmawiajacy(blad_sdk(400, ODMOWA_KREDYTOWA))
    with pytest.raises(Wyczerpany):
        _pytaj_anthropic(d)
    d._niedostepny_do = 0.0                   # symulacja uplywu ZWARCIE_TRWALE_S
    with pytest.raises(Wyczerpany):
        _pytaj_anthropic(d)
    assert klient.wywolan == 2, "po odczekaniu dostawca ma dostac kolejna szanse"


def test_awaria_TRWALA_zwiera_DLUZEJ_niz_limit_dobowy_i_tempa():
    """Trzy dlugosci, bo to trzy rozne sytuacje. Limit tempa mija w sekundy,
    dobowy o polnocy, a brak kredytu nie mija sam w ogole — konczy go czlowiek."""
    from services.ingest.llm import DostawcaMieszany

    assert (Dostawca.ZWARCIE_TRWALE_S > DostawcaMieszany.ZWARCIE_DOBOWE_S
            > DostawcaMieszany.ZWARCIE_S)


# =================== bezpiecznik obejmuje OBU dostawcow ====================

class _Martwy(Dostawca):
    """Dostawca z zerowym saldem: kazde zapytanie to awaria TRWALA."""

    def __init__(self, nazwa, triaz, analiza):
        self.nazwa, self.model_triazu, self.model_analizy = nazwa, triaz, analiza
        self.wywolan = 0

    async def zapytaj(self, **kw):
        self.wywolan += 1
        raise Wyczerpany(f"{self.nazwa}: brak srodkow na koncie (HTTP 400)")

    def koszt(self, model, usage):
        return 0.0


def _obaj_martwi():
    from services.ingest.llm import DostawcaMieszany

    tani = _Martwy("tani", "flash-lite", "flash")
    drogi = _Martwy("drogi", "haiku", "opus")
    return DostawcaMieszany(triaz=tani, analiza=drogi), tani, drogi


def test_awaria_TRWALA_u_OBU_daje_jawny_sygnal_ze_NIE_MA_CZYM_PYTAC():
    """Bezpiecznik chronil dotad wylacznie dostawce TRIAZU. Gdy padli obaj,
    nie bylo nic: kazdy artykul dostawal ten sam wyjatek, raz na artykul,
    przez caly cykl i przez kazdy nastepny cykl.

    Wolajacy musi miec z czego zrobic JEDEN warunek — stad `czy_ma_czym_pytac`
    i `BrakDostawcy` podnoszony BEZ wywolania API.
    """
    ai, tani, drogi = _obaj_martwi()
    assert ai.czy_ma_czym_pytac(), "zanim ktorykolwiek odmowil, pytac wolno"

    # Pierwszy artykul: triaz odmawia, zapasowy tez.
    with pytest.raises(Wyczerpany):
        _pytaj(ai, ai.model_triazu)

    assert not ai.czy_ma_czym_pytac(), (
        "obaj dostawcy odcieci, a mieszanka nadal melduje, ze ma czym pytac")
    assert ai.powod_niedostepnosci(), "powod ma trafic do logu, a nie zginac"

    # Reszta cyklu: ani jednego wywolania wiecej.
    for _ in range(20):
        with pytest.raises(BrakDostawcy):
            _pytaj(ai, ai.model_triazu)
        with pytest.raises(BrakDostawcy):
            _pytaj(ai, ai.model_analizy)
    assert (tani.wywolan, drogi.wywolan) == (1, 1), (
        f"po odcieciu obu dostawcy dostali {tani.wywolan} i {drogi.wywolan} "
        f"wywolan zamiast po jednym")


def test_dostawca_ANALIZY_tez_ma_bezpiecznik():
    """Analiza nie ma zejscia awaryjnego i wlasnie dlatego potrzebuje zwarcia:
    bez niego kazdy artykul, ktory przeszedl triaz, kupuje wlasna odmowe
    u dostawcy, o ktorym juz wiadomo, ze odmowi."""
    from services.ingest.llm import DostawcaMieszany

    class Zajety(_Dzialajacy):
        async def zapytaj(self, **kw):
            self.wywolan += 1
            raise Przeciazony("HTTP 429")

    triaz, analiza = _Dzialajacy(), Zajety()
    analiza.nazwa, analiza.model_triazu, analiza.model_analizy = "drogi", "haiku", "opus"
    ai = DostawcaMieszany(triaz=triaz, analiza=analiza)

    for _ in range(5):
        with pytest.raises((Przeciazony, BrakDostawcy)):
            _pytaj(ai, ai.model_analizy)
    assert analiza.wywolan == ai.PROG_ZWARCIA, (
        f"po {ai.PROG_ZWARCIA} odmowach z rzedu dostawca analizy ma nie byc "
        f"pytany, a dostal {analiza.wywolan} zapytan")
    assert not ai.czy_ma_czym_pytac(), "bez dostawcy analizy cykl nie ma co robic"


def test_ANALIZA_dalej_nie_ma_zejscia_na_dostawce_triazu():
    """Bezpiecznik nie moze przy okazji poluzowac tej zasady. Analiza WYTWARZA
    wartosc cechy `news_sent`; podmiana modelu wstawia w srodek szeregu uskok,
    ktorego kalibracja wagi nie zaklada. Brakujaca analiza to tylko brakujacy
    wiersz, ktory widac."""
    from services.ingest.llm import DostawcaMieszany

    triaz = _Dzialajacy()
    triaz.nazwa, triaz.model_triazu, triaz.model_analizy = "tani", "flash-lite", "flash"
    analiza = _Martwy("drogi", "haiku", "opus")
    ai = DostawcaMieszany(triaz=triaz, analiza=analiza)

    with pytest.raises(Wyczerpany):
        _pytaj(ai, ai.model_analizy)
    with pytest.raises(BrakDostawcy):
        _pytaj(ai, ai.model_analizy)
    assert triaz.wywolan == 0, (
        "analiza zeszla na model triazu — cecha `news_sent` dostalaby uskok")


# ============ stan bezpiecznika MUSI przezyc cykl ============
#
# Dowod z logu produkcyjnego 2026-09-10: komunikat
#   „[llm] triaz: gemini — wyczerpany limit DOBOWY; przechodze na anthropic
#    na 60 min"
# pojawial sie co ~6 minut, a nie raz na godzine. Zwarcie na 60 minut nie
# przezywalo cyklu, bo `wybierz_dostawce` buduje dostawce od nowa w KAZDYM
# cyklu, a stan siedzi w instancji.

def test_stan_bezpiecznika_DA_SIE_przeniesc_na_nastepny_cykl():
    ai, tani, drogi = _obaj_martwi()
    with pytest.raises(Wyczerpany):
        _pytaj(ai, ai.model_triazu)
    assert not ai.czy_ma_czym_pytac()

    nowy_cykl, tani2, drogi2 = _obaj_martwi()
    assert nowy_cykl.czy_ma_czym_pytac(), (
        "swiezo zbudowana instancja ma czysty stan — na tym polega ten podblad")

    nowy_cykl.przejmij_stan(ai)
    assert not nowy_cykl.czy_ma_czym_pytac(), (
        "bez przeniesienia stanu zwarcie na 60 min trwa tyle, co cykl")
    with pytest.raises(BrakDostawcy):
        _pytaj(nowy_cykl, nowy_cykl.model_triazu)
    assert (tani2.wywolan, drogi2.wywolan) == (0, 0)


def test_stanu_NIE_DA_SIE_przeniesc_miedzy_innymi_mieszankami():
    """Przeniesienie stanu na inny sklad dostawcow odcieloby dostawce, ktory
    nigdy nie odmowil — i to na szesc godzin, po cichu."""
    from services.ingest.llm import DostawcaMieszany

    ai, _, _ = _obaj_martwi()
    inna = DostawcaMieszany(triaz=_Martwy("inny", "lite-2", "flash-2"),
                            analiza=_Martwy("drogi", "haiku", "opus"))
    with pytest.raises(ValueError):
        inna.przejmij_stan(ai)


# ============ WYJATEK MA MOWIC, KTO ODMOWIL ============
#
# Przy sukcesie model faktyczny wraca w `usage["model_uzyty"]`. Przy odmowie
# `usage` czesto nie ma w ogole - a to wlasnie sciezka odmowy idzie do logu:
#
#   BŁĄD triaż gemini-3.5-flash-lite: BadRequestError: ... 'Your credit balance
#   is too low to access the Anthropic API...'
#
# Nazwa mowila „gemini", odmowil Anthropic. Dlatego mieszanka podpisuje wyjatek
# tym, kto go zwrocil.

def _po_zejsciu(blad):
    """Mieszanka po zejsciu na zapasowego: triaz odbija, odpowiada `drogi`."""
    from services.ingest.llm import DostawcaMieszany

    class Odbija(_Fałszywy):
        async def zapytaj(self, **kw):
            raise Przeciazony("429")

    class Zapasowy(_Fałszywy):
        async def zapytaj(self, *, model, **kw):
            self.pytania.append(model)
            raise blad()

    tani = Odbija("tani", "flash-lite", "flash", (0.3, 2.5))
    drogi = Zapasowy("drogi", "haiku", "opus", (5.0, 25.0))
    return DostawcaMieszany(triaz=tani, analiza=drogi), tani, drogi


def test_wyjatek_NIESIE_dostawce_ktory_go_zwrocil():
    """Sedno poprawki: „kto odmowil" musi byc dostepne TAKZE w wyjatku.

    `Przeciazony` i `Wyczerpany` nie niosa zadnego `usage` - odmowa zapadla,
    zanim powstal pierwszy token. Bez podpisu na samym wyjatku log nie ma skad
    wziac prawdy i podpisuje sie modelem zadanym.
    """
    from services.ingest.llm import kto_wykonal

    ai, tani, drogi = _po_zejsciu(
        lambda: Wyczerpany("drogi: brak srodkow na koncie (HTTP 400)"))
    with pytest.raises(Wyczerpany) as e:
        _pytaj(ai, ai.model_triazu)

    assert drogi.pytania == ["haiku"], "test nie odtworzyl zejscia na zapasowego"
    kto = kto_wykonal(e.value, ai, ai.model_triazu)
    assert (kto.nazwa, kto.model) == ("drogi", "haiku")
    assert str(kto) == "drogi/haiku", "podpis idzie wprost do logu"


def test_bez_podpisu_zostaje_model_ZADANY_bo_przy_jednym_dostawcy_jest_prawdziwy():
    """Druga granica. Domysl nie moze zmyslac: gdy dostawca jest jeden, zadany
    model jest jedynym, ktory mogl odpowiedziec - i wtedy podpisu nie potrzeba."""
    from services.ingest.llm import kto_wykonal

    pojedynczy = _Fałszywy("tani", "flash-lite", "flash", (0.3, 2.5))
    kto = kto_wykonal(RuntimeError("cokolwiek"), pojedynczy, "flash-lite")
    assert (kto.nazwa, kto.model) == ("tani", "flash-lite")


def test_ZUZYCIE_z_nieudanego_wywolania_tez_niesie_model_faktyczny():
    """Bo `Odmowa` NIE JEST awaria i nie dochodzi do obslugi bledow.

    `news_worker.triage` lapie ja i zwraca werdykt „rejected" razem z `usage` -
    ta sciezka wyglada wiec jak sukces i idzie do `news.api_spend` normalnym
    torem, przez `uzyty_model`. Gdyby zuzycie nie niosło modelu faktycznego,
    dziennik nazwalby tanszy model i policzyl tansza stawke za prace zapasowego.
    """
    ai, tani, drogi = _po_zejsciu(
        lambda: Odmowa({"input": 1000, "output": 200, "cache_read": 0}))
    with pytest.raises(Odmowa) as e:
        _pytaj(ai, ai.model_triazu)

    assert e.value.usage["model_uzyty"] == "haiku"
    assert ai.uzyty_model(ai.model_triazu, e.value.usage) == "haiku"
    # Cennik bierze ten, kto wykonal prace: atrapa `drogi` liczy 42.0.
    assert ai.koszt(ai.model_triazu, e.value.usage) == 42.0


# ============ każdy zbudowany dostawca ma swoje zamknięcie ============
#
# `zbuduj_dostawce` nie jest odczytem konfiguracji: KAŻDY zwrócony obiekt otwiera
# pulę połączeń (httpx u Gemini, klient SDK u Anthropica). Worker domyka je od
# 2026-09-10, ale poprawka objęła wyłącznie jego pętlę — a obiekt potrafi powstać
# i zginąć w dwóch miejscach, do których nikt wtedy nie zajrzał.

def test_nieudana_budowa_MIESZANKI_nie_zostawia_OTWARTEJ_puli(monkeypatch):
    """Obiekt, który nie wyjdzie z fabryki, nie ma komu zostać zamknięty.

    `DostawcaAnthropic` importuje pakiet `anthropic` LENIWIE — i to jest decyzja,
    dzięki której obraz złożony pod samo Gemini nie musi go mieć. Skutek uboczny:
    dopisanie drugiego klucza w takim obrazie kończy budowę mieszanki wyjątkiem.
    Gdy `DostawcaGemini` powstał wcześniej, jego pula httpx zostaje otwarta i nie
    ma już żadnej drogi do `zamknij()`: wołający dostał wyjątek, a nie obiekt.

    Dlatego ten, który może nie wstać, powstaje ZANIM powstanie jakakolwiek pula.
    """
    from services.ingest import llm

    otwarte = []
    prawdziwy = llm.DostawcaGemini

    class _Sledzony(prawdziwy):
        def __init__(self, klucz, http=None):
            super().__init__(klucz, http)
            otwarte.append(self)

    class _NieWstaje(llm.Dostawca):
        nazwa = "anthropic"

        def __init__(self, klucz):
            raise ImportError("No module named 'anthropic'")

    monkeypatch.setattr(llm, "DostawcaGemini", _Sledzony)
    monkeypatch.setattr(llm, "DostawcaAnthropic", _NieWstaje)

    with pytest.raises(ImportError):
        llm.zbuduj_dostawce({"anthropic": "a-1", "gemini": "g-1"})

    try:
        assert not otwarte, (
            f"budowa padła po otwarciu {len(otwarte)} puli połączeń — obiekt nie "
            f"wyszedł z `zbuduj_dostawce`, więc nikt jej już nie zamknie")
    finally:
        for d in otwarte:
            asyncio.run(d.zamknij())


def test_ekran_USTAWIEN_zamyka_dostawce_ktorego_zbudowal(monkeypatch):
    """Drugie miejsce, w którym dostawca powstaje — i jedyne bez sprzątania.

    `/api/settings/analiza-tryb` celowo liczy tryb TĄ SAMĄ funkcją, której używa
    worker, żeby ekran nie był drugą kopią reguły. Razem z regułą dostał jednak
    pulę połączeń, której nie zamykał. To gorsze miejsce na wyciek niż pętla
    workera: proces API żyje tygodniami, a `Settings.jsx` woła ten adres przy
    każdym wejściu w ustawienia I po każdym zapisie sekretu — pule przybywają
    bez żadnego ograniczenia.

    Ekranowi potrzebne są same nazwy, więc obiekt nie ma prawa przeżyć odpowiedzi.
    """
    from services.api.routers import settings as ekran
    from services.ingest import llm

    class _Atrapa(llm.Dostawca):
        nazwa = "gemini+anthropic"
        model_triazu = "tani"
        model_analizy = "drogi"

        def __init__(self):
            self.zamkniety = 0

        async def zamknij(self):
            self.zamkniety += 1

    zbudowane = []

    def zbuduj(klucze):
        d = _Atrapa()
        zbudowane.append(d)
        return d

    async def sekrety(sql, *a, **kw):
        return [{"name": "anthropic_api_key"}, {"name": "gemini_api_key"}]

    monkeypatch.setattr(llm, "zbuduj_dostawce", zbuduj)
    monkeypatch.setattr(ekran, "fetch_all", sekrety)

    odpowiedz = asyncio.run(ekran.tryb_analizy())

    # Ekran ma dalej mówić prawdę — sprzątanie nie może zjeść treści.
    assert odpowiedz["aktywny"] is True
    assert odpowiedz["dostawca"] == "gemini+anthropic"
    assert odpowiedz["mieszanka"] is True
    assert len(zbudowane) == 1
    assert zbudowane[0].zamkniety == 1, (
        "ekran ustawień zbudował dostawcę i go nie zamknął — pula połączeń na "
        "każde wejście w ustawienia, w procesie, który żyje tygodniami")


def test_ekran_USTAWIEN_nie_pada_gdy_zamkniecie_sie_nie_uda(monkeypatch):
    """Nieudane domknięcie gniazda to nie powód, żeby ekran przestał się rysować.

    Ta sama zasada, co w `news_worker.zamknij_dostawce`: powód sprzątania jest
    ważniejszy niż jego niepowodzenie. Tu dochodzi jeszcze to, że wyjątek
    z `finally` przesłoniłby gotową już odpowiedź i zamienił odczyt ekranu
    w błąd 500.
    """
    from services.api.routers import settings as ekran
    from services.ingest import llm

    class _NieDaSieZamknac(llm.Dostawca):
        nazwa = "gemini"
        model_triazu = "tani"
        model_analizy = "drogi"

        def __init__(self):
            self.probowano = 0

        async def zamknij(self):
            self.probowano += 1
            raise RuntimeError("gniazdo już zerwane")

    d = _NieDaSieZamknac()

    async def sekrety(sql, *a, **kw):
        return [{"name": "gemini_api_key"}]

    monkeypatch.setattr(llm, "zbuduj_dostawce", lambda klucze: d)
    monkeypatch.setattr(ekran, "fetch_all", sekrety)

    odpowiedz = asyncio.run(ekran.tryb_analizy())

    assert odpowiedz["dostawca"] == "gemini"
    assert d.probowano == 1, "próba zamknięcia miała się odbyć"


def test_BRAK_klucza_nie_buduje_niczego_do_zamkniecia(monkeypatch):
    """Ścieżka bez klucza nie ma obiektu — i nie wolno jej tego zmienić.

    Kanarek na poprawkę wyżej: `finally` dopisany w złym miejscu wywróciłby się
    tu na `None`, czyli dokładnie w tym stanie, w którym właściciel pierwszy raz
    ogląda ten ekran.
    """
    from services.api.routers import settings as ekran
    from services.ingest import llm

    async def bez_sekretow(sql, *a, **kw):
        return []

    monkeypatch.setattr(llm, "zbuduj_dostawce", lambda klucze: None)
    monkeypatch.setattr(ekran, "fetch_all", bez_sekretow)

    odpowiedz = asyncio.run(ekran.tryb_analizy())
    assert odpowiedz["aktywny"] is False
