/* Zakladka STAN: co dzieje sie teraz. */
import React from 'react';
import { useApi } from '../utils/api';
import { AreaChart } from '../charts';
import {
  fmtMoney, fmtPct, fmtTime, fmtAgo, fmtNum,
  STATE_PL, STATE_TONE, ACTION_PL,
} from '../utils/format';

function Stat({ label, value, sub, tone }) {
  return (
    <div className="glass-card tight stat">
      <span className="stat-label">{label}</span>
      <span className={`stat-value ${tone || ''}`}>{value}</span>
      {sub && <span className="stat-sub">{sub}</span>}
    </div>
  );
}

export default function Overview({ engine }) {
  const ov = useApi(`/api/engines/${engine}/overview`, [engine], { refreshMs: 20000 });
  const curve = useApi(`/api/engines/${engine}/equity-curve?days=45`, [engine]);
  const pos = useApi(`/api/engines/${engine}/positions`, [engine], { refreshMs: 20000 });
  const recent = useApi(`/api/engines/${engine}/decisions?limit=6`, [engine], { refreshMs: 20000 });

  const o = ov.data;
  const beat = o?.heartbeat;
  const beatAge = beat ? (Date.now() - new Date(beat.last_beat_at).getTime()) / 1000 : null;
  // Dead-man switch: brak heartbeatu > 5 min traktujemy jako silnik nieżywy,
  // niezależnie od tego, co deklaruje kolumna status.
  const beatStale = beatAge != null && beatAge > 300;

  return (
    <div className="grid" style={{ gap: 16 }}>
      <div className="grid grid-4">
        <Stat label="Kapitał" value={o ? fmtMoney(o.equity) : '—'}
              sub={o ? `na ${fmtTime(o.as_of)}` : ''} />
        <Stat label="Zmiana 24h" value={o ? fmtPct(o.change_24h_pct) : '—'}
              tone={o && o.change_24h_pct >= 0 ? 'profit' : 'loss'}
              sub={o ? `od startu ${fmtPct(o.change_total_pct)}` : ''} />
        <Stat label="Ekspozycja" value={o ? fmtMoney(o.exposure) : '—'}
              sub={o ? `gotówka ${fmtMoney(o.cash)}` : ''} />
        <Stat label="Zrealizowany P&L" value={o ? fmtMoney(o.realized_cum) : '—'}
              tone={o && o.realized_cum >= 0 ? 'profit' : 'loss'}
              sub={o ? `niezrealizowany ${fmtMoney(o.unrealized)}` : ''} />
      </div>

      <div className="glass-card">
        <div className="card-title">
          Krzywa kapitału
          <span className="hint">tryb demo · 45 dni</span>
        </div>
        <AreaChart
          data={curve.data || []}
          valueKey="equity"
          height={240}
          valueFmt={(v) => fmtMoney(v)}
          labelFmt={(d) => fmtTime(d.t)}
          color={o && o.change_total_pct >= 0 ? 'var(--profit)' : 'var(--loss)'}
        />
      </div>

      <div className="grid grid-2">
        <div className="glass-card">
          <div className="card-title">
            Stan silnika
            <span className="hint">odświeżane co 20 s</span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div className="row-item">
              <div className="row-head">
                <span className="row-title">Pętla decyzyjna</span>
                <span className={`badge ${beatStale ? 'bad' : beat?.status === 'running' ? 'ok' : 'warn'}`}>
                  {beatStale ? 'BRAK SYGNAŁU' : (beat?.status || '—').toUpperCase()}
                </span>
              </div>
              <div className="row-meta">
                <span>ostatni heartbeat: {fmtAgo(beat?.last_beat_at)}</span>
              </div>
              {beatStale && (
                <div className="row-body" style={{ color: 'var(--danger-light)' }}>
                  Silnik nie zgłosił się od ponad 5 minut. Dead-man switch wstrzymuje
                  nowe zlecenia do czasu przywrócenia sygnału.
                </div>
              )}
            </div>

            <div className="row-item">
              <div className="row-head">
                <span className="row-title">Stan uczenia</span>
                <span className={`badge ${STATE_TONE[o?.learning_status] || 'info'}`}>
                  {STATE_PL[o?.learning_status] || '—'}
                </span>
              </div>
              {o?.learning_reason && <div className="row-body">{o.learning_reason}</div>}
            </div>
          </div>
        </div>

        <div className="glass-card">
          <div className="card-title">
            Otwarte pozycje
            <span className="hint">{pos.data?.length || 0} aktywnych</span>
          </div>
          {pos.data?.length ? (
            <div className="table-scroll">
              <table className="table">
                <thead>
                  <tr><th>Instrument</th><th>Kierunek</th><th>Ilość</th><th>Cena wejścia</th><th>Otwarta</th></tr>
                </thead>
                <tbody>
                  {pos.data.map((p) => (
                    <tr key={p.id}>
                      <td style={{ fontWeight: 600 }}>{p.symbol}</td>
                      <td><span className={`badge ${p.side === 'long' ? 'ok' : 'bad'}`}>{p.side}</span></td>
                      <td className="mono">{fmtNum(p.qty, 4)}</td>
                      <td className="mono">{fmtMoney(p.avg_price)}</td>
                      <td className="dim">{fmtAgo(p.opened_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="empty">Brak otwartych pozycji</div>
          )}
        </div>
      </div>

      <div className="glass-card">
        <div className="card-title">
          Nad czym algorytm ostatnio pracował
          <span className="hint">ostatnie decyzje</span>
        </div>
        {recent.data?.length ? (
          <div className="row-list" style={{ maxHeight: 300 }}>
            {recent.data.map((d) => (
              <div key={d.id} className="row-item">
                <div className="row-head">
                  <span className="row-title">
                    {d.symbol} · {ACTION_PL[d.action] || d.action}
                  </span>
                  <span className={`badge ${d.risk_verdict === 'allow' ? 'ok' : d.risk_verdict === 'block' ? 'bad' : 'warn'}`}>
                    {Math.round(d.confidence * 100)}% pewności
                  </span>
                </div>
                <div className="row-meta">
                  <span>{fmtTime(d.at)}</span>
                  <span>{d.model}</span>
                  {d.latency_ms && <span>{d.latency_ms} ms</span>}
                </div>
                <div className="row-body">{d.rationale}</div>
              </div>
            ))}
          </div>
        ) : (
          <div className="empty">Brak decyzji w dzienniku</div>
        )}
      </div>
    </div>
  );
}
