/* Zakladka STAN: co dzieje sie teraz.

   Uklad pulpitu: kolumna kafli kapitalu, w srodku krzywa kapitalu, z prawej
   stan silnika, pod spodem zlecenia, pozycje i metryki modelu. Kazda liczba
   pochodzi z API; czego API nie mierzy (swiece, ceny na zywo, VaR), tego tu
   nie ma. */
import React, { useState } from 'react';
import { useApi } from '../utils/api';
import { AreaChart } from '../charts';
import KapitalLaczny from './KapitalLaczny';
import {
  fmtMoney, fmtPct, fmtTime, fmtAgo, fmtNum,
  STATE_PL, STATE_TONE, ACTION_PL,
} from '../utils/format';

function Stat({ label, value, sub, tone, size, card = '', pill }) {
  return (
    <div className={`glass-card tight stat ${card}`}>
      <span className="stat-label">
        {label}
        {pill}
      </span>
      <span className={`stat-value ${size || ''} ${tone || ''}`}>{value}</span>
      {sub && <span className="stat-sub">{sub}</span>}
    </div>
  );
}

/* Zakres krzywej kapitalu. To dni historii, nie interwal swiecy - API oddaje
   punkty kapitalu, nie swiece OHLC. */
const RANGES = [7, 45, 90];

export default function Overview({ engine }) {
  const ov = useApi(`/api/engines/${engine}/overview`, [engine], { refreshMs: 20000 });
  const [days, setDays] = useState(45);
  const curve = useApi(`/api/engines/${engine}/equity-curve?days=${days}`, [engine, days]);
  const pos = useApi(`/api/engines/${engine}/positions`, [engine], { refreshMs: 20000 });
  const recent = useApi(`/api/engines/${engine}/decisions?limit=6`, [engine], { refreshMs: 20000 });
  const planned = useApi(`/api/engines/${engine}/planned`, [engine], { refreshMs: 30000 });
  const st = useApi(`/api/engines/${engine}/learning/state`, [engine], { refreshMs: 30000 });
  const s = st.data;

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
  // TRZY CYKLE, NIE JEDEN. 900 s = 3 x `--interval 300` silnika, czyli ta sama
  // regula, ktora `dozor.py:203` zapisal dla `MARTWY_PO_MIN = 15` minut: prog
  // dwoch lezy na gornej granicy normalnego wahania. Do 2026-09-26 stalo tu 300,
  // czyli DOKLADNIE jeden cykl - panel malowal na czerwono kazde pojedyncze
  // opoznienie, a alarm, ktory zapala sie przy normalnej pracy, uczy go pomijac.
  const beatStale = !beatHalted && (beatMissing || (beatAge != null && beatAge > 900));

  const pnlTone = !hasPoint ? '' : o.realized_cum >= 0 ? 'profit' : 'loss';

  return (
    <div className="grid" style={{ gap: 16 }}>
      {ov.error && (
        <div className="notice">
          Nie udało się wczytać stanu rachunku: {ov.error}
          <br />Liczby poniżej pochodzą z ostatniego udanego odczytu albo z niego nie pochodzą wcale.
        </div>
      )}

      <div className="dash">
        <div className="dash-stats">
          <Stat label="Kapitał" size="lg" card="accent"
                value={hasPoint ? fmtMoney(o.equity) : '—'}
                sub={hasPoint
                  ? <><span className={o.change_total_pct == null ? 'dim' : o.change_total_pct >= 0 ? 'profit' : 'loss'}>
                      {fmtPct(o.change_total_pct)}</span> od startu · na {fmtTime(o.as_of)}</>
                  : 'brak punktu kapitału'} />
          <Stat label="Zrealizowany P&L" size="lg" card={pnlTone ? `tint-${pnlTone}` : ''}
                value={hasPoint ? fmtMoney(o.realized_cum) : '—'} tone={pnlTone}
                sub={hasPoint ? (
                  <span className={`pill ${o.unrealized == null ? '' : o.unrealized >= 0 ? 'profit' : 'loss'}`}>
                    niezrealizowany {fmtMoney(o.unrealized)}
                  </span>
                ) : ''} />
          <div className="dash-pair">
            <Stat label="Zmiana 24h" value={hasPoint ? fmtPct(o.change_24h_pct) : '—'}
                  tone={hasPoint && o.change_24h_pct != null ? (o.change_24h_pct >= 0 ? 'profit' : 'loss') : ''} />
            <Stat label="Gotówka" value={hasPoint ? fmtMoney(o.cash) : '—'} size="sm" />
          </div>
          <Stat label="Ekspozycja" value={hasPoint ? fmtMoney(o.exposure) : '—'}
                sub={hasPoint ? 'wartość otwartych pozycji' : ''} />
        </div>

        <div className="glass-card dash-chart">
          <div className="card-title">
            <span className="chart-head">
              Krzywa kapitału
              {hasPoint && <span className="chart-head-value mono">{fmtMoney(o.equity)}</span>}
            </span>
            <span className="segmented" role="group" aria-label="Zakres krzywej kapitału">
              {RANGES.map((d) => (
                <button key={d} type="button" aria-pressed={days === d}
                        className={days === d ? 'on' : ''} onClick={() => setDays(d)}>
                  {d} dni
                </button>
              ))}
            </span>
          </div>
          <div className="hint chart-sub">tryb demo · {days} dni · punkty kapitału rachunku</div>
          {curve.error ? (
            <div className="notice">Nie udało się wczytać krzywej kapitału: {curve.error}</div>
          ) : curve.loading ? (
            <div className="empty loading-pulse">Wczytywanie…</div>
          ) : (
            <AreaChart
              data={curve.data || []}
              valueKey="equity"
              height={320}
              valueFmt={(v) => fmtMoney(v)}
              labelFmt={(d) => fmtTime(d.t)}
              color={hasPoint && o.change_total_pct < 0 ? 'var(--loss)' : 'var(--profit)'}
            />
          )}
        </div>

        <div className="glass-card dash-side">
          <div className="card-title">
            Stan silnika
            <span className="hint">odświeżane co 20 s</span>
          </div>
          <div className="tile-stack">
            <div className="row-item tile">
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
                <span>ostatni heartbeat: <span className="mono">{fmtAgo(beatAt)}</span></span>
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

            <div className="row-item tile">
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

        <div className="glass-card dash-orders">
          <div className="card-title">
            Planowane zlecenia
            <span className="hint">{planned.error ? '—' : `${planned.data?.length || 0} oczekuje`}</span>
          </div>
          {planned.error ? (
            <div className="notice">Nie udało się wczytać planowanych zleceń: {planned.error}</div>
          ) : planned.loading ? (
            <div className="empty loading-pulse">Wczytywanie…</div>
          ) : planned.data?.length ? (
            <div className="line-list">
              <div className="line-head"><span>Zlecenie</span><span>Status</span></div>
              {planned.data.map((p) => (
                <div key={p.id} className="line">
                  <span className="line-main">
                    <strong>{p.symbol}</strong>{' '}
                    <span className={p.side === 'buy' ? 'profit' : 'loss'}>
                      {p.side === 'buy' ? 'kupno' : 'sprzedaż'} {p.order_type}
                    </span>
                    <span className="line-sub mono">
                      {fmtNum(p.qty, 4)}{p.limit_price != null && ` @ ${fmtMoney(p.limit_price)}`}
                    </span>
                  </span>
                  <span className="pill">oczekuje</span>
                </div>
              ))}
            </div>
          ) : <div className="empty">Brak zaplanowanych zleceń</div>}
        </div>

        <div className="glass-card dash-positions">
          <div className="card-title">
            Otwarte pozycje
            <span className="hint">{pos.error ? '—' : `${pos.data?.length || 0} aktywnych`}</span>
          </div>
          {pos.error ? (
            <div className="notice">Nie udało się wczytać pozycji: {pos.error}</div>
          ) : pos.loading ? (
            <div className="empty loading-pulse">Wczytywanie…</div>
          ) : pos.data?.length ? (
            <div className="line-list">
              <div className="line-head"><span>Pozycja</span><span>Kierunek</span></div>
              {pos.data.map((p) => (
                <div key={p.id} className="line">
                  <span className="line-main">
                    <strong>{p.symbol}</strong>
                    <span className="line-sub mono">
                      {fmtNum(p.qty, 4)} @ {fmtMoney(p.avg_price)}
                    </span>
                    <span className="line-sub">otwarta {fmtAgo(p.opened_at)}</span>
                  </span>
                  <span className={`pill solid ${p.side === 'long' ? 'profit' : 'loss'}`}>{p.side}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="empty">Brak otwartych pozycji</div>
          )}
        </div>

        <div className="glass-card dash-risk">
          <div className="card-title">
            Metryki modelu
            <span className="hint">out-of-sample</span>
          </div>
          {st.error ? (
            <div className="notice">Nie udało się wczytać stanu nauki: {st.error}</div>
          ) : st.loading ? (
            <div className="empty loading-pulse">Wczytywanie…</div>
          ) : (
            <dl className="kv">
              <div>
                <dt>Najgorszy drawdown</dt>
                <dd className={`mono ${s?.max_drawdown != null ? 'loss' : 'dim'}`}>
                  {s?.max_drawdown != null ? `${fmtNum(s.max_drawdown, 2)}%` : '—'}
                </dd>
              </div>
              <div>
                <dt>Mediana Sharpe / transakcję</dt>
                <dd className={`mono ${s?.median_oos_sharpe == null ? 'dim' : s.median_oos_sharpe > 0 ? 'profit' : 'loss'}`}>
                  {fmtNum(s?.median_oos_sharpe, 3)}
                </dd>
              </div>
              <div>
                <dt>Okna dodatnie</dt>
                <dd className={`mono ${s?.windows_total == null ? 'dim' : ''}`}>
                  {s?.windows_total != null
                    ? `${s.windows_positive}/${s.windows_total} (${fmtNum(s.windows_positive_pct, 1)}%)`
                    : '—'}
                </dd>
              </div>
            </dl>
          )}
        </div>
      </div>

      <KapitalLaczny days={45} />

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
