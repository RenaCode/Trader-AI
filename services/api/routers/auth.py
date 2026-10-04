"""Logowanie, sesje, TOTP.

Zgodnie z OWASP Session Management / Password Storage:
  - Argon2id 64 MiB / t=3 / p=4 (powyzej minimum 19 MiB / t=2 / p=1)
  - identyfikator sesji 256 bitow z CSPRNG, w bazie wylacznie SHA-256
  - NOWA sesja przy kazdym logowaniu i przy zmianie uprawnien (session fixation)
  - ciasteczko HttpOnly + Secure + SameSite=Strict
  - blokada konta po serii nieudanych prob, z rosnacym opoznieniem
"""
from __future__ import annotations

import base64
import io
import secrets
from datetime import datetime, timedelta, timezone

import qrcode
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..auth import (
    SESSION_IDLE_S, SQL_SESJE_AKTYWNE, adres_klienta, audit,
    clear_session_cookies, current_user, require_csrf, set_session_cookies,
)
from ..db import execute, fetch_all, fetch_one
from ..notifications import powiadom_o_bezpieczenstwie
from ..security import (
    SESSION_TTL, backup_code_hash, hash_password_async, new_backup_codes,
    new_session_token, new_totp_secret, session_expiry, sprawdz_haslo, totp_uri,
    verify_totp,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

MAX_FAILED = 5
LOCKOUT = timedelta(minutes=15)

# Ile bledych kodow drugiego skladnika wytrzymuje JEDEN bilet, zanim zostanie
# uniewazniony. Prog jest nizszy niz `MAX_FAILED` celowo: bilet jest jednym
# podejsciem do logowania, a nie budzetem prob na konto. Kto pomylil sie trzy
# razy, ma podac haslo jeszcze raz - i wtedy widac to w `failed_logins`.
MAX_MFA_NA_BILET = 3


class LoginBody(BaseModel):
    login: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=512)


class MfaBody(BaseModel):
    challenge: str = Field(min_length=10, max_length=256)
    code: str = Field(min_length=6, max_length=32)


class TotpConfirmBody(BaseModel):
    code: str = Field(min_length=6, max_length=10)


class PasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=12, max_length=512)


class TotpDisableBody(BaseModel):
    # Wylaczenie drugiego skladnika potwierdza sie BIEZACYM haslem. Do
    # 2026-10-02 endpoint bral `PasswordBody`, czyli wymagal tez pola
    # `new_password` (min. 12 znakow), ktorego nie uzywal - zadanie z samym
    # haslem dostawalo 422.
    current_password: str = Field(min_length=1, max_length=512)
    # I KODEM. Do 2026-10-03 wystarczalo samo haslo, a razem z brakiem limitu
    # prob przy ponownym podaniu hasla dawalo to lancuch: przejeta sesja ->
    # zgadniete haslo -> drugi skladnik wylaczony. Kod z aplikacji albo kod
    # zapasowy dowodzi, ze wylacza ten, kto ma URZADZENIE, a nie tylko sesje.
    code: str = Field(min_length=6, max_length=32)


class TotpSetupBody(BaseModel):
    # Konfiguracja nadpisuje niepotwierdzony sekret i po potwierdzeniu
    # przypina konto do cudzej aplikacji - kto ma tylko sesje, odcinal tym
    # wlasciciela od konta. Haslo jest tu tym samym progiem co przy wylaczeniu.
    current_password: str = Field(min_length=1, max_length=512)


# BLOKADA DLA LOGINU, KTOREGO NIE MA. Konto istniejace po `MAX_FAILED` zlych
# haslach odpowiada 429 z czasem do odblokowania; nieistniejace odpowiadalo
# zawsze 401. Piec prob na dowolny login mowilo wiec, czy konto istnieje.
# Licznik dla loginow nieistniejacych zyje w pamieci procesu (API ma jedna
# replike), zachowuje sie identycznie jak `failed_logins`/`locked_until`
# i jest przyciety, zeby zalew losowych loginow nie zjadl pamieci.
_FANTOMY_MAX = 10_000
_fantomy: dict[str, list] = {}


def _fantom_blokada(login: str) -> datetime | None:
    wpis = _fantomy.get(login)
    return wpis[1] if wpis else None


def _fantom_bledne_haslo(login: str, now: datetime) -> None:
    wpis = _fantomy.pop(login, None) or [0, None]
    wpis[0] += 1
    if wpis[0] >= MAX_FAILED:
        wpis[1] = now + LOCKOUT
    _fantomy[login] = wpis                     # na koniec kolejnosci = najswiezszy
    while len(_fantomy) > _FANTOMY_MAX:
        _fantomy.pop(next(iter(_fantomy)))


def _odpowiedz_blokady(locked_until: datetime, now: datetime) -> HTTPException:
    left = int((locked_until - now).total_seconds())
    return HTTPException(status_code=429,
                         detail=f"Konto zablokowane po nieudanych probach. Sprobuj za {left} s.")


async def potwierdz_haslo(user: dict, haslo: str, request: Request, *,
                          akcja: str, status: int, komunikat: str) -> None:
    """Ponowne podanie hasla w zalogowanej sesji - z TYM SAMYM limitem co logowanie.

    Do 2026-10-03 `/password`, `/totp/disable` i `revoke-others` sprawdzaly
    haslo bez licznika i bez sladu w audycie. Kto mial przejeta sesje,
    zgadywal haslo bez ograniczen i bez sladu - a odgadniete otwieralo zmiane
    hasla i wylaczenie drugiego skladnika.

    Teraz kazda pomylka podbija `failed_logins` (wspolny licznik z logowaniem,
    wiec nie da sie rozlozyc prob na dwie sciezki), zapisuje
    `auth.reauth_failed` z nazwa operacji, a po `MAX_FAILED` konto jest
    zablokowane, a SESJA, z ktorej zgadywano, uniewazniona.
    """
    now = datetime.now(timezone.utc)
    row = await fetch_one("SELECT password_hash, locked_until FROM app.user WHERE id = %s",
                          (user["id"],))
    if row["locked_until"] and row["locked_until"] > now:
        raise _odpowiedz_blokady(row["locked_until"], now)
    ok, _ = await sprawdz_haslo(row["password_hash"], haslo)
    if ok:
        return
    await _odnotuj_bledne_ponowne(user, request, akcja)
    raise HTTPException(status_code=status, detail=komunikat)


async def totp_wlaczone(user_id: int) -> bool:
    return bool(await fetch_one(
        "SELECT 1 AS jest FROM app.totp_secret"
        " WHERE user_id = %s AND confirmed_at IS NOT NULL", (user_id,)))


async def potwierdz_kod_totp(user: dict, kod: str | None, request: Request, *,
                             akcja: str, status: int, komunikat: str) -> None:
    """Kod drugiego skladnika w zalogowanej sesji - jak przy logowaniu.

    TOTP ze zuzyciem kroku (warunkowo i atomowo, zeby przechwycony kod nie
    dzialal drugi raz) albo jednorazowy kod zapasowy. Pomylka idzie do tego
    samego licznika co haslo (`_odnotuj_bledne_ponowne`). Wspolne dla
    wylaczenia TOTP i zwolnienia kill-switcha (przeglad 2026-10-04, T-12 c).
    Wolajacy sprawdza, ze TOTP jest wlaczone; tu brak sekretu = odmowa.
    """
    from ..security import SecretBox

    kod = (kod or "").strip()
    totp = await fetch_one(
        "SELECT secret_enc, last_step FROM app.totp_secret"
        " WHERE user_id = %s AND confirmed_at IS NOT NULL", (user["id"],))
    zuzyty = None
    if totp and kod:
        step = verify_totp(SecretBox().decrypt(bytes(totp["secret_enc"])), kod,
                           last_step=totp["last_step"])
        if step is not None:
            zuzyty = await fetch_one(
                "UPDATE app.totp_secret SET last_step = %s WHERE user_id = %s"
                " AND (last_step IS NULL OR last_step < %s) RETURNING user_id",
                (step, user["id"], step))
        if not zuzyty:
            zuzyty = await fetch_one(
                "UPDATE app.backup_code SET used_at = now()"
                " WHERE user_id = %s AND code_hash = %s AND used_at IS NULL RETURNING id",
                (user["id"], backup_code_hash(kod)))
    if not zuzyty:
        await _odnotuj_bledne_ponowne(user, request, akcja)
        raise HTTPException(status_code=status, detail=komunikat)


async def _odnotuj_bledne_ponowne(user: dict, request: Request, akcja: str) -> None:
    """Licznik W BAZIE (`x + 1`), jak w `SQL_REZERWACJA_KONTA` - z tego samego powodu."""
    stan = await fetch_one(
        "UPDATE app.user SET failed_logins = failed_logins + 1,"
        " locked_until = CASE WHEN failed_logins + 1 >= %s THEN now() + %s"
        "                     ELSE locked_until END"
        " WHERE id = %s RETURNING failed_logins",
        (MAX_FAILED, LOCKOUT, user["id"]),
    )
    await audit("auth.reauth_failed", user_id=user["id"], entity=akcja, request=request)
    if stan and stan["failed_logins"] >= MAX_FAILED:
        await execute("UPDATE app.session SET revoked_at = now()"
                      " WHERE id = %s AND revoked_at IS NULL", (user["session_id"],))
        await audit("auth.session_revoked_after_failures", user_id=user["id"],
                    entity=akcja, request=request)


async def _start_session(user_id: int, mfa_ok: bool, request: Request,
                         response: Response) -> None:
    token, token_hash = new_session_token()
    csrf = secrets.token_urlsafe(32)
    ip = adres_klienta(request)

    await execute(
        "INSERT INTO app.session (user_id, token_hash, expires_at, mfa_satisfied, ip, user_agent)"
        " VALUES (%s,%s,%s,%s,%s,%s)",
        (user_id, token_hash, session_expiry(), mfa_ok, ip, request.headers.get("user-agent")),
    )
    set_session_cookies(response, token, csrf, int(SESSION_TTL.total_seconds()))


async def _zeruj_licznik_prob(user_id: int) -> None:
    """Licznik prob zeruje PELNE zalogowanie, a nie samo poprawne haslo.

    Przy koncie z drugim skladnikiem haslo jest polowa drogi. Zerowanie po tej
    polowie sprawialo, ze `failed_logins` NIGDY nie rosl u kogos, kto haslo zna:
    kazdy nowy bilet startowal od zera, a nieudane kody i tak niczego nie
    podbijaly. Blokada konta - jedyny ogranicznik tempa w tym API - byla wiec
    dla atakujacego z wykradzionym haslem nieosiagalna.
    """
    await execute(
        "UPDATE app.user SET failed_logins = 0, locked_until = NULL, last_login_at = now()"
        " WHERE id = %s", (user_id,),
    )


# REZERWACJA PROBY PRZED WERYFIKACJA (przeglad 2026-10-04, T-12 a).
#
# Do tej poprawki blokada byla sprawdzana PRZED podbiciem licznika: odczyt
# `locked_until`, potem Argon2 (setki ms), dopiero potem `failed_logins + 1`.
# N rownoleglych zadan przechodzilo odczyt razem i dawalo N prob hasla albo
# kodu zamiast 5 (3 na bilet). Teraz proba jest REZERWOWANA jedna instrukcja,
# zanim cokolwiek zostanie sprawdzone: podbicie licznika zachodzi tylko na
# koncie niezablokowanym, a ta, ktora dobija do progu, zaklada blokade - kazda
# nastepna dostaje pusty wynik, czyli odmowe, bez weryfikacji. Udana weryfikacja
# zwraca rezerwacje (`_zwroc_rezerwacje`) albo zeruje licznik przy pelnym
# zalogowaniu (`_zeruj_licznik_prob`).
SQL_REZERWACJA_KONTA = (
    "UPDATE app.user SET failed_logins = failed_logins + 1,"
    " locked_until = CASE WHEN failed_logins + 1 >= %s THEN now() + %s"
    "                     ELSE locked_until END"
    " WHERE id = %s AND (locked_until IS NULL OR locked_until <= now())"
    " RETURNING failed_logins")


async def _zarezerwuj_probe(user_id: int) -> int | None:
    """Liczba prob PO rezerwacji albo None, gdy konto jest zablokowane."""
    r = await fetch_one(SQL_REZERWACJA_KONTA, (MAX_FAILED, LOCKOUT, user_id))
    return None if not r else int(r["failed_logins"])


async def _blokada_konta(user_id: int) -> HTTPException:
    """Odpowiedz 429 z czasem ze stanu bazy - rezerwacja odmowila, wiec blokada
    jest, nawet jesli wiersz przeczytany wczesniej jej nie mial."""
    now = datetime.now(timezone.utc)
    r = await fetch_one("SELECT locked_until FROM app.user WHERE id = %s", (user_id,))
    return _odpowiedz_blokady((r or {}).get("locked_until") or now + LOCKOUT, now)


async def _zwroc_rezerwacje(user_id: int, prob: int) -> None:
    """Poprawne haslo przy koncie z drugim skladnikiem: proba nie byla bledna,
    ale licznika NIE zerujemy (patrz `_zeruj_licznik_prob`). Cofamy tylko te
    jedna rezerwacje, a blokade - wylacznie wtedy, gdy zalozyla ja WLASNIE ona
    (wtedy wczesniejsza byla pusta albo wygasla, bo rezerwacja przechodzi
    tylko na koncie niezablokowanym)."""
    await execute(
        "UPDATE app.user SET failed_logins = greatest(failed_logins - 1, 0),"
        " locked_until = CASE WHEN %s THEN NULL ELSE locked_until END"
        " WHERE id = %s", (prob >= MAX_FAILED, user_id))


async def _zarezerwuj_probe_kodu(session_id: int) -> bool:
    """Rezerwacja proby kodu na BILECIE. False = bilet wyczerpany/uniewazniony.

    Dwie niezalezne bramki przy kazdym kodzie drugiego skladnika - bilet
    (`MAX_MFA_NA_BILET`) i konto (`_zarezerwuj_probe`, wspolny licznik
    z haslem). Zadna z nich sama nie wystarcza: blokada konta zatrzymuje ciag
    prob dopiero po piatej i dotyczy wszystkich biletow razem; uniewaznienie
    biletu zamyka to konkretne podejscie po trzeciej probie. Oba liczniki
    podbijaja sie W BAZIE (`x + 1`) i PRZED weryfikacja kodu.
    """
    r = await fetch_one(
        "UPDATE app.session SET mfa_failed = mfa_failed + 1,"
        " revoked_at = CASE WHEN mfa_failed + 1 >= %s THEN now() ELSE revoked_at END"
        " WHERE id = %s AND revoked_at IS NULL RETURNING mfa_failed",
        (MAX_MFA_NA_BILET, session_id))
    return bool(r)


@router.get("/me")
async def me(user: dict = Depends(current_user)):
    totp = await fetch_one(
        "SELECT confirmed_at IS NOT NULL AS enabled FROM app.totp_secret WHERE user_id = %s",
        (user["id"],),
    )
    return {
        "login": user["login"],
        "role": user["role"],
        "totp_enabled": bool(totp and totp["enabled"]),
        "session_expires_at": user["expires_at"].isoformat(),
    }


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    login_ = body.login.strip()
    row = await fetch_one(
        "SELECT id, login, password_hash, is_active, failed_logins, locked_until"
        " FROM app.user WHERE login = %s",
        (login_,),
    )

    # Stala odpowiedz niezaleznie od tego, czy login istnieje - inaczej czas
    # odpowiedzi i tresc bledu pozwalaja wyliczyc liste kont.
    generic = HTTPException(status_code=401, detail="Nieprawidlowy login lub haslo")

    now = datetime.now(timezone.utc)
    locked_until = row["locked_until"] if row else _fantom_blokada(login_)
    if locked_until and locked_until > now:
        raise _odpowiedz_blokady(locked_until, now)

    if not row:
        # Ta sama weryfikacja Argon2 na atrapie (koszt i czas jak przy koncie
        # istniejacym) i ten sam licznik prob - patrz `_fantomy`.
        await sprawdz_haslo(None, body.password)
        _fantom_bledne_haslo(login_, now)
        await audit("auth.login_failed_unknown", request=request)
        raise generic

    # Licznik podbijany W BAZIE i PRZED weryfikacja - patrz SQL_REZERWACJA_KONTA.
    # Do 2026-10-02 stal tu odczyt z `row` i zapis policzonej w Pythonie
    # wartosci; do 2026-10-04 podbicie szlo po Argon2, wiec rownolegle zadania
    # mijaly odczyt blokady razem.
    prob = await _zarezerwuj_probe(row["id"])
    if prob is None:
        raise await _blokada_konta(row["id"])

    ok, rehash = await sprawdz_haslo(row["password_hash"], body.password)
    if not ok:
        await audit("auth.login_failed", user_id=row["id"], request=request)
        raise generic

    if not row["is_active"]:
        raise HTTPException(status_code=403, detail="Konto nieaktywne")

    if rehash:
        await execute("UPDATE app.user SET password_hash = %s WHERE id = %s", (rehash, row["id"]))

    totp = await fetch_one(
        "SELECT secret_enc FROM app.totp_secret WHERE user_id = %s AND confirmed_at IS NOT NULL",
        (row["id"],),
    )

    if totp:
        # Drugi skladnik wlaczony: sesja powstaje dopiero po podaniu kodu.
        # Do tego czasu wydajemy krotkotrwaly bilet, ktory sam w sobie nic nie otwiera.
        #
        # LICZNIK PROB ZOSTAJE NIETKNIETY - zeruje go dopiero `/mfa`. Patrz
        # `_zeruj_licznik_prob`: kasowanie go tutaj otwieralo droge na wylot.
        # Cofamy wylacznie rezerwacje tej, udanej, proby hasla.
        await _zwroc_rezerwacje(row["id"], prob)
        challenge, challenge_hash = new_session_token()
        await execute(
            "INSERT INTO app.session (user_id, token_hash, expires_at, mfa_satisfied)"
            " VALUES (%s,%s,%s,false)",
            (row["id"], challenge_hash, now + timedelta(minutes=5)),
        )
        await audit("auth.password_ok_mfa_pending", user_id=row["id"], request=request)
        return {"mfa_required": True, "challenge": challenge}

    # Konto bez drugiego skladnika: haslo JEST calym logowaniem, wiec tutaj
    # zerowanie licznika nastepuje w tej samej chwili co dotad.
    await _zeruj_licznik_prob(row["id"])
    await _start_session(row["id"], True, request, response)
    await audit("auth.login", user_id=row["id"], request=request)
    # CALE wykrywanie jest osłonięte, nie tylko wysylka powiadomienia.
    #
    # Poprzednia wersja wolala _zglos_nowe_urzadzenie() wprost, ufajac, ze
    # `powiadom_o_bezpieczenstwie` w srodku polknie ewentualny blad. Ale ten
    # wariant chroni WYLACZNIE ostatni krok - zapytanie, ktore go karmi, bylo
    # nagie. Blad w nazwie kolumny wywrocil wiec logowanie bledem 500.
    #
    # To dokladnie ta awaria, przed ktora ostrzega komentarz przy
    # `powiadom_o_bezpieczenstwie`: mechanizm ostrzegania zablokowal chroniona
    # operacje. Zabezpieczylem ostatni krok zamiast calego skutku ubocznego.
    try:
        await _zglos_nowe_urzadzenie(row["id"], request)
    except Exception as e:                                    # noqa: BLE001
        print(f"[powiadomienie] wykrywanie nowego urządzenia zawiodło: "
              f"{type(e).__name__}: {e}", flush=True)
    return {"mfa_required": False, "login": row["login"]}


async def _zglos_nowe_urzadzenie(user_id: int, request: Request) -> None:
    """Powiadomienie o logowaniu z pary (adres, przegladarka), ktorej nie bylo.

    DEFINICJA "NOWEGO URZADZENIA" JEST TAKA, NA JAKA POZWALAJA DANE

    Dziennik audytu zapisuje `ip` i `user_agent` przy kazdym zdarzeniu. Nowe
    urzadzenie to para, ktora nie wystapila przy zadnym WCZESNIEJSZYM udanym
    logowaniu tego uzytkownika. To nie jest odcisk przegladarki ani rozpoznanie
    sprzetu - i nie udaje, ze jest.

    Skutki tej definicji, ktore trzeba znac: zmiana adresu IP u tego samego
    operatora wywola powiadomienie, a logowanie z tego samego adresu i tej samej
    przegladarki przez kogos innego - nie. Przy systemie trzymajacym klucze API
    gieldy falszywy alarm jest tanszy niz przeoczenie.

    Typ zdarzenia `auth.new_login` jest oznaczony jako `always_on`, wiec
    uzytkownik nie moze go wylaczyc. Tak zaprojektowano to w migracji 0007
    i nikt tego nigdy nie podlaczyl.
    """
    ip = adres_klienta(request)
    ua = request.headers.get("user-agent")

    # `at`, nie `created_at` - kolumna czasu w app.audit_log nazywa sie `at`
    # (migracja 0007). Pierwsza wersja tego zapytania uzywala zlej nazwy
    # i WYWRACALA LOGOWANIE bledem 500.
    #
    # Odejmowanie sekundy odcina wpis wlasnie zapisany przez audit() - inaczej
    # kazde logowanie widzialoby samo siebie jako wczesniejsze i nie zglaszaloby
    # nigdy niczego.
    #
    # Udane logowanie to KAZDA z trzech akcji: konto bez drugiego skladnika
    # konczy na `auth.login`, konto z TOTP na `auth.mfa` albo
    # `auth.mfa_backup_code`. Samo `auth.login` sprawialo, ze przy wlaczonym
    # TOTP zadne urzadzenie nie bylo nigdy "znane".
    znane = await fetch_one(
        "SELECT 1 FROM app.audit_log WHERE actor_user_id = %s"
        "   AND action IN ('auth.login', 'auth.mfa', 'auth.mfa_backup_code')"
        "   AND ip IS NOT DISTINCT FROM %s::inet AND user_agent IS NOT DISTINCT FROM %s"
        "   AND at < now() - interval '1 second' LIMIT 1",
        (user_id, ip, ua))
    if znane:
        return
    await powiadom_o_bezpieczenstwie(
        user_id, "auth.new_login", "Logowanie z nowego urządzenia",
        f"Adres: {ip or 'nieznany'}\nPrzeglądarka: {(ua or 'nieznana')[:200]}\n\n"
        f"Jeśli to nie Ty — zmień hasło i usuń klucze API giełdy.",
        severity="warning")


@router.post("/mfa")
async def mfa(body: MfaBody, request: Request, response: Response):
    from ..security import session_token_hash, SecretBox

    pending = await fetch_one(
        "SELECT s.id, s.user_id, s.expires_at, t.secret_enc, t.last_step, u.locked_until"
        " FROM app.session s JOIN app.totp_secret t ON t.user_id = s.user_id"
        " JOIN app.user u ON u.id = s.user_id"
        " WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.mfa_satisfied = false",
        (session_token_hash(body.challenge),),
    )
    now = datetime.now(timezone.utc)
    if not pending or pending["expires_at"] <= now:
        raise HTTPException(status_code=401, detail="Bilet wygasl - zaloguj sie ponownie")

    # BLOKADA KONTA OBOWIAZUJE TAKZE TUTAJ, nie tylko w `/login`. Sprawdzana
    # wylacznie przy hasle zatrzymywalaby wydawanie NOWYCH biletow, a biletowi
    # juz wydanemu pozwalalaby dokonczyc serie prob - czyli zaczynalaby dzialac
    # z opoznieniem dokladnie wtedy, gdy ktos zgaduje.
    if pending["locked_until"] and pending["locked_until"] > now:
        left = int((pending["locked_until"] - now).total_seconds())
        raise HTTPException(status_code=429,
                            detail=f"Konto zablokowane po nieudanych probach. Sprobuj za {left} s.")

    # REZERWACJA PROBY NA BILECIE I NA KONCIE, ZANIM KOD ZOSTANIE SPRAWDZONY
    # (przeglad 2026-10-04) - inaczej N rownoleglych kodow na jednym bilecie
    # to N prob zamiast MAX_MFA_NA_BILET.
    if not await _zarezerwuj_probe_kodu(pending["id"]):
        raise HTTPException(status_code=401, detail="Bilet wygasl - zaloguj sie ponownie")
    if await _zarezerwuj_probe(pending["user_id"]) is None:
        raise await _blokada_konta(pending["user_id"])

    secret = SecretBox().decrypt(bytes(pending["secret_enc"]))
    code = body.code.strip().replace(" ", "").replace("-", "")

    # Krok czasowy, na ktorym kod przeszedl. Zapisujemy go, zeby ten sam kod
    # nie dal sie uzyc drugi raz w oknie tolerancji zegara (RFC 6238 §5.2).
    step = verify_totp(secret, code, last_step=pending["last_step"])
    accepted = step is not None
    used_backup = False
    if step is not None:
        # Zapis PRZED utworzeniem sesji: gdyby kolejny krok zawiodl, kod i tak
        # ma pozostac zuzyty. Odwrotna kolejnosc zostawialaby okno na powtorzenie.
        #
        # ZUZYCIE KROKU JEST WARUNKOWE I ATOMOWE. `last_step` przeczytany wyzej
        # jest juz nieaktualny, gdy dwa zadania z tym samym kodem ida naraz:
        # oba przechodzily `verify_totp`, oba zapisywaly krok i oba dostawaly
        # sesje - ochrona przed powtorzeniem (RFC 6238 §5.2) nie dzialala
        # dokladnie wtedy, gdy przechwycony kod jest odtwarzany od razu.
        # Wygrywa to zadanie, ktore przestawi krok; drugie traci kod.
        zuzyty = await fetch_one(
            "UPDATE app.totp_secret SET last_step = %s WHERE user_id = %s"
            " AND (last_step IS NULL OR last_step < %s) RETURNING user_id",
            (step, pending["user_id"], step))
        if not zuzyty:
            step = None
            accepted = False

    if not accepted:
        # Kod zapasowy - jednorazowy. Zuzycie i sprawdzenie to JEDNA instrukcja:
        # wczesniej SELECT i osobny UPDATE wpuszczaly ten sam kod dwa razy,
        # gdy dwa zadania zmiescily sie miedzy nimi.
        bc = await fetch_one(
            "UPDATE app.backup_code SET used_at = now()"
            " WHERE user_id = %s AND code_hash = %s AND used_at IS NULL RETURNING id",
            (pending["user_id"], backup_code_hash(body.code)),
        )
        if bc:
            accepted = True
            used_backup = True

    if not accepted:
        # Proba juz policzona (rezerwacja wyzej) - zostaje sam slad w audycie.
        await audit("auth.mfa_failed", user_id=pending["user_id"], request=request)
        raise HTTPException(status_code=401, detail="Nieprawidlowy kod")

    # Bilet zuzyty. Sesja powstaje NOWA - to jest zabezpieczenie przed session fixation.
    await execute("UPDATE app.session SET revoked_at = now() WHERE id = %s", (pending["id"],))
    # DOPIERO TERAZ logowanie jest pelne, wiec dopiero teraz licznik prob wraca
    # do zera. Wczesniej robilo to samo haslo - patrz `_zeruj_licznik_prob`.
    await _zeruj_licznik_prob(pending["user_id"])
    await _start_session(pending["user_id"], True, request, response)
    await audit("auth.mfa_backup_code" if used_backup else "auth.mfa",
                user_id=pending["user_id"], request=request)
    # NOWE URZADZENIE TAKZE PRZY DRUGIM SKLADNIKU. Do 2026-10-02 wykrywanie
    # stalo wylacznie na sciezce konta BEZ TOTP, wiec wlaczenie drugiego
    # skladnika wylaczalo po cichu alarm `always_on` o logowaniu z obcego
    # adresu - lepiej chronione konto bylo gorzej pilnowane. Oslona jak przy
    # `/login`: blad wykrywania nie ma prawa wywrocic logowania.
    try:
        await _zglos_nowe_urzadzenie(pending["user_id"], request)
    except Exception as e:                                    # noqa: BLE001
        print(f"[powiadomienie] wykrywanie nowego urządzenia zawiodło: "
              f"{type(e).__name__}: {e}", flush=True)
    return {"ok": True}


@router.post("/logout", dependencies=[Depends(require_csrf)])
async def logout(request: Request, response: Response, user: dict = Depends(current_user)):
    await execute("UPDATE app.session SET revoked_at = now() WHERE id = %s", (user["session_id"],))
    clear_session_cookies(response)
    await audit("auth.logout", user_id=user["id"], request=request)
    return {"ok": True}


@router.post("/password", dependencies=[Depends(require_csrf)])
async def change_password(body: PasswordBody, request: Request,
                          user: dict = Depends(current_user)):
    await potwierdz_haslo(user, body.current_password, request, akcja="auth.password",
                          status=401, komunikat="Biezace haslo jest nieprawidlowe")

    await execute("UPDATE app.user SET password_hash = %s WHERE id = %s",
                  (await hash_password_async(body.new_password), user["id"]))
    # Zmiana hasla uniewaznia WSZYSTKIE pozostale sesje - jesli ktos przejal konto,
    # traci dostep w tym momencie.
    await execute(
        "UPDATE app.session SET revoked_at = now() WHERE user_id = %s AND id <> %s AND revoked_at IS NULL",
        (user["id"], user["session_id"]),
    )
    await audit("auth.password_changed", user_id=user["id"], request=request)
    # `credential.changed` obejmuje "zmiane kluczy LUB hasla" - patrz etykieta
    # typu zdarzenia w migracji 0007. Zawsze wlaczone, bo zmiana hasla bez
    # wiedzy wlasciciela to pierwszy krok przejecia konta.
    await powiadom_o_bezpieczenstwie(
        user["id"], "credential.changed", "Hasło zostało zmienione",
        "Jeśli to nie Ty — konto jest przejęte. Wszystkie sesje zostały "
        "unieważnione, ale klucze API giełdy nadal działają.",
        severity="warning")
    return {"ok": True}


# ----------------------------------------------------------------- TOTP
@router.post("/totp/setup", dependencies=[Depends(require_csrf)])
async def totp_setup(body: TotpSetupBody, request: Request,
                     user: dict = Depends(current_user)):
    """Generuje sekret i kod QR. Sekret jest AKTYWNY dopiero po potwierdzeniu kodem."""
    from ..security import SecretBox

    await potwierdz_haslo(user, body.current_password, request, akcja="auth.totp_setup",
                          status=401, komunikat="Haslo jest nieprawidlowe")

    existing = await fetch_one(
        "SELECT confirmed_at FROM app.totp_secret WHERE user_id = %s", (user["id"],))
    if existing and existing["confirmed_at"]:
        raise HTTPException(status_code=409, detail="Drugi skladnik jest juz wlaczony")

    secret = new_totp_secret()
    await execute(
        "INSERT INTO app.totp_secret (user_id, secret_enc) VALUES (%s,%s)"
        " ON CONFLICT (user_id) DO UPDATE SET secret_enc = EXCLUDED.secret_enc, confirmed_at = NULL",
        (user["id"], SecretBox().encrypt(secret)),
    )

    uri = totp_uri(secret, user["login"])
    buf = io.BytesIO()
    qrcode.make(uri).save(buf, format="PNG")
    await audit("auth.totp_setup_started", user_id=user["id"], request=request)
    return {
        "secret": secret,                     # do recznego wpisania w aplikacji
        "uri": uri,
        "qr_png_base64": base64.b64encode(buf.getvalue()).decode(),
    }


@router.post("/totp/confirm", dependencies=[Depends(require_csrf)])
async def totp_confirm(body: TotpConfirmBody, request: Request,
                       user: dict = Depends(current_user)):
    from ..security import SecretBox

    row = await fetch_one(
        "SELECT secret_enc, last_step FROM app.totp_secret WHERE user_id = %s", (user["id"],))
    if not row:
        raise HTTPException(status_code=404, detail="Najpierw uruchom konfiguracje")
    step = verify_totp(SecretBox().decrypt(bytes(row["secret_enc"])), body.code,
                       last_step=row["last_step"])
    if step is None:
        raise HTTPException(status_code=401, detail="Kod nie pasuje - sprawdz zegar urzadzenia")

    await execute("UPDATE app.totp_secret SET confirmed_at = now(), last_step = %s"
                  " WHERE user_id = %s", (step, user["id"]))
    await execute("DELETE FROM app.backup_code WHERE user_id = %s", (user["id"],))

    codes = new_backup_codes()
    for _jawny, h in codes:
        await execute("INSERT INTO app.backup_code (user_id, code_hash) VALUES (%s,%s)",
                      (user["id"], h))
    await audit("auth.totp_enabled", user_id=user["id"], request=request)
    # `credential.changed` obejmuje wprost "ustawienia drugiego skladnika"
    # (etykieta w migracji 0011), a nikt go tu nie wysylal. Wlaczenie TOTP
    # z przejetej sesji - setup i confirm nie pytaja o haslo - zamyka
    # wlasciciela poza kontem przy nastepnym logowaniu. Ma o tym wiedziec.
    await powiadom_o_bezpieczenstwie(
        user["id"], "credential.changed", "Włączono weryfikację dwuetapową",
        "Jeśli to nie Ty — ktoś z dostępem do sesji przypisał konto do swojej "
        "aplikacji uwierzytelniającej.",
        severity="warning")
    # Kody pokazywane RAZ - w bazie sa tylko ich hashe.
    return {"ok": True, "backup_codes": [c for c, _ in codes]}


@router.post("/totp/disable", dependencies=[Depends(require_csrf)])
async def totp_disable(body: TotpDisableBody, request: Request,
                       user: dict = Depends(current_user)):
    await potwierdz_haslo(user, body.current_password, request, akcja="auth.totp_disable",
                          status=401, komunikat="Haslo jest nieprawidlowe")

    if not await totp_wlaczone(user["id"]):
        raise HTTPException(status_code=409, detail="Drugi skladnik nie jest wlaczony")
    await potwierdz_kod_totp(user, body.code, request, akcja="auth.totp_disable.code",
                             status=401, komunikat="Nieprawidlowy kod")

    await execute("DELETE FROM app.totp_secret WHERE user_id = %s", (user["id"],))
    await execute("DELETE FROM app.backup_code WHERE user_id = %s", (user["id"],))
    await audit("auth.totp_disabled", user_id=user["id"], request=request)
    await powiadom_o_bezpieczenstwie(
        user["id"], "credential.changed", "Wyłączono weryfikację dwuetapową",
        "Jeśli to nie Ty — konto chroni teraz samo hasło. Zmień je i włącz "
        "drugi składnik ponownie.",
        severity="warning")
    return {"ok": True}


@router.get("/sessions")
async def sessions(user: dict = Depends(current_user)):
    rows = await fetch_all(SQL_SESJE_AKTYWNE,
                           (user["session_id"], user["id"], SESSION_IDLE_S))
    return [
        {"id": r["id"], "created_at": r["created_at"].isoformat(),
         "expires_at": r["expires_at"].isoformat(), "ip": str(r["ip"]) if r["ip"] else None,
         "user_agent": r["user_agent"], "current": r["current"]}
        for r in rows
    ]
