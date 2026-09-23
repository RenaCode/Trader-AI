/* Zakladka ZRODLA: z jakich stron system korzysta i co z nich wyciagnal. */
import React from 'react';
import { useApi } from '../utils/api';
import { fmtTime, fmtAgo, fmtLag, DIRECTION_PL } from '../utils/format';

/* Pomiar, ktorego nie ma, nie jest zerem. `val ?? 0` rysowalo "pewnosc 0%"
   dla artykulu bez analizy (LEFT JOIN nie dal wiersza) - czyli twierdzilo,
   ze model ocenil artykul na zero, zamiast ze nikt go nie ocenial. */
const fmtOcena = (v) => (v == null ? '—' : `${Math.round(v * 100)}%`);

export default function Sources({ engine }) {
  const feeds = useApi(`/api/feeds?engine=${engine}`, [engine], { refreshMs: 60000 });
  const news = useApi(`/api/news?engine=${engine}&limit=40`, [engine], { refreshMs: 60000 });
  const spend = useApi('/api/api-spend?dni=30', [], { refreshMs: 60000 });
  /* Czy klucz analizy w ogole jest wpisany, rozstrzyga SERWER - ta sama
     funkcja, ktorej uzywa worker. Front wypisywal dotad instrukcje "wpisz
     klucz" na podstawie samego zera wywolan, nie sprawdzajac, czy klucz jest.
     Przy wpisanym kluczu i pustym koncie u dostawcy (przezyta awaria
     z 09.09.2026) wlasciciel czytal polecenie wpisania klucza, ktory juz
     wpisal - a prawdziwa przyczyna byla zupelnie gdzie indziej.

     BEZ ODSWIEZANIA W PETLI: ten endpoint buduje i zamyka dostawce przy kazdym
     wywolaniu (patrz komentarz w routers/settings.py), a zbior wpisanych kluczy
     zmienia sie raz na kilka miesiecy. */
  const tryb = useApi('/api/settings/analiza-tryb', []);

  const s = spend.data;
  // `wykorzystanie` jest nullem, gdy limit dobowy nie jest znany. `|| 0` dawalo
  // wtedy zielone "0% · w normie" w miejscu, w ktorym nie ma zadnego pomiaru.
  const wykorzystanieUsd = s?.dzis?.wykorzystanie != null ? Math.min(1, s.dzis.wykorzystanie) : null;
  /* DRUGA BRAMKA, KTORA ZATRZYMUJE POTOK. Worker przerywa przy przekroczeniu
     limitu kwotowego ALBO limitu LICZBY wywolan (news_worker.py:412-417).
     Karta pokazywala tylko ten pierwszy — a to on nie zadzialal ani razu:
     11.09.2026 analiza stanela calkowicie przy $0,114 z $5, zatrzymana
     darmowym progiem Gemini liczonym w zapytaniach, i pisala przy tym
     "wykorzystanie 2% · w normie".

     "W normie" liczy sie wiec z MAKSIMUM obu wykorzystan: wyczerpanie
     jednej bramki zatrzymuje analize niezaleznie od stanu drugiej. */
  const wykorzystanieWywolan = s?.dzis?.wykorzystanie_wywolan != null
    ? Math.min(1, s.dzis.wykorzystanie_wywolan) : null;
  const zmierzone = [wykorzystanieUsd, wykorzystanieWywolan].filter((v) => v != null);
  const wykorzystanie = zmierzone.length ? Math.max(...zmierzone) : null;
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
        {spend.error && (
          <div className="notice">
            <strong>Nie udało się odczytać wydatków na model:</strong> {spend.error}.
            To nie znaczy, że skaner nie pracuje — znaczy, że nie wiadomo, ile wydał.
          </div>
        )}
        {!s ? (
          <div className="empty">{spend.error ? 'Koszt analizy nieznany.' : 'Wczytywanie…'}</div>
        ) : (
          <>
            {s.dzis.wywolania === 0 && (
              /* Trzy rozne przyczyny zera, trzy rozne rzeczy do zrobienia.
                 Karta zostaje na ekranie w kazdym z tych przypadkow: wczesniej
                 znikala w calosci, a razem z nia limit dobowy i koszt z 30 dni,
                 ktore API liczy i zwraca. */
              <div className="notice" style={{ marginBottom: 14 }}>
                {tryb.data?.aktywny === false ? (
                  <>
                    <strong>Brak klucza analizy.</strong> {tryb.data.opis} Skaner wiadomości
                    rusza po wpisaniu klucza Anthropic albo Google Gemini w Ustawieniach.
                  </>
                ) : tryb.data?.aktywny ? (
                  <>
                    <strong>Klucz jest wpisany, ale dziś nie było ani jednego wywołania.</strong>{' '}
                    Dostawca: <span className="mono">{tryb.data.dostawca || 'mieszanka'}</span>.
                    Zero wywołań przy ustawionym kluczu to zwykle wyczerpany limit dobowy,
                    brak środków na koncie u dostawcy albo zatrzymany worker analizy —
                    a nie brak konfiguracji.
                  </>
                ) : (
                  <>
                    <strong>Dziś nie było wywołań modelu.</strong> Nie udało się sprawdzić,
                    czy klucz analizy jest wpisany
                    {tryb.error ? ` (${tryb.error})` : ''}, więc przyczyna zera jest nieznana.
                  </>
                )}
              </div>
            )}
            <div className="kpi-row">
              <div className="kpi">
                <div className="kpi-label">Dziś</div>
                <div className="kpi-value">{s.dzis.koszt_usd.toFixed(3)} USD</div>
                <div className="kpi-sub">{s.dzis.wywolania} wywołań</div>
              </div>
              <div className="kpi">
                <div className="kpi-label">Limit dobowy</div>
                <div className="kpi-value">{s.limit_dobowy_usd.toFixed(2)} USD</div>
                <div className="kpi-sub">
                  {wykorzystanieUsd == null ? 'twardy stop po przekroczeniu'
                    : `wykorzystane ${(wykorzystanieUsd * 100).toFixed(0)}%`}
                </div>
              </div>
              {/* Limit LICZBY wywołań stoi tu jako osobny kafel, a nie jako
                  dopisek: to jest bramka, która realnie zatrzymała potok,
                  podczas gdy kwotowa nie zadziałała ani razu. */}
              <div className="kpi">
                <div className="kpi-label">Limit wywołań</div>
                <div className="kpi-value">
                  {s.limit_dobowy_wywolan == null ? '—' : s.limit_dobowy_wywolan}
                </div>
                <div className="kpi-sub">
                  {wykorzystanieWywolan == null
                    ? 'limit liczby wywołań nieznany — brak odniesienia'
                    : `wykorzystane ${(wykorzystanieWywolan * 100).toFixed(0)}%`}
                </div>
              </div>
              <div className="kpi">
                <div className="kpi-label">Wykorzystanie</div>
                <div className={`kpi-value ${wykorzystanie > 0.8 ? 'neg' : ''}`}>
                  {wykorzystanie == null ? '—' : `${(wykorzystanie * 100).toFixed(0)}%`}
                </div>
                <div className="kpi-sub">
                  {wykorzystanie == null ? 'żaden limit dobowy nie jest znany — brak odniesienia'
                    : wykorzystanie >= 1
                      ? `analiza wstrzymana do północy UTC (${
                          wykorzystanieWywolan != null && wykorzystanieWywolan >= 1
                            ? 'limit wywołań' : 'limit kwotowy'})`
                      : 'w normie'}
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
          <span className="hint">{feeds.data ? `${feeds.data.length} kanałów RSS` : '—'}</span>
        </div>
        {feeds.error && (
          <div className="notice">
            <strong>Nie udało się odczytać listy źródeł:</strong> {feeds.error}.
          </div>
        )}
        {feeds.data?.length ? (
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr><th>Źródło</th><th>Rynek</th><th>Status</th><th>Ostatni odczyt</th>
                    <th>Co N</th><th>Zachowane / pobrane</th><th>24h</th></tr>
              </thead>
              <tbody>
                {feeds.data.map((f) => {
                  /* Wylaczony kanal wygladal dokladnie jak dzialajacy: status
                     `ok` i odczyt sprzed trzech dni, bo to ostatni odczyt
                     SPRZED wylaczenia. Stary sukces nie jest dowodem pracy. */
                  const wylaczony = f.enabled === false;
                  return (
                  <tr key={f.id} style={{ opacity: wylaczony ? 0.55 : 1 }}>
                    <td>
                      <div style={{ fontWeight: 600 }}>{f.name}</div>
                      <a className="src-link" href={f.url} target="_blank" rel="noopener noreferrer">{f.url}</a>
                    </td>
                    <td className="dim">{f.engine || 'makro'}</td>
                    <td>
                      {wylaczony ? (
                        <span className="badge warn" title="kanał wyłączony — nie jest odpytywany">
                          wyłączony
                        </span>
                      ) : (
                        <span className={`badge ${f.last_status === 'ok' ? 'ok' : f.last_status ? 'bad' : ''}`}>
                          {f.last_status || 'brak'}
                        </span>
                      )}
                    </td>
                    <td className="dim">{fmtAgo(f.last_polled_at)}</td>
                    <td className="mono dim">{wylaczony ? '—' : `${f.poll_interval_s}s`}</td>
                    <td className="mono">
                      {f.articles_kept} / {f.articles_seen}
                      {f.keep_rate != null && (
                        <span className="dim"> ({(f.keep_rate * 100).toFixed(0)}%)</span>
                      )}
                    </td>
                    <td className="mono">{f.last_24h}</td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : feeds.data ? (
          <div className="empty">Brak skonfigurowanych źródeł</div>
        ) : feeds.error ? (
          <div className="empty">Lista źródeł nieznana.</div>
        ) : <div className="empty">Wczytywanie…</div>}
        <div className="row-body" style={{ marginTop: 10 }}>
          Niski odsetek zachowanych artykułów jest oczekiwany, nie alarmujący — tani model
          odsiewa szum, a droższy analizuje tylko to, co przeszło filtr istotności.
        </div>
      </div>

      <div className="glass-card">
        <div className="card-title">
          Przeanalizowane artykuły <span className="badge ai">Claude</span>
          <span className="hint">{news.data ? `najnowsze ${news.data.length}` : '—'}</span>
        </div>
        {news.error && (
          <div className="notice">
            <strong>Nie udało się odczytać artykułów:</strong> {news.error}.
          </div>
        )}
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
                          <span className="mono" style={{ color: val == null ? 'var(--text-dim)' : col }}>
                            {fmtOcena(val)}
                          </span>
                        </div>
                        <div className="bar-track" style={{ height: 4 }}>
                          {val != null && (
                            <div className="bar-fill" style={{ width: `${val * 100}%`, background: col }} />
                          )}
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
        ) : news.data ? (
          <div className="empty">Brak przeanalizowanych artykułów</div>
        ) : news.error ? (
          <div className="empty">Nie wiadomo, czy są przeanalizowane artykuły.</div>
        ) : <div className="empty">Wczytywanie…</div>}
        <div className="row-body" style={{ marginTop: 10 }}>
          <strong>Nowość</strong> jest osobnym wymiarem od siły sygnału: news, który rynek już
          wycenił, ma dla systemu wartość bliską zeru niezależnie od tego, jak mocno brzmi.
        </div>
      </div>
    </div>
  );
}
