import React, { useEffect, useState } from 'react';
import Overview from './components/Overview';
import Learning from './components/Learning';
import Moves from './components/Moves';
import Sources from './components/Sources';
import Risk from './components/Risk';
import { useApi } from './utils/api';

const SECTIONS = [
  { key: 'overview', label: 'Stan',           Component: Overview },
  { key: 'learning', label: 'Stopień nauki',  Component: Learning },
  { key: 'moves',    label: 'Ruchy',          Component: Moves },
  { key: 'sources',  label: 'Źródła',         Component: Sources },
  { key: 'risk',     label: 'Ryzyko',         Component: Risk },
];

const ENGINE_LABEL = { crypto: 'Krypto', equity: 'Giełda' };
const ENGINE_ICON = { crypto: '₿', equity: '📈' };

export default function App() {
  const engines = useApi('/api/engines', []);
  const health = useApi('/api/health', [], { refreshMs: 60000 });
  const [engine, setEngine] = useState(null);
  const [section, setSection] = useState('overview');

  // Zrodlem prawdy dla zakladek jest baza (ref.venue), nie stala w kodzie -
  // dolozenie trzeciego rynku nie wymaga zmiany frontu.
  useEffect(() => {
    if (!engine && engines.data?.length) setEngine(engines.data[0].engine);
  }, [engines.data, engine]);

  const Active = SECTIONS.find((s) => s.key === section)?.Component || Overview;
  const current = engines.data?.find((e) => e.engine === engine);

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
              {health.data && <span className="logo-badge">API OK</span>}
            </div>
          </div>
        </div>

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
      </header>

      {current && (
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center',
                      marginBottom: 14, fontSize: '0.78rem', color: 'var(--text-dim)' }}>
          <span className="badge info">{current.venue}</span>
          <span>{current.instruments} instrumentów</span>
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
        <Active key={`${engine}-${section}`} engine={engine} />
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
