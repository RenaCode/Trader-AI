/* Warstwa HTTP.

   Ciasteczko sesji jest HttpOnly, wiec JS go nie widzi i nie moze wyciec przez XSS.
   Token CSRF jest w osobnym, czytelnym ciasteczku i doklejamy go do kazdej metody
   zmieniajacej stan (double-submit cookie). */
import { useCallback, useEffect, useState } from 'react';

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function csrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)trader_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : '';
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
    throw new ApiError(
      res.status,
      typeof detail === 'string' ? detail : `Błąd ${res.status}`
    );
  }
  return payload;
}

export const get = (path) => request(path);
export const post = (path, body) => request(path, { method: 'POST', body });

/* Hook do odczytu. `onUnauthorized` pozwala aplikacji wyrzucic uzytkownika
   na ekran logowania, gdy sesja wygasnie w trakcie pracy - bez tego dashboard
   pokazywalby w kolko bledy 401. */
export function useApi(path, deps = [], { refreshMs = 0, onUnauthorized } = {}) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    if (!path) return;
    try {
      setData(await get(path));
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401 && onUnauthorized) onUnauthorized();
      setError(e.message);
    } finally {
      setLoading(false);
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
