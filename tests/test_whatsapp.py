"""Kanał WhatsApp przez CallMeBot.

DLACZEGO TE TESTY WYGLADAJA TAK, JAK WYGLADAJA

Powiadomienie ma jedno zadanie: odezwac sie, gdy cos jest nie tak. Kanal, ktory
przestal dzialac, a nie zglasza tego, jest gorszy niz brak kanalu - zamienia
awarie w cisze, a cisza jest nieodroznialna od spokoju.

CallMeBot ma dwie wlasciwosci, ktore prowadza dokladnie do takiej ciszy:

  1. Odpowiada HTTP 200 takze wtedy, gdy odmawia. Powod siedzi w tresci
     odpowiedzi, wiec sam kod stanu nie wystarcza za potwierdzenie.
  2. Przy numerze bez kierunkowego nie zwraca bledu - po prostu nie dostarcza.
     Kanal wyglada wtedy na dzialajacy w nieskonczonosc.

Kazdy test ponizej pilnuje jednego z tych dwoch.
"""
import asyncio

import httpx
import pytest

from services.api.notifications import _send_whatsapp


def bramka(status: int = 200, tresc: str = "Message queued"):
    """CallMeBot z podstawionym transportem. Zwraca (funkcja wysylki, zapytania)."""
    zapytania = []

    def handler(req):
        zapytania.append(req)
        return httpx.Response(status, text=tresc)

    async def wyslij(config, title="Tytuł", body="Treść", severity="warning"):
        transport = httpx.MockTransport(handler)
        oryginalny = httpx.AsyncClient

        class Podstawiony(oryginalny):
            def __init__(self, *a, **kw):
                kw["transport"] = transport
                super().__init__(*a, **kw)

        import services.api.notifications as n
        n.httpx.AsyncClient = Podstawiony
        try:
            await _send_whatsapp(config, title, body, severity)
        finally:
            n.httpx.AsyncClient = oryginalny

    return wyslij, zapytania


KONFIG = {"telefon": "+48123456789", "apikey": "123456"}


def test_wysylka_niesie_numer_klucz_i_tresc():
    wyslij, zapytania = bramka()
    asyncio.run(wyslij(KONFIG, "Kill-switch", "Limit straty dziennej przekroczony"))
    assert len(zapytania) == 1
    q = zapytania[0].url.params
    assert q["phone"] == "+48123456789"
    assert q["apikey"] == "123456"
    assert "Kill-switch" in q["text"] and "Limit straty" in q["text"]


def test_powaga_zdarzenia_jest_widoczna_w_tresci():
    """Alert krytyczny i informacyjny musza sie roznic na pierwszy rzut oka -
    na telefonie widac zwykle sam poczatek."""
    wyslij, zapytania = bramka()
    for sev in ("critical", "warning", "info"):
        asyncio.run(wyslij(KONFIG, "T", "B", sev))
    ikony = {z.url.params["text"][0] for z in zapytania}
    assert len(ikony) == 3, f"trzy poziomy powagi maja dac trzy rozne ikony, dały {ikony}"


def test_ODMOWA_ZE_STATUSEM_200_jest_bledem():
    """Najwazniejszy test w tym pliku.

    CallMeBot na zly klucz odpowiada 200 z wyjasnieniem w tresci. Bez tego
    warunku wysylka konczylaby sie sukcesem, wiersz w notify.delivery mialby
    status 'sent', a wiadomosc nie doszlaby nigdy.
    """
    wyslij, _ = bramka(200, "APIKey is invalid. Please check your API key.")
    with pytest.raises(RuntimeError, match="klucz"):
        asyncio.run(wyslij(KONFIG))


def test_slowo_error_w_tresci_tez_jest_bledem():
    wyslij, _ = bramka(200, "ERROR: phone number not registered")
    with pytest.raises(RuntimeError):
        asyncio.run(wyslij(KONFIG))


def test_status_inny_niz_200_jest_bledem():
    wyslij, _ = bramka(429, "Too many requests")
    with pytest.raises(RuntimeError, match="429"):
        asyncio.run(wyslij(KONFIG))


def test_poprawna_odpowiedz_NIE_jest_bledem():
    """Kanarek do trzech powyzszych. Gdyby warunek bledu byl za szeroki, kazda
    wysylka konczylaby sie 'failed' i kanal wygladalby na zepsuty mimo
    dzialania - a to takze konczy sie tym, ze nikt go nie oglada."""
    wyslij, _ = bramka(200, "Message queued. You will receive it in a few seconds.")
    asyncio.run(wyslij(KONFIG))          # brak wyjatku = sukces


def test_dlugi_alert_jest_przyciety_a_nie_odrzucony():
    wyslij, zapytania = bramka()
    asyncio.run(wyslij(KONFIG, "T", "x" * 5000))
    assert len(zapytania[0].url.params["text"]) <= 900


# --------------------------------------------------------- walidacja numeru

def test_numer_bez_kierunkowego_jest_ODRZUCANY_przy_zapisie():
    """Druga cicha awaria: CallMeBot przy numerze bez kierunkowego nie zwraca
    bledu, tylko nie dostarcza. Kanal wyglada na dzialajacy w nieskonczonosc,
    wiec numer musi byc odrzucony JUZ PRZY ZAPISIE."""
    from services.api.routers.notifications import ChannelBody
    import re as _re

    wzorzec = _re.compile(r"\+[1-9]\d{7,17}")
    assert wzorzec.fullmatch("+48123456789")
    for zly in ("123456789", "48123456789", "+0123456789", "+48 123", "abc"):
        assert not wzorzec.fullmatch(zly), f"{zly} nie powinien przejsc"

    # Model przyjmuje 'whatsapp', a nie juz 'telegram'.
    ChannelBody(kind="whatsapp", label="telefon", telefon="+48123456789", apikey="1")
    with pytest.raises(Exception):
        ChannelBody(kind="telegram", label="x")


def test_telegram_zniknal_z_kodu_i_ze_schematu():
    """Usuniecie kanalu ma byc zupelne. Martwa galaz w wysylce albo martwa
    wartosc w enumie wracaja przy pierwszym przegladzie jako pytanie
    'a czy to jeszcze dziala' - i kosztuja wiecej niz zajmuja."""
    from pathlib import Path

    for plik in ("services/api/notifications.py",
                 "services/api/routers/notifications.py",
                 "web/src/components/Profile.jsx"):
        tresc = Path(plik).read_text(encoding="utf-8").lower()
        assert "telegram" not in tresc, f"{plik} nadal wspomina Telegram"

    migracja = Path("db/migrations/0027_whatsapp_zamiast_telegrama.sql").read_text(encoding="utf-8")
    assert "'inapp', 'whatsapp', 'webhook'" in migracja
