/* Warstwa HTTP.

   Ciasteczko sesji jest HttpOnly, wiec JS go nie widzi i nie moze wyciec przez XSS.
   Token CSRF jest w osobnym, czytelnym ciasteczku i doklejamy go do kazdej metody
   zmieniajacej stan (double-submit cookie). */
import { useCallback, useEffect, useRef, useState } from 'react';

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function csrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)__Host-trader_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : '';
}

/* Wygasniecie sesji obslugujemy w JEDNYM miejscu - tutaj, a nie callbackiem
   przekazywanym propem przez cale drzewo. Tamta droga byla martwa: zadna
   sekcja proga nie odbierala, wiec 401 w trakcie pracy zostawial uzytkownika
   na pulpicie z danymi sprzed wygasniecia. */
let sessionExpired = null;
export function setSessionExpiredHandler(fn) { sessionExpired = fn; }

/* 401 spod /api/auth/ znaczy "zle dane" - zle haslo, zly kod TOTP, wygasly
   bilet miedzy krokami logowania - a nie "sesja wygasla". Ten blad nalezy do
   formularza, ktory go wywolal (Login, zmiana hasla, wlaczanie 2FA); wyrzucenie
   uzytkownika na ekran logowania zabieraloby mu komunikat sprzed oczu.
   O stanie samej sesji rozstrzyga /api/auth/me w App. */
function isLoginFlow(path, method) {
  if (!path.startsWith('/api/auth/')) return false;
  return method !== 'GET' || path.startsWith('/api/auth/me');
}

async function request(path, { method = 'GET', body } = {}) {
  const headers = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (method !== 'GET' && method !== 'HEAD') headers['X-CSRF-Token'] = csrfToken();

  const res = await fetch(path, {
    method,
    headers,
    credentials: 'same-origin',
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (res.status === 204) return null;

  let payload = null;
  const text = await res.text();
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = { detail: text }; }
  }

  if (!res.ok) {
    const detail = payload?.detail;
    if (res.status === 401 && !isLoginFlow(path, method)) sessionExpired?.();
    throw new ApiError(
      res.status,
      typeof detail === 'string' ? detail : `Błąd ${res.status}`
    );
  }
  return payload;
}

export const get = (path) => request(path);
export const post = (path, body) => request(path, { method: 'POST', body });
export const put = (path, body) => request(path, { method: 'PUT', body });
export const del = (path, body) => request(path, { method: 'DELETE', body });

/* Hook do odczytu. Zwraca trzy rozlaczne stany: `loading`, `error` i `data` -
   karty musza je rozrozniac, bo awaria API wygladala dotad identycznie jak
   czysty system ("Dziennik jest pusty"). Przy bledzie `data` zostaje z
   poprzedniego udanego odczytu, wiec nie sluzy do wykrywania awarii.
   401 obsluguje warstwa nizej (setSessionExpiredHandler). */
export function useApi(path, deps = [], { refreshMs = 0 } = {}) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  /* STRAZNIK POKOLENIA: wynik zapytania, ktore zdazylo sie zdezaktualizowac,
     jest ODRZUCANY, a nie wpisywany do stanu.

     Bez niego wystarczylo przelaczyc zakladke Krypto -> Gielda w chwili, gdy
     API jest wolne (okno obciazenia jest znane i opisane w main.py:83-88:
     kopia 03:17, trener 04:40). Odpowiedz equity wracala pierwsza, po niej
     ladowala spozniona odpowiedz crypto i `setData` ja NADPISYWALA. Na
     zakladce "Gielda" stal wtedy kapital, ekspozycja i P&L rachunku krypto -
     bez `error`, bez `loading`, bez jakiegokolwiek oznaczenia. Liczby
     wygladaly na zmierzone, bo byly; tylko nie dla tego rynku.

     To samo dotyczy nakladajacych sie cykli `refreshMs` (20 s w Overview,
     30 s w Risk), gdzie starsza odpowiedz mogla wyprzec nowsza.

     Licznik siedzi w `useRef`, wiec przezywa przerysowanie komponentu - a
     wlasnie przerysowanie ze zmieniona sciezka jest tym momentem, o ktory
     chodzi. Sprzatanie `useEffect` tego nie zalatwialo: kasowalo wylacznie
     `setInterval`, a zadanie juz wyslane leci dalej. */
  const pokolenie = useRef(0);

  const load = useCallback(async () => {
    if (!path) return;
    const moje = (pokolenie.current += 1);
    const aktualne = () => moje === pokolenie.current;
    try {
      const wynik = await get(path);
      if (!aktualne()) return;
      setData(wynik);
      setError(null);
    } catch (e) {
      if (!aktualne()) return;
      setError(e.message);
    } finally {
      // `loading` tez nalezy do biezacego zapytania. Zgaszony przez spozniona
      // odpowiedz oglaszalby koniec wczytywania, ktore jeszcze trwa.
      if (aktualne()) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);

  useEffect(() => {
    setLoading(true);
    load();
    if (!refreshMs) return undefined;
    const id = setInterval(load, refreshMs);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, refreshMs, ...deps]);

  return { data, error, loading, reload: load };
}
