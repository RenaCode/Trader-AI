/* Zakladka RUCHY: co system zrobil, dlaczego, i co planuje. */
import React, { useState } from 'react';
import { useApi, get } from '../utils/api';
import { ContributionBars } from '../charts';
import {
  fmtMoney, fmtPrice, fmtNum, fmtTime, ACTION_PL, REGIME_PL, VERDICT_PL, DIRECTION_PL,
} from '../utils/format';

/* Brak pomiaru to nie jest pomiar rowny zeru: `null * 100` dawalo "0%"
   i wygladalo jak zmierzona zerowa pewnosc czy zerowa nowosc. */
const fmtPct100 = (v) => (v == null ? '—' : `${Math.round(v * 100)}%`);

function DecisionDetail({ engine, id, onClose }) {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);

  React.useEffect(() => {
    let alive = true;
    // Zerowanie na wejsciu, bo efekt biegnie od nowa przy kazdej decyzji:
    // bez tego jeden blad zostawal na ekranie przy wszystkich nastepnych
    // (`if (err)` stoi przed sprawdzeniem `d`), a w czasie ladowania widac
    // bylo komplet danych POPRZEDNIEJ decyzji pod nowym numerem.
    setErr(null);
    setD(null);
    get(`/api/engines/${engine}/decisions/${id}`)
      .then((j) => alive && setD(j))
      .catch((e) => alive && setErr(e.message));
    return () => { alive = false; };
  }, [engine, id]);

  if (err) return <div className="notice">Nie udało się wczytać decyzji: {err}</div>;
  if (!d) return <div className="empty loading-pulse">Wczytywanie…</div>;

  return (
    <div className="glass-card" style={{ borderColor: 'var(--border-glass-glow)' }}>
      <div className="card-title">
        Decyzja #{d.id} — {d.symbol} · {ACTION_PL[d.action] || d.action}
        <button className="nav-tab" onClick={onClose}>Zamknij</button>
      </div>

      <div className="grid grid-4" style={{ marginBottom: 14 }}>
        <div className="stat"><span className="stat-label">Pewność</span>
          <span className="stat-value sm">{fmtPct100(d.confidence)}</span></div>
        <div className="stat"><span className="stat-label">Reżim</span>
          <span className="stat-value sm">{REGIME_PL[d.regime] || d.regime}</span></div>
        <div className="stat"><span className="stat-label">Werdykt ryzyka</span>
          <span className={`stat-value sm ${d.risk_verdict === 'allow' ? 'profit' : 'loss'}`}>
            {VERDICT_PL[d.risk_verdict]}</span></div>
        <div className="stat"><span className="stat-label">Czas</span>
          <span className="stat-value sm">{fmtTime(d.at)}</span></div>
      </div>

      {d.risk_reason && <div className="notice" style={{ marginBottom: 14 }}>{d.risk_reason}</div>}

      <div className="card-title" style={{ marginTop: 8 }}>
        Dlaczego — wkład każdego czynnika
        <span className="hint">odczytane z dziennika, nie odtworzone</span>
      </div>
      <ContributionBars items={d.contributions} />

      {d.news?.length > 0 && (
        <>
          <div className="card-title" style={{ marginTop: 18 }}>
            Artykuły, które współtworzyły tę decyzję
            <span className="hint">{d.news.length}</span>
          </div>
          <div className="row-list" style={{ maxHeight: 240 }}>
            {d.news.map((n) => (
              <div key={n.id} className="row-item">
                <div className="row-head">
                  <span className="row-title" style={{ fontSize: '0.85rem' }}>{n.title}</span>
                  <span className={`badge ${n.direction === 'bullish' ? 'ok' : n.direction === 'bearish' ? 'bad' : ''}`}>
                    {DIRECTION_PL[n.direction] || n.direction}
                  </span>
                </div>
                <div className="row-meta">
                  <span>{n.source}</span>
                  <span>{fmtTime(n.at)}</span>
                  <span>nowość {fmtPct100(n.novelty)}</span>
                  <a className="src-link" href={n.url} target="_blank" rel="noopener noreferrer">źródło ↗</a>
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      <div className="card-title" style={{ marginTop: 18 }}>
        Wektor cech w momencie decyzji
        <span className="hint">snapshot — niezależny od późniejszych korekt danych</span>
      </div>
      <div className="table-scroll">
        <table className="table">
          <tbody>
            {Object.entries(d.feature_snapshot || {}).map(([k, v]) => (
              <tr key={k}><td className="mono dim" style={{ fontSize: '0.78rem' }}>{k}</td>
                <td className="mono">{fmtNum(v, 4)}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function Moves({ engine }) {
  const [selected, setSelected] = useState(null);
  const decisions = useApi(`/api/engines/${engine}/decisions?limit=60`, [engine], { refreshMs: 30000 });
  const planned = useApi(`/api/engines/${engine}/planned`, [engine], { refreshMs: 30000 });
  const trades = useApi(`/api/engines/${engine}/trades?limit=40`, [engine]);

  return (
    <div className="grid" style={{ gap: 16 }}>
      {selected && <DecisionDetail engine={engine} id={selected} onClose={() => setSelected(null)} />}

      <div className="glass-card">
        <div className="card-title">
          Co system planuje
          <span className="hint">warunki wejścia oczekujące na spełnienie</span>
        </div>
        {planned.error ? (
          <div className="notice">Nie udało się wczytać planowanych zleceń: {planned.error}</div>
        ) : planned.loading ? (
          <div className="empty loading-pulse">Wczytywanie…</div>
        ) : planned.data?.length ? (
          <div className="row-list" style={{ maxHeight: 320 }}>
            {planned.data.map((p) => (
              <div key={p.id} className="row-item clickable" onClick={() => setSelected(p.decision_id)}>
                <div className="row-head">
                  <span className="row-title">
                    {p.symbol} · {p.side === 'buy' ? 'kupno' : 'sprzedaż'} {fmtNum(p.qty, 4)}
                  </span>
                  <span className="badge warn">oczekuje</span>
                </div>
                <div className="row-meta">
                  <span>typ: {p.order_type}</span>
                  {p.limit_price != null && <span>limit: {fmtPrice(p.limit_price)}</span>}
                  <span>ważne do: {fmtTime(p.valid_until)}</span>
                </div>
                {p.trigger && (
                  <div className="row-body">
                    {Object.entries(p.trigger).map(([k, v]) => (
                      <div key={k}><span className="dim">{k}:</span> <span className="mono">{String(v)}</span></div>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        ) : <div className="empty">Brak zaplanowanych zleceń</div>}
      </div>

      <div className="glass-card">
        <div className="card-title">
          Dziennik decyzji
          <span className="hint">kliknij wiersz, żeby zobaczyć pełne uzasadnienie</span>
        </div>
        {decisions.error ? (
          <div className="notice">Nie udało się wczytać dziennika decyzji: {decisions.error}</div>
        ) : decisions.loading ? (
          <div className="empty loading-pulse">Wczytywanie…</div>
        ) : decisions.data?.length ? (
          <div className="row-list" style={{ maxHeight: 520 }}>
            {decisions.data.map((d) => (
              <div key={d.id} className="row-item clickable" onClick={() => setSelected(d.id)}>
                <div className="row-head">
                  <span className="row-title">{d.symbol} · {ACTION_PL[d.action] || d.action}</span>
                  <span style={{ display: 'flex', gap: 6 }}>
                    <span className="badge">{fmtPct100(d.confidence)}</span>
                    <span className={`badge ${d.risk_verdict === 'allow' ? 'ok' : d.risk_verdict === 'block' ? 'bad' : 'warn'}`}>
                      {VERDICT_PL[d.risk_verdict]}
                    </span>
                  </span>
                </div>
                <div className="row-meta">
                  <span>{fmtTime(d.at)}</span>
                  <span>{REGIME_PL[d.regime] || d.regime}</span>
                  {d.target_price != null && <span className="mono">{fmtPrice(d.target_price)}</span>}
                </div>
              </div>
            ))}
          </div>
        ) : <div className="empty">Dziennik jest pusty</div>}
      </div>

      <div className="glass-card">
        <div className="card-title">
          Zamknięte transakcje
          <span className="hint">{trades.error ? '—' : `${trades.data?.length || 0} pozycji`}</span>
        </div>
        {trades.error ? (
          <div className="notice">Nie udało się wczytać transakcji: {trades.error}</div>
        ) : trades.loading ? (
          <div className="empty loading-pulse">Wczytywanie…</div>
        ) : trades.data?.length ? (
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr><th>Instrument</th><th>Kierunek</th><th>Ilość</th><th>Wejście</th>
                    <th>Otwarta</th><th>Zamknięta</th><th>P&L</th><th>Prowizja</th></tr>
              </thead>
              <tbody>
                {trades.data.map((t) => (
                  <tr key={t.id} className="clickable"
                      onClick={() => t.decision_id && setSelected(t.decision_id)}
                      style={{ cursor: t.decision_id ? 'pointer' : 'default' }}>
                    <td style={{ fontWeight: 600 }}>{t.symbol}</td>
                    <td><span className={`badge ${t.side === 'long' ? 'ok' : 'bad'}`}>{t.side}</span></td>
                    <td className="mono">{fmtNum(t.qty, 4)}</td>
                    <td className="mono">{fmtPrice(t.avg_price)}</td>
                    <td className="dim">{fmtTime(t.opened_at)}</td>
                    <td className="dim">{fmtTime(t.closed_at)}</td>
                    {/* `null >= 0` jest prawda w JS, wiec brak rozliczenia
                        malowal sie na zielono. Brak wyniku ma byc neutralny. */}
                    <td className={`mono ${t.realized_pnl == null ? 'dim' : t.realized_pnl >= 0 ? 'profit' : 'loss'}`}>
                      {t.realized_pnl != null && t.realized_pnl >= 0 ? '+' : ''}{fmtMoney(t.realized_pnl)}
                    </td>
                    <td className="mono dim">{fmtNum(t.fees_paid, 2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <div className="empty">Brak zamkniętych transakcji</div>}
      </div>
    </div>
  );
}
