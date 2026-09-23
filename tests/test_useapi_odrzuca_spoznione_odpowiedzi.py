"""PRZEŁĄCZENIE SILNIKA NIE MOŻE POKAZAĆ LICZB DRUGIEGO RYNKU.

CO BYŁO ZŁE

`useApi` (web/src/utils/api.js) nie miał ani `AbortController`, ani strażnika
pokolenia; sprzątanie `useEffect` kasowało wyłącznie `setInterval`, a żądanie
już wysłane leci dalej.

Właściciel klika zakładkę Krypto → Giełda (`App.jsx:126`). Zapytanie
`/api/engines/crypto/overview` jest wolne — okno obciążenia jest znane
i opisane wprost w `main.py:83-88`: kopia 03:17, trener 04:40. Odpowiedź
equity wraca pierwsza, po niej ląduje spóźniona odpowiedź crypto i `setData`
ją NADPISUJE. Na zakładce „Giełda" stoi wtedy kapitał, ekspozycja i P&L
rachunku krypto — bez `error`, bez `loading`, bez jakiegokolwiek oznaczenia.
Liczby wyglądają na zmierzone, bo są; tylko nie dla tego rynku.

To samo dotyczy nakładających się cykli `refreshMs` (20 s w `Overview.jsx`,
30 s w `Risk.jsx`), gdzie starsza odpowiedź może wyprzeć nowszą.

DLACZEGO TEN TEST URUCHAMIA KOD, A NIE CZYTA GO

Wada jest wyłącznie CZASOWA: w źródle nie ma ani jednej linii, która wygląda
źle. Strażnik szukający napisu przeszedłby dla każdej implementacji
o właściwym kształcie i dla żadnej nie powiedziałby, która odpowiedź wygra
wyścig. Ten test bierze więc PRAWDZIWY `web/src/utils/api.js`, podstawia mu
atrapę haczyków Reacta i atrapę `fetch` o sterowanych opóźnieniach, i sprawdza,
co po wyścigu naprawdę zostaje w stanie.

Podmieniony jest wyłącznie import `react` — reszta pliku idzie bajt w bajt.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

KORZEN = Path(__file__).resolve().parents[1]
API_JS = KORZEN / "web/src/utils/api.js"

# Atrapa haczyków Reacta. Zna dokładnie tyle, ile `useApi` używa: stan trzymany
# między przerysowaniami po indeksie wywołania, `useRef` przeżywający
# przerysowanie, `useCallback` bez pamięci (zwrócenie świeżej funkcji jest tu
# bezpieczne — test i tak woła `reload` wprost) i `useEffect` jako pustka,
# bo efekty odpalamy ręcznie, żeby panować nad kolejnością.
ATRAPA_REACT = """
let haczyki = [];
let i = 0;

export function useState(poczatek) {
  const k = i++;
  if (!(k in haczyki)) haczyki[k] = typeof poczatek === 'function' ? poczatek() : poczatek;
  return [haczyki[k], (v) => { haczyki[k] = typeof v === 'function' ? v(haczyki[k]) : v; }];
}
export function useRef(poczatek) {
  const k = i++;
  if (!(k in haczyki)) haczyki[k] = { current: poczatek };
  return haczyki[k];
}
export function useCallback(fn) { i++; return fn; }
export function useEffect() { i++; }
export function przerysuj(fn) { i = 0; return fn(); }
"""

STEND = """
import { przerysuj } from './atrapa-react.mjs';
import { useApi } from './api.js';

/* Opóźnienia dobrane tak, jak to wygląda na produkcji: zapytanie porzuconej
   zakładki jest WOLNE (to dlatego użytkownik ją przełączył), a zapytanie
   nowej wraca od razu. */
const OPOZNIENIE = { '/crypto': 60, '/equity': 5 };

globalThis.fetch = async (sciezka) => {
  await new Promise((r) => setTimeout(r, OPOZNIENIE[sciezka] ?? 0));
  return {
    status: 200, ok: true,
    text: async () => JSON.stringify({ rynek: sciezka, equity: sciezka === '/crypto' ? 106000 : 21000 }),
  };
};

// 1. Zakładka Krypto: zapytanie wychodzi i grzęźnie.
const krypto = przerysuj(() => useApi('/crypto'));
const wolne = krypto.reload();

// 2. Użytkownik przełącza na Giełdę. TEN SAM komponent, nowa ścieżka.
const gielda = przerysuj(() => useApi('/equity'));
const szybkie = gielda.reload();

await Promise.all([szybkie, wolne]);

// 3. Co widać na zakładce Giełda po tym, jak spóźniona odpowiedź wylądowała.
const po = przerysuj(() => useApi('/equity'));
console.log(JSON.stringify({
  rynek: po.data ? po.data.rynek : null,
  equity: po.data ? po.data.equity : null,
  blad: po.error,
  wczytywanie: po.loading,
}));
"""

STEND_KANAREK = """
import { przerysuj } from './atrapa-react.mjs';
import { useApi } from './api.js';

globalThis.fetch = async (sciezka) => ({
  status: 200, ok: true,
  text: async () => JSON.stringify({ rynek: sciezka }),
});

/* Kanarek: zwykły, niezakłócony odczyt ma dalej trafiać do stanu. Strażnik
   pokolenia, który odrzuca WSZYSTKO, przechodziłby test wyżej i nie działał. */
const widok = przerysuj(() => useApi('/crypto'));
await widok.reload();
const po = przerysuj(() => useApi('/crypto'));
console.log(JSON.stringify({
  rynek: po.data ? po.data.rynek : null,
  blad: po.error,
  wczytywanie: po.loading,
}));
"""

STEND_BLAD = """
import { przerysuj } from './atrapa-react.mjs';
import { useApi } from './api.js';

/* Spóźniona odpowiedź, która jest BŁĘDEM, też nie może wylądować na cudzej
   zakładce — inaczej przełączenie rynku malowałoby zdrowy widok na czerwono
   komunikatem o awarii, której na nim nie było. */
const OPOZNIENIE = { '/crypto': 60, '/equity': 5 };
globalThis.fetch = async (sciezka) => {
  await new Promise((r) => setTimeout(r, OPOZNIENIE[sciezka] ?? 0));
  if (sciezka === '/crypto') return { status: 500, ok: false, text: async () => '{"detail":"baza padla"}' };
  return { status: 200, ok: true, text: async () => JSON.stringify({ rynek: sciezka }) };
};

const krypto = przerysuj(() => useApi('/crypto'));
const wolne = krypto.reload();
const gielda = przerysuj(() => useApi('/equity'));
await Promise.all([gielda.reload(), wolne]);

const po = przerysuj(() => useApi('/equity'));
console.log(JSON.stringify({ rynek: po.data ? po.data.rynek : null, blad: po.error }));
"""


def uruchom(tmp_path: Path, stend: str) -> dict:
    """Odpala stend na PRAWDZIWYM `api.js` z podmienionym jednym importem."""
    if shutil.which("node") is None:
        pytest.skip("brak `node` — tego strażnika nie da się tu uruchomić")

    zrodlo = API_JS.read_text(encoding="utf-8")
    podmieniony = zrodlo.replace("from 'react'", "from './atrapa-react.mjs'")
    assert podmieniony != zrodlo, (
        "nie znaleziono importu z 'react' — stend podstawiłby prawdziwy React "
        "albo nic, a wynik nie znaczyłby nic")

    (tmp_path / "atrapa-react.mjs").write_text(ATRAPA_REACT, encoding="utf-8")
    (tmp_path / "api.js").write_text(podmieniony, encoding="utf-8")
    (tmp_path / "stend.mjs").write_text(stend, encoding="utf-8")
    (tmp_path / "package.json").write_text('{"type":"module"}', encoding="utf-8")

    p = subprocess.run(["node", "stend.mjs"], cwd=tmp_path, capture_output=True,
                       text=True, timeout=60)
    assert p.returncode == 0, f"stend nie wstał:\n{p.stdout}\n{p.stderr}"
    return json.loads(p.stdout.strip().splitlines()[-1])


# ============================================ spóźniona odpowiedź nie wygrywa

def test_spozniona_odpowiedz_NIE_nadpisuje_danych_nowej_zakladki(tmp_path):
    wynik = uruchom(tmp_path, STEND)

    assert wynik["rynek"] == "/equity", (
        f"na zakładce Giełda stoją dane rynku {wynik['rynek']} (kapitał "
        f"{wynik['equity']}) — spóźniona odpowiedź porzuconej zakładki "
        f"nadpisała stan, i to bez żadnego oznaczenia")
    assert wynik["blad"] is None
    assert wynik["wczytywanie"] is False, (
        "spóźniona odpowiedź zostawiła widok w stanie wczytywania albo zgasiła "
        "go za cudze zapytanie")


def test_spozniony_BLAD_tez_nie_laduje_na_cudzej_zakladce(tmp_path):
    wynik = uruchom(tmp_path, STEND_BLAD)

    assert wynik["blad"] is None, (
        f"zdrowa zakładka pokazuje błąd {wynik['blad']!r} z zapytania, które "
        f"należało do poprzedniego rynku")
    assert wynik["rynek"] == "/equity"


# ============================================ kanarek na drugą stronę

def test_zwykly_odczyt_dalej_trafia_do_stanu(tmp_path):
    """Strażnik odrzucający wszystko przeszedłby oba testy wyżej."""
    wynik = uruchom(tmp_path, STEND_KANAREK)

    assert wynik["rynek"] == "/crypto"
    assert wynik["blad"] is None
    assert wynik["wczytywanie"] is False
