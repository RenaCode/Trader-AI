"""Czy z kopii zapasowej da się ODTWORZYĆ bazę — nie: czy kopia się robi.

Zadanie `trader-backup` robi `pg_dump --format=custom` i weryfikuje go przez
`pg_restore --list`. To dowodzi, że archiwum jest CZYTELNE. Nie dowodzi, że
wgra się do działającej bazy — a między jednym a drugim leżą dwie rzeczy,
których w zrzucie po prostu nie ma.

ROLE. `pg_dump` zrzuca JEDNĄ BAZĘ. Role są obiektem klastra i nie trafiają do
zrzutu — trafiają za to wszystkie GRANT-y, które się do nich odwołują (na
produkcji 7927 nadań tabelowych na pięć ról z migracji 0034). Odtworzenie na
czystym klastrze wywala się na każdym z nich, a procesy nie mają się czym
zalogować, bo ról nie ma. Kopia przechodzi `pg_restore --list` i mimo to nie
wstaje.

WERSJA TIMESCALE. Zrzut niesie zawartość `_timescaledb_catalog` (rozszerzenie
ma ustawione `extconfig`), a `pg_dump` pisze `CREATE EXTENSION timescaledb`
BEZ klauzuli VERSION. Obraz bazy jest przypięty do ruchomego tagu `pg17`,
więc przy odtwarzaniu za pół roku rozszerzenie założy się w wersji nowszej niż
ta, z której pochodzi zrzucony katalog. Restorujący nie ma jak się dowiedzieć,
której wersji potrzebuje, bo nigdzie jej nie zapisujemy.
"""
from __future__ import annotations

import re
from pathlib import Path

KORZEN = Path(__file__).resolve().parents[1]
BACKUP_YAML = (KORZEN / "charts" / "trader" / "templates" / "backup.yaml"
               ).read_text(encoding="utf-8")
MIGRACJA_ROL = (KORZEN / "db" / "migrations" / "0034_role_procesow.sql"
                ).read_text(encoding="utf-8")


def _skrypt_zrzutu() -> str:
    """Sama treść polecenia zadania, bez komentarzy YAML-a nad nim.

    Komentarz w `backup.yaml` opisuje procedurę odtwarzania z TimescaleDB
    (`timescaledb_pre_restore`) i to jest dobry komentarz — ale komentarz nie
    jest wykonywany. Test musi patrzeć na to, co zadanie ROBI.
    """
    ciało = BACKUP_YAML.split("command:", 1)[1]
    return "\n".join(l for l in ciało.splitlines()
                     if not l.lstrip().startswith("#"))


def test_kopia_obejmuje_role_ktore_nazywa():
    """Zrzut nadaje prawa rolom, których sam nie tworzy.

    To jest cała wada: `pg_dump` bez zrzutu obiektów globalnych daje archiwum
    wewnętrznie sprzeczne — pełne GRANT-ów na role nieistniejące w pliku.
    """
    role = set(re.findall(r"\btrader_(?:app|silnik|dozorca|czujka|proba)\b",
                          MIGRACJA_ROL))
    assert len(role) == 5, "migracja 0034 przestała definiować pięć ról procesów"

    skrypt = _skrypt_zrzutu()
    assert "pg_dumpall" in skrypt and "--globals-only" in skrypt, (
        "kopia nie zawiera definicji ról — pg_dump zrzuca jedną bazę, a role "
        "są obiektem klastra. Odtworzenie na czystym klastrze padnie na każdym "
        f"GRANT-cie dla {sorted(role)} i żaden proces się nie zaloguje."
    )


def test_kopia_notuje_wersje_timescale():
    """Bez zapisanej wersji rozszerzenia nie wiadomo, w czym odtwarzać.

    `pg_dump` pisze `CREATE EXTENSION timescaledb` bez wersji, a zrzucony
    katalog `_timescaledb_catalog` pasuje tylko do tej wersji, z której
    pochodzi. Ruchomy tag `pg17` znaczy, że obraz użyty do odtworzenia jest
    inny niż ten, który robił zrzut.
    """
    skrypt = _skrypt_zrzutu()
    assert re.search(r"extversion|timescaledb_information|--version-file"
                     r"|WERSJA", skrypt), (
        "zadanie nie zapisuje wersji TimescaleDB obok zrzutu — odtwarzający "
        "nie ma jak dobrać obrazu, a niezgodna wersja rozjeżdża katalog "
        "rozszerzenia"
    )
