/* Zakladka ZRODLA: z jakich stron system korzysta i co z nich wyciagnal. */
import React from 'react';
import { useApi } from '../utils/api';
import { fmtTime, fmtAgo, fmtLag, DIRECTION_PL } from '../utils/format';

export default function Sources({ engine }) {
  const feeds = useApi(`/api/feeds?engine=${engine}`, [engine], { refreshMs: 60000 });
  const news = useApi(`/api/news?engine=${engine}&limit=40`, [engine], { refreshMs: 60000 });
  const spend = useApi('/api/api-spend?dni=30', [], { refreshMs: 60000 });

  const s = spend.data;
  const wykorzystanie = s ? Math.min(1, s.dzis.wykorzystanie || 0) : 0;
  const perDzien = {};
  (s?.dobowe || []).forEach((d) => {
    perDzien[d.dzien] = (perDzien[d.dzien] || 0) + d.koszt_usd;
  });
  const dni = Object.entries(perDzien).sort((a, b) => (a[0] < b[0] ? 1 : -1)).slice(0, 14);
  const maxDzien = Math.max(0.0001, ...dni.map(([, v]) => v));

  return (
    <div className="grid" style={{ gap: 16 }}>
      <div className="glass-card">
        <div className="card-title">
          Koszt analizy AI
          <span className="hint">
            {s ? `${s.koszt_okresu_usd.toFixed(2)} USD przez ${s.okres_dni} dni` : '—'}
          </span>
        </div>
        {!s || s.dzis.wywolania === 0 ? (
          <div className="empty">
            Brak wywołań modelu. Skaner wiadomości rusza po wpisaniu klucza Anthropic
            albo Google Gemini w Ustawieniach.
          </div>
        ) : (
          <>
            <div className="kpi-row">
              <div className="kpi">
                <div className="kpi-label">Dziś</div>
                <div className="kpi-value">{s.dzis.koszt_usd.toFixed(3)} USD</div>
                <div className="kpi-sub">{s.dzis.wywolania} wywołań</div>
              </div>
              <div className="kpi">
                <div className="kpi-label">Limit dobowy</div>
                <div className="kpi-value">{s.limit_dobowy_usd.toFixed(2)} USD</div>
                <div className="kpi-sub">twardy stop po przekroczeniu</div>
              </div>
              <div className="kpi">
                <div className="kpi-label">Wykorzystanie</div>
                <div className={`kpi-value ${wykorzystanie > 0.8 ? 'neg' : ''}`}>
                  {(wykorzystanie * 100).toFixed(0)}%
                </div>
                <div className="kpi-sub">
                  {wykorzystanie >= 1 ? 'analiza wstrzymana do północy UTC' : 'w normie'}
                </div>
              </div>
            </div>
            {dni.length > 1 && (
              <div className="spend-bars">
                {dni.slice().reverse().map(([dzien, koszt]) => (
                  <div className="spend-bar" key={dzien} title={`${dzien}: ${koszt.toFixed(4)} USD`}>
                    <div className="spend-fill" style={{ height: `${(koszt / maxDzien) * 100}%` }} />
                    <div className="spend-day">{dzien.slice(5)}</div>
                  </div>
                ))}
              </div>
            )}
            <div className="hint" style={{ marginTop: 10 }}>
              Tani model odsiewa większość artykułów, drogi analizuje resztę — dlatego
              liczba wywołań jest znacznie większa niż liczba analiz.
            </div>
          </>
        )}
      </div>
      <div className="glass-card">
        <div className="card-title">
          Źródła danych
          <span className="hint">{feeds.data?.length || 0} kanałów RSS</span>
        </div>
        {feeds.data?.length ? (
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr><th>Źródło</th><th>Rynek</th><th>Status</th><th>Ostatni odczyt</th>
                    <th>Co N</th><th>Zachowane / pobrane</th><th>24h</th></tr>
              </thead>
              <tbody>
                {feeds.data.map((f) => (
                  <tr key={f.id}>
                    <td>
                      <div style={{ fontWeight: 600 }}>{f.name}</div>
                      <a className="src-link" href={f.url} target="_blank" rel="noopener noreferrer">{f.url}</a>
                    </td>
                    <td className="dim">{f.engine || 'makro'}</td>
                    <td>
                      <span className={`badge ${f.last_status === 'ok' ? 'ok' : f.last_status ? 'bad' : ''}`}>
                        {f.last_status || 'brak'}
                      </span>
                    </td>
                    <td className="dim">{fmtAgo(f.last_polled_at)}</td>
                    <td className="mono dim">{f.poll_interval_s}s</td>
                    <td className="mono">
                      {f.articles_kept} / {f.articles_seen}
                      {f.keep_rate != null && (
                        <span className="dim"> ({(f.keep_rate * 100).toFixed(0)}%)</span>
                      )}
                    </td>
                    <td className="mono">{f.last_24h}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <div className="empty">Brak skonfigurowanych źródeł</div>}
        <div className="row-body" style={{ marginTop: 10 }}>
          Niski odsetek zachowanych artykułów jest oczekiwany, nie alarmujący — tani model
          odsiewa szum, a droższy analizuje tylko to, co przeszło filtr istotności.
        </div>
      </div>

      <div className="glass-card">
        <div className="card-title">
          Przeanalizowane artykuły <span className="badge ai">Claude</span>
          <span className="hint">najnowsze {news.data?.length || 0}</span>
        </div>
        {news.data?.length ? (
          <div className="row-list" style={{ maxHeight: 620 }}>
            {news.data.map((n) => (
              <div key={n.id} className="row-item">
                <div className="row-head">
                  <span className="row-title" style={{ fontSize: '0.87rem' }}>{n.title}</span>
                  {n.direction && (
                    <span className={`badge ${n.direction === 'bullish' ? 'ok' : n.direction === 'bearish' ? 'bad' : ''}`}>
                      {DIRECTION_PL[n.direction]}
                    </span>
                  )}
                </div>
                <div className="row-meta">
                  <span style={{ color: 'var(--color-blue)' }}>{n.source}</span>
                  <span>{fmtTime(n.at)}</span>
                  {/* Opoznienie miedzy publikacja a poznaniem przez system.
                      Decyzje wczesniejsze niz to nie mogly tego newsa widziec. */}
                  <span title="opóźnienie: publikacja → system">opóźnienie {fmtLag(n.lag_s)}</span>
                  {n.symbols?.length > 0 && <span className="mono">{n.symbols.join(', ')}</span>}
                  <a className="src-link" href={n.url} target="_blank" rel="noopener noreferrer">źródło ↗</a>
                </div>
                {n.magnitude != null && (
                  <div style={{ display: 'flex', gap: 16, marginTop: 8, fontSize: '0.74rem' }}>
                    {[
                      ['siła', n.magnitude, 'var(--color-primary)'],
                      ['pewność', n.confidence, 'var(--color-blue)'],
                      ['nowość', n.novelty, 'var(--color-orange)'],
                    ].map(([lbl, val, col]) => (
                      <div key={lbl} style={{ flex: 1 }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 3 }}>
                          <span className="dim">{lbl}</span>
                          <span className="mono" style={{ color: col }}>{Math.round((val ?? 0) * 100)}%</span>
                        </div>
                        <div className="bar-track" style={{ height: 4 }}>
                          <div className="bar-fill" style={{ width: `${(val ?? 0) * 100}%`, background: col }} />
                        </div>
                      </div>
                    ))}
                  </div>
                )}
                {n.horizon && (
                  <div className="row-meta" style={{ marginTop: 6 }}>
                    <span className="dim">horyzont: {n.horizon}</span>
                    <span className="dim">model: {n.model}</span>
                  </div>
                )}
              </div>
            ))}
          </div>
        ) : <div className="empty">Brak przeanalizowanych artykułów</div>}
        <div className="row-body" style={{ marginTop: 10 }}>
          <strong>Nowość</strong> jest osobnym wymiarem od siły sygnału: news, który rynek już
          wycenił, ma dla systemu wartość bliską zeru niezależnie od tego, jak mocno brzmi.
        </div>
      </div>
    </div>
  );
}
