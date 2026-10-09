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
import Icon from './components/Icon';
import { get, post, useApi, setSessionExpiredHandler } from './utils/api';
import { fmtMoney, fmtPct } from './utils/format';

const SECTIONS = [
  { key: 'overview', label: 'Stan',           icon: 'home',     Component: Overview },
  { key: 'learning', label: 'Stopień nauki',  icon: 'learning', Component: Learning },
  { key: 'moves',    label: 'Ruchy',          icon: 'moves',    Component: Moves },
  { key: 'sources',  label: 'Źródła',         icon: 'sources',  Component: Sources },
  { key: 'risk',     label: 'Ryzyko',         icon: 'risk',     Component: Risk },
  { key: 'settings', label: 'Ustawienia',     icon: 'settings', Component: Settings, global: true },
  { key: 'profile',  label: 'Profil',         icon: 'profile',  Component: Profile,  global: true },
];

const ENGINE_LABEL = { crypto: 'Krypto', equity: 'Giełda' };
const ENGINE_ICON = { crypto: '₿', equity: <Icon name="equity" size={14} /> };

/* Zakladka silnika w pasku gornym, w ksztalcie "tickera": rynek, kapital
   rachunku i zmiana z doby. Liczby pochodza z /api/kapital/laczny - tego
   samego odczytu, z ktorego karta "Kapital laczny" rysuje wykres - wiec
   pasek i karta nie moga sobie przeczyc. Brak pomiaru to szare "—", nigdy
   zielone zero (`null >= 0` jest w JS prawda). */
function EngineTicker({ e, active, onSelect, zmiana, waluta }) {
  const teraz = zmiana?.teraz;
  const pct = zmiana?.doba_pct;
  return (
    <button
      role="tab"
      aria-selected={active}
      className={`engine-tab ${active ? 'active' : ''}`}
      onClick={onSelect}
    >
      <span className={`engine-glyph ${e.engine}`} aria-hidden="true">{ENGINE_ICON[e.engine] || '•'}</span>
      <span className="engine-name">{ENGINE_LABEL[e.engine] || e.engine}</span>
      <span className="engine-value mono">
        {teraz == null ? '—' : `${fmtMoney(teraz)}${waluta ? ` ${waluta}` : ''}`}
      </span>
      <span className={`engine-change mono ${pct == null ? 'dim' : pct >= 0 ? 'profit' : 'loss'}`}
            title="zmiana kapitału rachunku w ciągu doby">
        {pct == null ? '—' : fmtPct(pct)}
      </span>
    </button>
  );
}

/* Podglad UI bez backendu - WYLACZNIE w `npm run dev` (import.meta.env.DEV).
   W buildzie produkcyjnym Vite podstawia false i caly kod podgladu znika:
   na produkcji nie ma przycisku, falszywego konta ani zmyslonych rynkow. */
function readPreview() {
  try {
    return window.location.search.includes('preview')
      || window.localStorage.getItem('trader_preview') === 'true';
  } catch { return false; }
}
const PREVIEW_ENGINES = [
  { engine: 'crypto', venue: 'binance' },
  { engine: 'equity', venue: 'xetra' },
];

export default function App() {
  // 'checking' zapobiega mignieciu ekranu logowania u zalogowanego uzytkownika
  // przy odswiezeniu strony - dopiero odpowiedz /api/auth/me rozstrzyga.
  const [authState, setAuthState] = useState('checking');
  const [user, setUser] = useState(null);

  const isPreview = import.meta.env.DEV && readPreview();

  const checkSession = useCallback(async () => {
    if (isPreview) {
      setUser({ login: 'admin', role: 'admin', totp_enabled: true });
      setAuthState('in');
      return;
    }
    try {
      setUser(await get('/api/auth/me'));
      setAuthState('in');
    } catch {
      setUser(null);
      setAuthState('out');
    }
  }, [isPreview]);

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
  // Te same parametry co karta "Kapital laczny" (45 dni), wiec liczby w pasku
  // i na karcie pochodza z identycznego zapytania.
  const kapital = useApi(authState === 'in' ? '/api/kapital/laczny?days=45' : null, [authState],
                         { refreshMs: 60000 });
  const [engine, setEngine] = useState(null);
  const [section, setSection] = useState('overview');

  // Zrodlem prawdy dla zakladek jest baza (ref.venue), nie stala w kodzie -
  // dolozenie trzeciego rynku nie wymaga zmiany frontu. Zmyslone rynki tylko
  // w podgladzie deweloperskim.
  const activeEngines = engines.data?.length
    ? engines.data
    : (import.meta.env.DEV && isPreview ? PREVIEW_ENGINES : []);

  useEffect(() => {
    if (!engine && activeEngines.length) setEngine(activeEngines[0].engine);
  }, [activeEngines, engine]);

  const Active = SECTIONS.find((s) => s.key === section)?.Component || Overview;
  const current = activeEngines.find((e) => e.engine === engine);

  if (authState === 'checking') {
    return <div className="login-shell"><div className="loading-pulse dim">Sprawdzanie sesji…</div></div>;
  }

  if (authState === 'out') {
    return <Login onSuccess={checkSession} />;
  }

  if (engines.error && !(import.meta.env.DEV && isPreview)) {
    return (
      <div className="app-container">
        <div className="notice">
          Nie udało się połączyć z API: {engines.error}
          <br />Sprawdź, czy usługa <code>api</code> działa.
        </div>
      </div>
    );
  }

  const activeSection = SECTIONS.find((s) => s.key === section) || SECTIONS[0];

  return (
    <div className="app-container shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="logo-icon" aria-hidden="true">◈</div>
          <div className="logo-text">Trader AI</div>
        </div>

        <nav className="nav-tabs" role="tablist" aria-label="Sekcje panelu">
          {SECTIONS.map((s) => (
            <button
              key={s.key}
              role="tab"
              aria-selected={section === s.key}
              className={`nav-tab ${section === s.key ? 'active' : ''}`}
              onClick={() => setSection(s.key)}
            >
              <Icon name={s.icon} />
              <span>{s.label}</span>
            </button>
          ))}
        </nav>

        <footer className="app-footer">
          Trader AI v0.1.0 · faza demo
          <br />
          <a href="https://renacode.com" target="_blank" rel="noopener noreferrer">RenaCode</a>
        </footer>
      </aside>

      <div className="main">
        <header className="topbar">
          <div className="brand brand-compact">
            <div className="logo-icon" aria-hidden="true">◈</div>
            <div className="logo-text">Trader AI</div>
          </div>

          {engines.data?.length > 0 && (
            <div className="engine-tabs" role="tablist" aria-label="Rynek">
              {engines.data.map((e) => (
                <EngineTicker key={e.engine} e={e} active={engine === e.engine}
                              onSelect={() => setEngine(e.engine)}
                              zmiana={kapital.data?.zmiany?.[e.engine]}
                              waluta={kapital.data?.waluty?.[e.engine]} />
              ))}
            </div>
          )}

          <div className="user-menu">
            <span className="logo-badge demo">DEMO</span>
            {/* Trzy stany, nie jeden: "API OK" swiecilo zawsze, gdy fetch
                sie udal, i nie mowilo niczego. Teraz mowi, czy API odpowiada,
                i kiedy ostatnio sprawdzono. Liczby migracji publiczny
                /api/health juz nie oddaje (audyt Fable U-11). */}
            <span className={`logo-badge ${health.error ? 'bad' : health.data ? '' : 'muted'}`}
                  title={health.error
                    ? `API nie odpowiada: ${health.error}`
                    : health.data
                      ? `API odpowiada · sprawdzono ${new Date().toLocaleTimeString('pl-PL')}`
                      : 'Sprawdzam API…'}>
              {health.error ? 'API ✗' : health.data ? 'API ✓' : 'API …'}
            </span>
            <Bell />
            <span className="user-chip">
              <span className="avatar" aria-hidden="true">{(user?.login || '?').slice(0, 1).toUpperCase()}</span>
              <span className="user-login">{user?.login}</span>
              {user?.totp_enabled
                ? <span className="badge ok">2FA</span>
                : <span className="badge warn">bez 2FA</span>}
            </span>
            <button className="btn-ghost btn-logout" onClick={signOut}>
              <Icon name="logout" size={16} />
              <span>Wyloguj</span>
            </button>
          </div>
        </header>

        <div className="page-head">
          <h1 className="page-title">{activeSection.label}</h1>
          {current && (
            <div className="page-meta">
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
        </div>

        {engine ? (
          <Active key={`${engine}-${section}`} engine={engine} user={user}
                  onUserChanged={checkSession} />
        ) : (
          <div className="empty loading-pulse">Wczytywanie silników…</div>
        )}
      </div>
    </div>
  );
}
