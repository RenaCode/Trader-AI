/* Zakladka STAN: co dzieje sie teraz. */
import React from 'react';
import { useApi } from '../utils/api';
import { AreaChart } from '../charts';
import KapitalLaczny from './KapitalLaczny';
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
  // Brak punktu kapitalu w bazie: API podstawia wtedy 0.0 za kazda kwote
  // (routers/overview.py:56-69), a zero wygladalo jak zmierzony stan rachunku -
  // razem z zielonym "+0,00%". Brak pomiaru pokazujemy jako "—".
  const hasPoint = !!o?.as_of;
  const beat = o?.heartbeat;
  const beatAt = beat?.last_beat_at;
  const beatAge = beatAt ? (Date.now() - new Date(beatAt).getTime()) / 1000 : null;
  // SILNIK ZATRZYMANY ŚWIADOMIE TO NIE JEST MARTWY SILNIK.
  //
  // `halted` ustawia człowiek albo zasiew demo (`detail.source = seed_demo`) —
  // silnik equity nigdy nie został uruchomiony, więc jego heartbeat jest sprzed
  // miesiąca i dead-man switch malował zakładkę na czerwono „BRAK SYGNAŁU”.
  // Czujka żywotności rozróżnia te stany od 12.09 („pomijam equity/demo: halted
  // — zatrzymany świadomie”), interfejs nie. Alarm o awarii, której nie ma,
  // uczy ignorować alarm o awarii, która będzie.
  const beatHalted = beat?.status === 'halted';
  // Silnik, który NIGDY nie zgłosił pulsu, nie ma wiersza w risk.engine_heartbeat
  // (albo ma go bez daty) — a wtedy nie wiadomo nawet, czy został zatrzymany
  // świadomie. Dead-man switch liczony z wieku pulsu z definicji go omijał:
  // wiek był `null`, więc badge pokazywał „—" i wyglądało to na stan znany.
  const beatMissing = !!o && !beatHalted && beatAge == null;
  // Dead-man switch: brak heartbeatu > 5 min traktujemy jako silnik nieżywy,
  // niezależnie od tego, co deklaruje kolumna status — ale NIE gdy jest
  // zatrzymany świadomie.
  const beatStale = !beatHalted && (beatMissing || (beatAge != null && beatAge > 300));

  return (
    <div className="grid" style={{ gap: 16 }}>
      {ov.error && (
        <div className="notice">
          Nie udało się wczytać stanu rachunku: {ov.error}
          <br />Liczby poniżej pochodzą z ostatniego udanego odczytu albo z niego nie pochodzą wcale.
        </div>
      )}

      <div className="grid grid-4">
        <Stat label="Kapitał" value={hasPoint ? fmtMoney(o.equity) : '—'}
              sub={hasPoint ? `na ${fmtTime(o.as_of)}` : 'brak punktu kapitału'} />
        <Stat label="Zmiana 24h" value={hasPoint ? fmtPct(o.change_24h_pct) : '—'}
              tone={hasPoint ? (o.change_24h_pct >= 0 ? 'profit' : 'loss') : ''}
              sub={hasPoint ? `od startu ${fmtPct(o.change_total_pct)}` : ''} />
        <Stat label="Ekspozycja" value={hasPoint ? fmtMoney(o.exposure) : '—'}
              sub={hasPoint ? `gotówka ${fmtMoney(o.cash)}` : ''} />
        <Stat label="Zrealizowany P&L" value={hasPoint ? fmtMoney(o.realized_cum) : '—'}
              tone={hasPoint ? (o.realized_cum >= 0 ? 'profit' : 'loss') : ''}
              sub={hasPoint ? `niezrealizowany ${fmtMoney(o.unrealized)}` : ''} />
      </div>

      <KapitalLaczny days={45} />

      <div className="glass-card">
        <div className="card-title">
          Krzywa kapitału
          <span className="hint">tryb demo · 45 dni</span>
        </div>
        {curve.error ? (
          <div className="notice">Nie udało się wczytać krzywej kapitału: {curve.error}</div>
        ) : curve.loading ? (
          <div className="empty loading-pulse">Wczytywanie…</div>
        ) : (
          <AreaChart
            data={curve.data || []}
            valueKey="equity"
            height={240}
            valueFmt={(v) => fmtMoney(v)}
            labelFmt={(d) => fmtTime(d.t)}
            color={hasPoint && o.change_total_pct < 0 ? 'var(--loss)' : 'var(--profit)'}
          />
        )}
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
                  {beatMissing ? 'BRAK PULSU'
                    : beatStale ? 'BRAK SYGNAŁU'
                      : beatHalted ? 'NIE URUCHOMIONY'
                        : (beat?.status || '—').toUpperCase()}
                </span>
              </div>
              <div className="row-meta">
                <span>ostatni heartbeat: {fmtAgo(beatAt)}</span>
              </div>
              {beatHalted && (
                <div className="row-body">
                  Silnik tego rynku jest zatrzymany świadomie — nie podejmuje decyzji
                  i nie składa zleceń. Dane rynkowe i kapitał rachunku zbierają się dalej.
                </div>
              )}
              {beatMissing ? (
                <div className="row-body" style={{ color: 'var(--danger-light)' }}>
                  Silnik nigdy nie zgłosił pulsu — nie ma wpisu, z którego dałoby się
                  odczytać, czy pracuje. To nie to samo co zatrzymanie świadome:
                  tamto jest zapisane w bazie i widać je wprost.
                </div>
              ) : beatStale && (
                <div className="row-body" style={{ color: 'var(--danger-light)' }}>
                  Silnik nie zgłosił się od ponad 5 minut. Nic tego NIE ZATRZYMUJE
                  automatycznie: zlecenia nie są wstrzymywane, a ten wiersz jest
                  jedynym sygnałem. Sprawdź, czy pętla decyzyjna żyje.
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
            <span className="hint">{pos.error ? '—' : `${pos.data?.length || 0} aktywnych`}</span>
          </div>
          {pos.error ? (
            <div className="notice">Nie udało się wczytać pozycji: {pos.error}</div>
          ) : pos.loading ? (
            <div className="empty loading-pulse">Wczytywanie…</div>
          ) : pos.data?.length ? (
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
        {recent.error ? (
          <div className="notice">Nie udało się wczytać decyzji: {recent.error}</div>
        ) : recent.loading ? (
          <div className="empty loading-pulse">Wczytywanie…</div>
        ) : recent.data?.length ? (
          <div className="row-list" style={{ maxHeight: 300 }}>
            {recent.data.map((d) => (
              <div key={d.id} className="row-item">
                <div className="row-head">
                  <span className="row-title">
                    {d.symbol} · {ACTION_PL[d.action] || d.action}
                  </span>
                  <span className={`badge ${d.risk_verdict === 'allow' ? 'ok' : d.risk_verdict === 'block' ? 'bad' : 'warn'}`}>
                    {d.confidence == null ? 'pewność —' : `${Math.round(d.confidence * 100)}% pewności`}
                  </span>
                </div>
                <div className="row-meta">
                  <span>{fmtTime(d.at)}</span>
                  <span>{d.model}</span>
                  {/* `&&` na liczbie wypuszczalo samo 0 jako luzny tekst obok czasu. */}
                  {d.latency_ms != null && <span>{d.latency_ms} ms</span>}
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
