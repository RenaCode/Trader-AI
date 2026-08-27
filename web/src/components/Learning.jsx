/* Zakladka STOPIEN NAUKI.
   Wszystko tutaj jest OUT-OF-SAMPLE. Celowo nie ma tu "wyniku backtestu" - swietny
   backtest jest domyslnie sygnalem przeuczenia, wiec nie ma go czym pokazac. */
import React from 'react';
import { useApi } from '../utils/api';
import { BarChart, CalibrationChart } from '../charts';
import { fmtNum, fmtTime, fmtDate, STATE_PL, REGIME_PL } from '../utils/format';

const THRESHOLD_PCT = 70; // Prog wyjscia z demo na live.

function Metric({ label, value, tone, hint }) {
  return (
    <div className="glass-card tight stat">
      <span className="stat-label">{label}</span>
      <span className={`stat-value sm ${tone || ''}`}>{value}</span>
      {hint && <span className="stat-sub dim">{hint}</span>}
    </div>
  );
}

export default function Learning({ engine }) {
  const st = useApi(`/api/engines/${engine}/learning/state`, [engine], { refreshMs: 30000 });
  const wf = useApi(`/api/engines/${engine}/learning/walk-forward?limit=40`, [engine]);
  const cal = useApi(`/api/engines/${engine}/learning/calibration`, [engine]);
  const drift = useApi(`/api/engines/${engine}/learning/drift`, [engine]);
  const perf = useApi(`/api/engines/${engine}/learning/signal-performance`, [engine]);
  const pm = useApi(`/api/engines/${engine}/learning/postmortems?limit=3`, [engine]);
  const wag = useApi(`/api/engines/${engine}/learning/weights`, [engine], { refreshMs: 60000 });

  const s = st.data;
  const pct = s?.windows_positive_pct ?? 0;
  const passes = pct >= THRESHOLD_PCT;

  const bars = (wf.data || []).map((w) => ({
    label: `okno ${w.idx} · ${fmtDate(w.test_from)}–${fmtDate(w.test_to)} · ${w.n_trades} transakcji`,
    v: w.metrics?.sharpe ?? 0,
    regime: REGIME_PL[w.regime] || w.regime,
  }));

  return (
    <div className="grid" style={{ gap: 16 }}>
      <div className="notice info">
        Wszystkie metryki na tej zakładce pochodzą <strong>wyłącznie z okien testowych</strong>{' '}
        walk-forward — z danych, których model nie widział podczas treningu. Nie ma tu wyniku
        backtestu, bo wysoki wynik backtestu jest sygnałem przeuczenia, nie skuteczności.
      </div>

      <div className="grid grid-4">
        <Metric label="Stan" value={STATE_PL[s?.status] || '—'} tone={s?.status === 'validated' ? 'profit' : ''}
                hint={s?.model || 'brak aktywnego modelu'} />
        <Metric label="Okna dodatnie (OOS)" value={s ? `${s.windows_positive}/${s.windows_total}` : '—'}
                tone={passes ? 'profit' : 'loss'} hint={`${fmtNum(pct, 1)}% · próg ${THRESHOLD_PCT}%`} />
        <Metric label="Mediana Sharpe / transakcję" value={fmtNum(s?.median_oos_sharpe, 3)}
                tone={(s?.median_oos_sharpe ?? 0) > 0 ? 'profit' : 'loss'} hint="out-of-sample, bez anualizacji" />
        <Metric label="Najgorszy drawdown" value={s?.max_drawdown != null ? `${fmtNum(s.max_drawdown, 2)}%` : '—'}
                tone="loss" hint="w oknie testowym" />
      </div>

      <div className="glass-card">
        <div className="card-title">
          Czego algorytm się nauczył
          <span className="hint">
            {wag.data?.wyuczone
              ? `wersja modelu ${wag.data.wersja_modelu} · świeca ${wag.data.swieca}`
              : 'brak zwalidowanego modelu'}
          </span>
        </div>
        {!wag.data ? (
          <div className="empty">Wczytywanie…</div>
        ) : !wag.data.wyuczone ? (
          <div className="empty">
            Silnik pracuje na jawnych założeniach początkowych. Żadna wersja modelu
            nie została jeszcze promowana, więc nie ma czego porównywać — i tak
            należy czytać każdą decyzję.
          </div>
        ) : (
          <>
            <div className="hint" style={{ marginBottom: 12 }}>
              Metryki mówią, <em>czy</em> wynik jest lepszy. Ta tabela mówi, <em>co</em>{' '}
              model zmienił: któremu sygnałowi w którym reżimie zaczął ufać bardziej,
              a któremu mniej. Próg wejścia: {wag.data.prog_wejscia?.toFixed?.(4) ?? '—'}.
            </div>
            <div className="regime-grid">
              {wag.data.rezimy.map((r) => (
                <div className="regime-box" key={r.rezim}>
                  <div className="regime-name">{r.rezim}</div>
                  {r.sygnaly.map((x) => (
                    <div className="weight-row" key={x.sygnal} title={x.etykieta}>
                      <span className="weight-label">{x.etykieta}</span>
                      <span className="mono weight-val">{x.wyuczona.toFixed(3)}</span>
                      <span className={`mono weight-delta ${x.zmiana > 0.005 ? 'pos' : x.zmiana < -0.005 ? 'neg' : 'dim'}`}>
                        {x.zmiana > 0 ? '+' : ''}{x.zmiana.toFixed(3)}
                      </span>
                    </div>
                  ))}
                </div>
              ))}
            </div>
            <div className="hint" style={{ marginTop: 10 }}>
              Liczba po prawej to różnica względem założenia początkowego. Wagi w każdym
              reżimie sumują się do jedności, więc wzrost jednego sygnału zawsze odbywa
              się kosztem pozostałych.
            </div>
          </>
        )}
      </div>

      <div className="glass-card">
        <div className="card-title">
          Postęp do progu przejścia na live
          <span className="hint">wymagane: {THRESHOLD_PCT}% okien OOS na plusie</span>
        </div>
        <div style={{ position: 'relative' }}>
          <div className="bar-track" style={{ height: 12 }}>
            <div className="bar-fill" style={{
              width: `${Math.min(100, pct)}%`,
              background: passes ? 'var(--gradient-profit), var(--profit)' : 'var(--color-orange)',
              backgroundColor: passes ? 'var(--profit)' : 'var(--color-orange)',
            }} />
          </div>
          <div style={{
            position: 'absolute', left: `${THRESHOLD_PCT}%`, top: -4, bottom: -4,
            width: 2, background: 'var(--text-muted)',
          }} />
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 8, fontSize: '0.78rem' }}>
          <span className="mono" style={{ color: passes ? 'var(--profit)' : 'var(--color-orange)' }}>
            {fmtNum(pct, 1)}%
          </span>
          <span className="dim">{s?.reason}</span>
        </div>
      </div>

      <div className="glass-card">
        <div className="card-title">
          Sharpe na transakcję, per okno walk-forward
          <span className="hint">out-of-sample · najstarsze po lewej</span>
        </div>
        <BarChart data={bars} height={210} valueFmt={(v) => v.toFixed(3)} />
      </div>

      <div className="grid grid-2">
        <div className="glass-card">
          <div className="card-title">
            Kalibracja pewności
            <span className="hint">błąd ECE: {fmtNum(s?.calibration_error, 3)}</span>
          </div>
          <CalibrationChart data={cal.data?.kubelki || []} />
          {cal.data?.podsumowanie && (
            <div className="verdict" style={{ marginTop: 10 }}>
              <div className="verdict-line">{cal.data.podsumowanie.werdykt}</div>
              <div className="hint" style={{ marginTop: 6 }}>
                {cal.data.podsumowanie.transakcji.toLocaleString('pl')} transakcji ·
                {' '}trafność {(cal.data.podsumowanie.trafnosc * 100).toFixed(1)}% ·
                {' '}rozpiętość między kubełkami{' '}
                {cal.data.podsumowanie.rozpietosc_trafnosci != null
                  ? `${(cal.data.podsumowanie.rozpietosc_trafnosci * 100).toFixed(1)} pkt proc.`
                  : '—'}
              </div>
            </div>
          )}
          <div className="row-body" style={{ marginTop: 8 }}>
            Punkty poniżej przekątnej oznaczają model <strong>zbyt pewny siebie</strong>: deklaruje
            70% pewności, a trafia rzadziej. Ale ważniejsze od odległości od przekątnej jest to,
            czy trafność w ogóle <strong>rośnie</strong> wraz z pewnością — jeśli jest płaska,
            deklarowana pewność nie mówi nic o jakości sytuacji.
          </div>
        </div>

        <div className="glass-card">
          <div className="card-title">
            Dryf rozkładu cech (PSI)
            <span className="hint">próg alarmowy: 0,25</span>
          </div>
          {drift.data?.length ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 9 }}>
              {drift.data.map((d) => {
                const w = Math.min(100, (d.psi / 0.4) * 100);
                const tone = d.psi > 0.25 ? 'var(--loss)' : d.psi > 0.15 ? 'var(--color-orange)' : 'var(--profit)';
                return (
                  <div key={d.feature}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.77rem', marginBottom: 4 }}>
                      <span className="mono" style={{ color: 'var(--text-muted)' }}>{d.feature}</span>
                      <span className="mono" style={{ color: tone }}>{fmtNum(d.psi, 3)}</span>
                    </div>
                    <div className="bar-track"><div className="bar-fill" style={{ width: `${w}%`, background: tone }} /></div>
                  </div>
                );
              })}
              <div className="row-body">
                PSI mierzy, jak bardzo bieżący rozkład cechy odjechał od tego, na którym model
                był trenowany. Rosnący dryf zapowiada spadek skuteczności <em>zanim</em> pojawi
                się on w wyniku.
              </div>
            </div>
          ) : <div className="empty">Brak pomiarów dryfu</div>}
        </div>
      </div>

      <div className="grid grid-2">
        <div className="glass-card">
          <div className="card-title">
            Skuteczność sygnałów
            <span className="hint">która hipoteza faktycznie coś wnosi</span>
          </div>
          {perf.data?.length ? (
            <div className="table-scroll">
              <table className="table">
                <thead><tr><th>Sygnał</th><th>Rodzina</th><th>Użyć</th><th>Trafność</th><th>P&L</th></tr></thead>
                <tbody>
                  {perf.data.map((p) => (
                    <tr key={p.name}>
                      <td className="mono" style={{ fontSize: '0.78rem' }}>{p.name}</td>
                      <td className="dim">{p.family}</td>
                      <td className="mono">{p.used_count}</td>
                      <td className="mono">{p.hit_rate != null ? `${(p.hit_rate * 100).toFixed(0)}%` : '—'}</td>
                      <td className={`mono ${p.total_pnl >= 0 ? 'profit' : 'loss'}`}>{fmtNum(p.total_pnl, 0)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <div className="empty">Brak danych o sygnałach</div>}
        </div>

        <div className="glass-card">
          <div className="card-title">
            Uczciwość pomiaru
            <span className="hint">licznik przetestowanych hipotez</span>
          </div>
          <div className="stat" style={{ marginBottom: 12 }}>
            <span className="stat-value">{s?.trials_run ?? '—'}</span>
            <span className="stat-sub">konfiguracji przetestowanych na tym modelu</span>
          </div>
          <div className="row-body">
            Ten licznik istnieje, żeby nie dało się nas oszukać selekcją. Im więcej konfiguracji
            przetestowano, tym łatwiej trafić dobry wynik przypadkiem — i tym mocniej trzeba
            zdyskontować Sharpe'a. Bez tej liczby <em>deflated Sharpe</em> jest nieobliczalny.
          </div>
          <div className="row-body">
            Model trenowany na danych {fmtDate(s?.trained_from)} – {fmtDate(s?.trained_to)}.
            Ostatnia aktualizacja stanu: {fmtTime(s?.updated_at)}.
          </div>
        </div>
      </div>

      <div className="glass-card">
        <div className="card-title">
          Post-mortem <span className="badge ai">Claude</span>
          <span className="hint">dziennik jakościowy — nie zmienia wag automatycznie</span>
        </div>
        {pm.data?.length ? (
          <div className="row-list">
            {pm.data.map((p) => (
              <div key={p.id} className="row-item">
                <div className="row-head">
                  <span className="row-title">{fmtDate(p.period_from)} – {fmtDate(p.period_to)}</span>
                  <span className="badge">{p.model}</span>
                </div>
                <div className="row-body">{p.summary}</div>
                {Array.isArray(p.findings) && p.findings.map((f, i) => (
                  <div key={i} className="row-meta" style={{ marginTop: 6 }}>
                    <span style={{ color: 'var(--color-orange)' }}>▸ {f.finding}</span>
                    <span className="dim">→ {f.action}</span>
                  </div>
                ))}
              </div>
            ))}
          </div>
        ) : <div className="empty">Brak analiz post-mortem</div>}
      </div>
    </div>
  );
}
