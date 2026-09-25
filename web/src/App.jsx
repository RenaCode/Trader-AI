import React, { useCallback, useEffect, useState } from 'react';
import Login from './components/Login';
import Overview from './components/Overview';
import Learning from './components/Learning';
import Moves from './components/Moves';
import Sources from './components/Sources';
import Risk from './components/Risk';
import Settings from './components/Settings';
import Profile from './components/Profile';
import Bell from './components/Bell';
import { get, post, useApi, setSessionExpiredHandler } from './utils/api';

const SECTIONS = [
  { key: 'overview', label: 'Stan',           Component: Overview },
  { key: 'learning', label: 'Stopień nauki',  Component: Learning },
  { key: 'moves',    label: 'Ruchy',          Component: Moves },
  { key: 'sources',  label: 'Źródła',         Component: Sources },
  { key: 'risk',     label: 'Ryzyko',         Component: Risk },
  { key: 'settings', label: 'Ustawienia',     Component: Settings, global: true },
  { key: 'profile',  label: 'Profil',         Component: Profile,  global: true },
];

const ENGINE_LABEL = { crypto: 'Krypto', equity: 'Giełda' };
const ENGINE_ICON = { crypto: '₿', equity: '📈' };

export default function App() {
  // 'checking' zapobiega mignieciu ekranu logowania u zalogowanego uzytkownika
  // przy odswiezeniu strony - dopiero odpowiedz /api/auth/me rozstrzyga.
  const [authState, setAuthState] = useState('checking');
  const [user, setUser] = useState(null);

  const checkSession = useCallback(async () => {
    try {
      setUser(await get('/api/auth/me'));
      setAuthState('in');
    } catch {
      setUser(null);
      setAuthState('out');
    }
  }, []);

  useEffect(() => { checkSession(); }, [checkSession]);

  const signOut = useCallback(async () => {
    try { await post('/api/auth/logout'); } catch { /* sesja i tak przepada */ }
    setUser(null);
    setAuthState('out');
  }, []);

  // Sesja moze wygasnac w trakcie pracy. Decyzje podejmuje warstwa HTTP - kazde
  // 401 spoza logowania konczy sie tutaj, niezaleznie od tego, ktory komponent
  // wyslal zapytanie. Wczesniej sluzyl do tego prop `onUnauthorized`, ktorego
  // zadna sekcja nie odbierala: uzytkownik zostawal na pulpicie z danymi
  // sprzed wygasniecia i nie mial jak tego zauwazyc.
  useEffect(() => {
    setSessionExpiredHandler(() => { setUser(null); setAuthState('out'); });
    return () => setSessionExpiredHandler(null);
  }, []);

  const engines = useApi(authState === 'in' ? '/api/engines' : null, [authState]);
  const health = useApi(authState === 'in' ? '/api/health' : null, [authState], { refreshMs: 60000 });
  const [engine, setEngine] = useState(null);
  const [section, setSection] = useState('overview');

  // Zrodlem prawdy dla zakladek jest baza (ref.venue), nie stala w kodzie -
  // dolozenie trzeciego rynku nie wymaga zmiany frontu.
  useEffect(() => {
    if (!engine && engines.data?.length) setEngine(engines.data[0].engine);
  }, [engines.data, engine]);

  const Active = SECTIONS.find((s) => s.key === section)?.Component || Overview;
  const current = engines.data?.find((e) => e.engine === engine);

  if (authState === 'checking') {
    return <div className="login-shell"><div className="loading-pulse dim">Sprawdzanie sesji…</div></div>;
  }

  if (authState === 'out') {
    return <Login onSuccess={checkSession} />;
  }

  if (engines.error) {
    return (
      <div className="app-container">
        <div className="notice">
          Nie udało się połączyć z API: {engines.error}
          <br />Sprawdź, czy usługa <code>api</code> działa.
        </div>
      </div>
    );
  }

  return (
    <div className="app-container">
      <header className="app-header">
        <div className="logo-container">
          <div className="logo-icon">◈</div>
          <div>
            <div className="logo-text">Trader AI</div>
            <div style={{ display: 'flex', gap: 6, marginTop: 3 }}>
              <span className="logo-badge demo">DEMO</span>
              {/* Trzy stany, nie jeden: "API OK" swiecilo zawsze, gdy fetch
                  sie udal, i nie mowilo niczego. Teraz mowi, czy API odpowiada,
                  kiedy ostatnio sprawdzono i ile migracji ma baza. */}
              <span className={`logo-badge ${health.error ? 'bad' : health.data ? '' : 'muted'}`}
                    title={health.error
                      ? `API nie odpowiada: ${health.error}`
                      : health.data
                        ? `API odpowiada · migracji ${health.data.migrations} · sprawdzono ${new Date().toLocaleTimeString('pl-PL')}`
                        : 'Sprawdzam API…'}>
                {health.error ? 'API ✗' : health.data ? `API · ${health.data.migrations} migr.` : 'API …'}
              </span>
            </div>
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        {engines.data?.length > 0 && (
          <div className="engine-tabs" role="tablist">
            {engines.data.map((e) => (
              <button
                key={e.engine}
                role="tab"
                aria-selected={engine === e.engine}
                className={`engine-tab ${engine === e.engine ? 'active' : ''}`}
                onClick={() => setEngine(e.engine)}
              >
                <span>{ENGINE_ICON[e.engine]}</span>
                {ENGINE_LABEL[e.engine] || e.engine}
              </button>
            ))}
          </div>
        )}
        <div className="user-menu">
          <Bell />
          <span className="user-chip">
            <span className="avatar">{(user?.login || '?').slice(0, 1).toUpperCase()}</span>
            {user?.login}
            {user?.totp_enabled
              ? <span className="badge ok">2FA</span>
              : <span className="badge warn">bez 2FA</span>}
          </span>
          <button className="btn-ghost" onClick={signOut}>Wyloguj</button>
        </div>
        </div>
      </header>

      {current && (
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center',
                      marginBottom: 14, fontSize: '0.78rem', color: 'var(--text-dim)' }}>
          <span className="badge info">{current.venue}</span>
          {/* DWIE LICZBY, DWA RÓŻNE ZBIORY.

              Do 2026-09-25 stała tu JEDNA liczba — `active` — podpisana
              słowem opisującym uniwersum decyzyjne.
              Panel mówił przez to „Bybit — 34 instrumentów" dla silnika
              obstawiającego dziesięć i „Alpaca — 21" dla obstawiającego ZERO.

              `active` to instrumenty, dla których zbieramy historię (szerzej,
              niż handlujemy — migracja 0020); `tradeable` to te, które silnik
              obstawia. Podpisy mówią teraz, o co chodzi w każdej z nich. */}
          <span>{current.instruments} w historii</span>
          <span title="uniwersum decyzyjne: instrumenty, którymi silnik obstawia">
            {current.tradeable ?? 0} w uniwersum
          </span>
          <span>{current.is_24_7 ? 'rynek 24/7' : 'sesje giełdowe'}</span>
        </div>
      )}

      <nav className="nav-tabs" role="tablist">
        {SECTIONS.map((s) => (
          <button
            key={s.key}
            role="tab"
            aria-selected={section === s.key}
            className={`nav-tab ${section === s.key ? 'active' : ''}`}
            onClick={() => setSection(s.key)}
          >
            {s.label}
          </button>
        ))}
      </nav>

      {engine ? (
        <Active key={`${engine}-${section}`} engine={engine} user={user}
                onUserChanged={checkSession} />
      ) : (
        <div className="empty loading-pulse">Wczytywanie silników…</div>
      )}

      <footer className="app-footer">
        Trader AI v0.1.0 · faza demo ·{' '}
        <a href="https://renacode.com" target="_blank" rel="noopener noreferrer">RenaCode</a>
      </footer>
    </div>
  );
}
