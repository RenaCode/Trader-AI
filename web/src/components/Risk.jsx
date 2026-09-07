/* Zakladka RYZYKO: limity, ich wykorzystanie, naruszenia, kill-switch. */
import React, { useState } from 'react';
import { useApi, post } from '../utils/api';
import { fmtNum, fmtTime, fmtAgo } from '../utils/format';

const LIMIT_PL = {
  max_daily_loss: 'Maks. strata dzienna',
  max_drawdown: 'Maks. obsunięcie kapitału',
  max_position_notional: 'Maks. wielkość pozycji',
  max_total_exposure: 'Maks. łączna ekspozycja',
  max_orders_per_hour: 'Maks. zleceń na godzinę',
  min_equity: 'Próg kapitału',
};
const UNIT_PL = { pct: '%', pct_equity: '% kapitału', quote_ccy: 'USD', count: 'szt.' };
const ACTION_PL = { block: 'blokada', downsize: 'zmniejszenie', killswitch: 'kill-switch' };
// Regula wznowienia jest czescia definicji limitu. Bez niej interfejs mowi,
// ze system sie zatrzyma, i milczy o tym, czy sam wroci - a to pierwsze pytanie,
// jakie zadaje sie o autonomiczny bezpiecznik.
const WZNOWIENIE_PL = {
  reczne: { etykieta: 'tylko ręcznie', opis: 'granica kapitału — automat jej nie zwalnia' },
  po_ustaniu: { etykieta: 'sam, po ustaniu', opis: 'limit okresowy — mija razem z okresem' },
  po_odbudowie: { etykieta: 'sam, po odbudowie', opis: 'wznowienie poniżej połowy limitu' },
};

export default function Risk({ engine }) {
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const limits = useApi(`/api/engines/${engine}/risk/limits`, [engine], { refreshMs: 30000 });
  const viol = useApi(`/api/engines/${engine}/risk/violations?limit=30`, [engine], { refreshMs: 30000 });
  const ks = useApi(`/api/engines/${engine}/risk/killswitch`, [engine], { refreshMs: 30000 });
  const doz = useApi(`/api/engines/${engine}/risk/dozorca`, [engine], { refreshMs: 30000 });

  const active = (ks.data || []).filter((k) => k.active);

  return (
    <div className="grid" style={{ gap: 16 }}>
      {active.length > 0 && (
        <div className="notice">
          <strong>Kill-switch aktywny.</strong> {active[0].reason} — uruchomiony {fmtAgo(active[0].triggered_at)}.
          Odblokowanie wymaga ręcznego potwierdzenia; automatyczne wznowienie po obsunięciu
          kapitału jest najprostszą drogą do powtórzenia tej samej straty.

          <div style={{ marginTop: 12 }}>
            <label className="field" style={{ marginBottom: 8 }}>
              <span className="field-label">
                Powód zwolnienia — co się zmieniło, żeby to się nie powtórzyło?
              </span>
              <input className="input-field" value={note} maxLength={500}
                     placeholder="np. przyczyną był błąd w danych świec, poprawiony w sha-…"
                     onChange={(e) => { setNote(e.target.value); setErr(null); }} />
            </label>
            <button className="btn" disabled={busy || note.trim().length < 10}
                    onClick={async () => {
                      setBusy(true); setErr(null);
                      try {
                        await post(`/api/engines/${engine}/risk/killswitch/${active[0].id}/release`,
                                   { note: note.trim() });
                        setNote('');
                        ks.reload?.();
                      } catch (e) {
                        setErr(e.message || 'Nie udało się zwolnić');
                      } finally { setBusy(false); }
                    }}>
              {busy ? 'Zwalniam…' : 'Zwolnij kill-switch'}
            </button>
            {note.trim().length > 0 && note.trim().length < 10 && (
              <span className="hint" style={{ marginLeft: 10 }}>
                powód musi mieć co najmniej 10 znaków
              </span>
            )}
            {err && <div className="notice error" style={{ marginTop: 8 }}>{err}</div>}
          </div>
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
                      {l.action === 'killswitch' && WZNOWIENIE_PL[l.wznowienie] && (
                        <span className="badge" title={WZNOWIENIE_PL[l.wznowienie].opis}>
                          {WZNOWIENIE_PL[l.wznowienie].etykieta}
                        </span>
                      )}
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
            <span className="hint">
              {doz.data?.length
                ? (doz.data.every((d) => d.zyje)
                    ? `dozorca żyje — puls ${doz.data[0].sekund_temu}s temu`
                    : 'DOZORCA NIE BIJE')
                : 'brak dozorcy'}
            </span>
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
                    {k.limit_name && <span className="dim">{LIMIT_PL[k.limit_name] || k.limit_name}</span>}
                    {k.released_at && (
                      <span>
                        zwolniony {fmtTime(k.released_at)}
                        {k.released_auto ? ' — automatycznie' : ` przez ${k.released_by || 'człowieka'}`}
                      </span>
                    )}
                  </div>
                  {k.release_note && (
                    <div className="row-meta">
                      <span className="dim" title="podstawa wznowienia">{k.release_note}</span>
                    </div>
                  )}
                </div>
              ))}
            </div>
          ) : <div className="empty">Kill-switch nigdy się nie uruchomił</div>}
        </div>
      </div>
    </div>
  );
}
