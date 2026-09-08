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
    BladOdpowiedzi, DostawcaGemini, Odmowa, Przeciazony,
    _bez_additional_properties, limit_dobowy_usd,
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
    przyczynie i zuzywa limit."""
    bez_czekania(monkeypatch)
    d, licznik = gemini_sekwencja([(401, {"error": {"message": "bad key"}}, None)])
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(d.zapytaj(model="m", system="s", user="u",
                              schema=TRIAGE_SCHEMA, max_tokens=100))
    assert licznik["n"] == 1, f"401 ma isc bez ponawiania, prob bylo {licznik['n']}"


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

class _Fałszywy:
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

class _Odmawiajacy:
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


class _Dzialajacy:
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
    ai._zwarcie_do = 0.0                      # symulacja uplywu ZWARCIE_S
    _pytaj(ai, ai.model_triazu)
    assert triaz.wywolan == ai.PROG_ZWARCIA + 1, (
        "po odczekaniu dostawca musi dostać kolejną szansę")


def test_UDANE_zapytanie_zeruje_licznik_odmow():
    """Bez zerowania odmowy rozsiane po całej dobie sumowałyby się i w końcu
    odcięłyby dostawcę, który działa poprawnie."""
    from services.ingest.llm import Przeciazony

    ai, triaz, _ = _mieszany()
    _pytaj(ai, ai.model_triazu)
    assert ai._odmowy_triazu == 1

    async def raz_dobrze(**kw):
        return {"ok": True}, {"input": 1, "output": 1, "cache_read": 0}
    triaz.zapytaj = raz_dobrze
    _pytaj(ai, ai.model_triazu)
    assert ai._odmowy_triazu == 0


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
