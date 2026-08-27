/* Zakladka RYZYKO: limity, ich wykorzystanie, naruszenia, kill-switch. */
import React from 'react';
import { useApi } from '../utils/api';
import { fmtNum, fmtTime, fmtAgo } from '../utils/format';

const LIMIT_PL = {
  max_daily_loss: 'Maks. strata dzienna',
  max_drawdown: 'Maks. obsunięcie kapitału',
  max_position_notional: 'Maks. wielkość pozycji',
  max_total_exposure: 'Maks. łączna ekspozycja',
  max_orders_per_hour: 'Maks. zleceń na godzinę',
};
const ACTION_PL = { block: 'blokada', downsize: 'zmniejszenie', killswitch: 'kill-switch' };

export default function Risk({ engine }) {
  const limits = useApi(`/api/engines/${engine}/risk/limits`, [engine], { refreshMs: 30000 });
  const viol = useApi(`/api/engines/${engine}/risk/violations?limit=30`, [engine], { refreshMs: 30000 });
  const ks = useApi(`/api/engines/${engine}/risk/killswitch`, [engine], { refreshMs: 30000 });

  const active = (ks.data || []).filter((k) => k.active);

  return (
    <div className="grid" style={{ gap: 16 }}>
      {active.length > 0 && (
        <div className="notice">
          <strong>Kill-switch aktywny.</strong> {active[0].reason} — uruchomiony {fmtAgo(active[0].triggered_at)}.
          Odblokowanie wymaga ręcznego potwierdzenia; automatyczne wznowienie po obsunięciu
          kapitału jest najprostszą drogą do powtórzenia tej samej straty.
        </div>
      )}

      <div className="glass-card">
        <div className="card-title">
          Limity ryzyka
          <span className="hint">egzekwowane w kodzie, nie w promptcie</span>
        </div>
        {limits.data?.length ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            {limits.data.map((l) => {
              const u = Math.min(1, l.utilization || 0);
              const tone = u > 0.85 ? 'var(--loss)' : u > 0.6 ? 'var(--color-orange)' : 'var(--profit)';
              return (
                <div key={l.id}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
                                marginBottom: 5, flexWrap: 'wrap', gap: 8 }}>
                    <span style={{ fontSize: '0.85rem', fontWeight: 600 }}>
                      {LIMIT_PL[l.name] || l.name}
                      {l.symbol && <span className="dim"> · {l.symbol}</span>}
                    </span>
                    <span style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
                      <span className="mono" style={{ fontSize: '0.78rem', color: tone }}>
                        {fmtNum(l.used, 2)} / {fmtNum(l.threshold, 2)} {l.unit === 'pct' ? '%' : l.unit === 'count' ? '' : ''}
                      </span>
                      <span className="badge">{ACTION_PL[l.action] || l.action}</span>
                    </span>
                  </div>
                  <div className="bar-track"><div className="bar-fill" style={{ width: `${u * 100}%`, background: tone }} /></div>
                </div>
              );
            })}
          </div>
        ) : <div className="empty">Brak zdefiniowanych limitów</div>}
      </div>

      <div className="grid grid-2">
        <div className="glass-card">
          <div className="card-title">
            Naruszenia limitów
            <span className="hint">{viol.data?.length || 0} zdarzeń</span>
          </div>
          {viol.data?.length ? (
            <div className="row-list" style={{ maxHeight: 380 }}>
              {viol.data.map((v) => (
                <div key={v.id} className="row-item">
                  <div className="row-head">
                    <span className="row-title" style={{ fontSize: '0.85rem' }}>
                      {LIMIT_PL[v.limit] || v.limit}
                    </span>
                    <span className="badge bad">{ACTION_PL[v.action] || v.action}</span>
                  </div>
                  <div className="row-meta">
                    <span>{fmtTime(v.at)}</span>
                    <span className="mono">
                      zaobserwowano {fmtNum(v.observed, 2)} przy progu {fmtNum(v.threshold, 2)}
                    </span>
                    {v.decision_id && <span className="dim">decyzja #{v.decision_id}</span>}
                  </div>
                </div>
              ))}
            </div>
          ) : <div className="empty">Brak naruszeń — limity nie były przekroczone</div>}
        </div>

        <div className="glass-card">
          <div className="card-title">
            Historia kill-switcha
            <span className="hint">wymaga ręcznego odblokowania</span>
          </div>
          {ks.data?.length ? (
            <div className="row-list" style={{ maxHeight: 380 }}>
              {ks.data.map((k) => (
                <div key={k.id} className="row-item">
                  <div className="row-head">
                    <span className="row-title" style={{ fontSize: '0.85rem' }}>{k.reason}</span>
                    <span className={`badge ${k.active ? 'bad' : 'ok'}`}>
                      {k.active ? 'aktywny' : 'zwolniony'}
                    </span>
                  </div>
                  <div className="row-meta">
                    <span>uruchomiony {fmtTime(k.triggered_at)}</span>
                    {k.released_at && <span>zwolniony {fmtTime(k.released_at)} przez {k.released_by}</span>}
                  </div>
                </div>
              ))}
            </div>
          ) : <div className="empty">Kill-switch nigdy się nie uruchomił</div>}
        </div>
      </div>
    </div>
  );
}
