"""Znacznik próby pożarowej a PRAWDZIWY kill-switch — i próba porzucona.

Dwie sprawy z audytu 2026-09-23, obie o tej samej porze doby i obie o tym, co
czujka wie NAPRAWDĘ, a co tylko zakłada.

ZNALEZISKO 7. `coalesce(d.proba_do > now(), false) AS proba_w_toku` mówi
wyłącznie „na TYM LIMICIE trwa próba", a nie „TO KONKRETNE zdarzenie jest jej
skutkiem". Zdarzenie próby jest z założenia nieodróżnialne od prawdziwego —
migracja 0045 mówi to wprost i jest to CELOWE, bo próba mierzy prawdziwą
ścieżkę. Skutek: zdarzenie `min_equity` powstałe w oknie próby z PRAWDZIWEGO
spadku kapitału dostawało tytuł „PRÓBA POŻAROWA" i treść „To PRÓBA POŻAROWA,
nie strata", a kod wyjścia zostawał zerowy. Odbiorca o 05:30 czytał
zapewnienie, że to sprawdzian kanału — o realnej stracie.

Pełne rozstrzygnięcie wymaga kolumny w `risk.killswitch_event`
z identyfikatorem zdarzenia zapisanym przez próbę, czyli migracji. Bez niej
da się zrobić dwie rzeczy i obie są tu sprawdzane: ZAWĘZIĆ okno do zdarzeń
powstałych po podniesieniu progu (`k.triggered_at >= d.updated_at`) oraz
przestać twierdzić w treści maila coś, czego czujka nie wie.

DRUGA WARSTWA DO ZNALEZISKA 2. `proba_do` przeszłe i wciąż nie-NULL to podpis
próby, która zginęła między podniesieniem progu a blokiem `finally` — bo ten
sam `finally` kasuje znacznik razem z wartością. Handler SIGTERM załatwia
przerwanie, ale nie SIGKILL, OOMKill ani zniknięcie węzła. Ta czujka jest
ostatnią warstwą: nie przywraca wartości (nie ma gdzie zapisanego progu sprzed
próby — to zależność wymagająca migracji), ale zamienia stan niewidoczny
w zgłoszony.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.engine import dozor

KORZEN = Path(__file__).resolve().parents[1]
TERAZ = datetime(2026, 9, 23, 3, 30, tzinfo=timezone.utc)


def _zapytanie_o_zdarzenia() -> str:
    """Treść SQL-a czujki ryzyka, bez komentarzy, w jednej linii."""
    zrodlo = (KORZEN / "services/engine/dozor.py").read_text(encoding="utf-8")
    cialo = zrodlo[zrodlo.index("async def sprawdz_ryzyko"):]
    cialo = cialo[:cialo.index("\n# Ile moze minac od ostatniej udanej kopii")]
    kod = "\n".join(w for w in cialo.splitlines()
                    if not w.lstrip().startswith("#"))
    return " ".join(kod.split())


# ------------------------------------------------------------ znalezisko 7

def test_okno_proby_ZAWEZONE_do_zdarzen_po_podniesieniu_progu():
    """Sam `proba_do > now()` obejmuje też zdarzenia STARSZE od próby.

    Próba ustawia próg, znacznik i `updated_at` JEDNYM UPDATE-em, więc
    `d.updated_at` jest dokładnie chwilą podniesienia progu. Zdarzenie sprzed
    niej nie ma prawa uchodzić za jej skutek — a taki stan jest osiągalny:
    próba, która zginęła przed `finally`, zostawia termin do wygaśnięcia,
    a w tym czasie kill-switch może wystrzelić z prawdziwego powodu.
    """
    sql = _zapytanie_o_zdarzenia()
    assert "d.proba_do > now() AND k.triggered_at >= d.updated_at" in sql, (
        "klasyfikacja zdarzenia jako próby opiera się WYŁĄCZNIE na terminie "
        "na limicie — a termin mówi o limicie, nie o zdarzeniu")


def test_tresc_dla_proby_NIE_ZAPEWNIA_ze_to_nie_strata():
    """Sedno szkody z tego znaleziska nie jest w kodzie wyjścia, tylko
    w zdaniu, które czyta człowiek.

    Prawdziwy spadek kapitału w oknie próby wygląda DOKŁADNIE tak samo jak jej
    skutek (migracja 0045). Powiadomienie, które twierdzi „To PRÓBA POŻAROWA,
    nie strata", zamyka sprawę za odbiorcę — o tej samej porze, o której
    według komentarza chartu prawdziwy nocny kill-switch jest najbardziej
    prawdopodobny.
    """
    zrodlo = (KORZEN / "services/engine/dozor.py").read_text(encoding="utf-8")
    # Bez komentarzy: w tym pliku opis USUNIETEGO zdania stoi tuz obok jego
    # nastepcy i jest tam celowo - historia poprawki jest tu czescia kodu.
    kod = "\n".join(w for w in zrodlo.splitlines()
                    if not w.lstrip().startswith("#"))
    assert "To PRÓBA POŻAROWA, nie strata" not in kod, (
        "treść nadal twierdzi jako pewnik coś, czego czujka nie wie")
    assert "NAJPEWNIEJ jej skutkiem" in zrodlo, (
        "treść musi nazwać swój stopień pewności — inaczej odbiorca dostaje "
        "rozstrzygnięcie zamiast przesłanki")
    assert "nieodróżnialne od prawdziwego" in zrodlo, (
        "powiadomienie nie mówi, CZEGO nie dowodzi")
    assert "Sprawdź stan rachunku" in zrodlo, (
        "brak jedynego działania, które tę wątpliwość rozstrzyga")


# ----------------------------------------- porzucona próba (druga warstwa do 2)

class _Atrapa:
    def __init__(self, porzucone, powiadomiono=False):
        self.porzucone = porzucone
        self.powiadomiono = powiadomiono
        self.wyslane = []

    async def fetch_all(self, sql, params=None):
        p = " ".join(sql.split())
        if "risk.limit_def" in p and "proba_do < now()" in p:
            return list(self.porzucone)
        if "notify.notification" in p:
            return [{"?column?": 1}] if self.powiadomiono else []
        if 'app."user"' in p:
            return [{"id": 1}]
        raise AssertionError(f"atrapa nie zna zapytania: {p}")

    async def notify(self, user_id, kod, tytul, tresc, **kw):
        self.wyslane.append({"kod": kod, "tytul": tytul, "tresc": tresc, **kw})
        return 1


def _porzucona(minut_temu=45, wartosc=100_500):
    return {"engine": "crypto", "mode": "demo", "name": "min_equity",
            "value": wartosc, "proba_do": TERAZ - timedelta(minutes=minut_temu),
            "od_min": minut_temu}


def test_porzucona_proba_JEST_ZGLASZANA():
    """Podpis jest jednoznaczny: `finally` kasuje znacznik razem z wartością,
    więc przeszłe i niewykasowane `proba_do` znaczy, że proces zginął między
    jednym a drugim — a podniesiony `min_equity` ma regułę `reczne` i nie
    wróci sam."""
    a = _Atrapa([_porzucona()])
    kod = asyncio.run(dozor.sprawdz_porzucona_probe(a.fetch_all, a.notify))

    assert kod != 0, "porzucona próba nie maluje zadania na czerwono"
    assert a.wyslane, "nikt się nie dowiedział, że silnik stoi przez artefakt"
    tresc = a.wyslane[0]["tresc"]
    assert "reguł" in tresc and "reczne" in tresc, (
        "treść nie mówi, DLACZEGO to się samo nie naprawi — a to jest cała "
        "różnica między tym stanem a zwykłym kill-switchem")
    assert "Ryzyko" in tresc, "brak wskazania, gdzie przywrócić wartość"


def test_porzucona_proba_NIE_jest_powtarzana_co_dziesiec_minut():
    """To samo tłumienie, co wszędzie w tym pliku: powiadomienie wysłane PO
    zamknięciu tego okna próby znaczy, że ta porzucona próba jest zgłoszona.
    Bez tego 144 maile `critical` na dobę — czyli lekarstwo gorsze od
    choroby."""
    a = _Atrapa([_porzucona()], powiadomiono=True)
    kod = asyncio.run(dozor.sprawdz_porzucona_probe(a.fetch_all, a.notify))

    assert not a.wyslane
    assert kod == 0


def test_zdrowy_stan_NIE_pyta_nawet_o_odbiorcow():
    """Kanarek: czujka, która przy braku znalezisk i tak coś robi, kosztuje
    przy każdym ze 144 przebiegów doby."""
    a = _Atrapa([])
    assert asyncio.run(dozor.sprawdz_porzucona_probe(a.fetch_all, a.notify)) == 0
    assert not a.wyslane


def test_czujka_ryzyka_PYTA_o_porzucona_probe_takze_bez_aktywnych_zdarzen():
    """Porzucony próg potrafi istnieć BEZ czynnego kill-switcha: wystarczy, że
    ktoś zwolnił zdarzenie ręcznie. Silnik wystrzeli wtedy znowu w następnym
    cyklu, bo przyczyna nadal tam jest — a czujka kończąca się na
    `if not zdarzenia` akurat tego stanu nigdy by nie zobaczyła."""
    sql = _zapytanie_o_zdarzenia()
    wywolanie = sql.index("sprawdz_porzucona_probe(fetch_all")
    wyjscie = sql.index("if not zdarzenia")
    assert wywolanie < wyjscie, (
        "sprawdzenie porzuconej próby stoi PO wyjściu z czujki przy pustej "
        "liście zdarzeń — czyli nie wykona się dokładnie w tym stanie, "
        "w którym jest jedynym pytaniem, jakie zostaje")


def test_czujka_porzuconej_proby_NICZEGO_NIE_ZAPISUJE():
    """Przywrócenie wartości wymagałoby progu sprzed próby, a tego nigdzie nie
    ma — `risk.limit_def` trzyma jedną wartość i historii limitów w tej bazie
    nie ma. Zgadnięcie wartości byłoby gorsze od jej braku: limit ryzyka
    ustawiony na oko wygląda dokładnie jak limit uzgodniony.

    Samoczynny powrót wymaga KOLUMNY (wartość sprzed próby + termin) plus
    nadania dla roli `trader_proba`, czyli migracji. Do tego czasu ta czujka
    ma tylko patrzeć — i test tego pilnuje, żeby nikt nie dołożył tu zapisu
    z wartością wziętą z niczego.
    """
    import ast
    import inspect

    # SAM KOD WYKONYWALNY, bez komentarzy I BEZ DOCSTRINGA: docstring tej
    # funkcji tlumaczy, co robi z limitem PROBA (jednym UPDATE-em), wiec
    # szukanie slowa w calym tekscie zapalaloby sie od wyjasnienia, dlaczego
    # ta czujka niczego nie zapisuje.
    drzewo = ast.parse(inspect.getsource(dozor.sprawdz_porzucona_probe).lstrip())
    funkcja = drzewo.body[0]
    if (funkcja.body and isinstance(funkcja.body[0], ast.Expr)
            and isinstance(funkcja.body[0].value, ast.Constant)):
        funkcja.body = funkcja.body[1:]
    kod = ast.unparse(funkcja)
    for zakazane in ("UPDATE", "INSERT", "DELETE"):
        assert zakazane not in kod, (
            f"czujka pisze do bazy ({zakazane}) — a nie ma skąd wziąć progu "
            f"sprzed próby")
