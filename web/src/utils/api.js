/* Cienka warstwa nad fetch. Wszystkie sciezki sa wzgledne (/api/...), wiec ten sam
   kod dziala lokalnie przez proxy Vite i na VPS za Traefikiem. */
export async function get(path) {
  const res = await fetch(path, { credentials: 'same-origin' });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${path}`);
  return res.json();
}

import { useCallback, useEffect, useState } from 'react';

export function useApi(path, deps = [], { refreshMs = 0 } = {}) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    if (!path) return;
    try {
      const json = await get(path);
      setData(json);
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);

  useEffect(() => {
    setLoading(true);
    load();
    if (!refreshMs) return;
    const id = setInterval(load, refreshMs);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, refreshMs, ...deps]);

  return { data, error, loading, reload: load };
}
