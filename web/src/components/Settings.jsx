/* Zakladka USTAWIENIA: klucze API gield i drugi skladnik logowania.
   Klucze sa szyfrowane po stronie serwera; front nigdy nie widzi ich z powrotem. */
import React, { useCallback, useEffect, useState } from 'react';
import { get, post, put, del, ApiError } from '../utils/api';
import { fmtTime, fmtAgo } from '../utils/format';
import Potwierdzenie from './Potwierdzenie';

function TestResult({ result }) {
  if (!result) return null;
  return (
    <div className={result.ok ? 'notice info' : 'notice'} style={{ marginTop: 10 }}>
      <div style={{ fontWeight: 600, marginBottom: result.ok ? 6 : 0 }}>{result.message}</div>
      {result.ok && (
        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', fontSize: '0.78rem' }}>
          {result.account_type && <span>typ konta: {result.account_type}</span>}
          {result.latency_ms != null && <span>opóźnienie: {result.latency_ms} ms</span>}
          {result.permissions?.length > 0 && (
            <span>uprawnienia: {result.permissions.join(', ')}</span>
          )}
          {result.balances?.length > 0 && (
            <span>saldo: {result.balances.map((b) => `${b.total} ${b.currency}`).join(', ')}</span>
          )}
        </div>
      )}
    </div>
  );
}

function AddForm({ venues, onAdded }) {
  const [venue, setVenue] = useState('');
  const [label, setLabel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [apiSecret, setApiSecret] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => { if (!venue && venues?.length) setVenue(venues[0].code); }, [venues, venue]);

  const submit = async (e) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true); setError('');
    try {
      await post('/api/credentials', {
        venue_code: venue, mode: 'demo', label: label.trim(),
        api_key: apiKey.trim(), api_secret: apiSecret.trim(),
      });
      setLabel(''); setApiKey(''); setApiSecret('');
      onAdded();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Nie udało się zapisać klucza');
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit}>
      <div className="grid grid-2" style={{ gap: 14 }}>
        <label className="field" style={{ marginBottom: 0 }}>
          <span className="field-label">Giełda</span>
          <select className="input-field" value={venue} onChange={(e) => setVenue(e.target.value)}>
            {(venues || []).map((v) => (
              <option key={v.code} value={v.code}>{v.name} · {v.engine}</option>
            ))}
          </select>
        </label>
        <label className="field" style={{ marginBottom: 0 }}>
          <span className="field-label">Etykieta</span>
          <input className="input-field" value={label} onChange={(e) => setLabel(e.target.value)}
                 placeholder="np. bybit-demo-glowny" required />
        </label>
      </div>

      <label className="field" style={{ marginTop: 14 }}>
        <span className="field-label">Klucz API</span>
        <input className="input-field mono" value={apiKey} autoComplete="off" spellCheck="false"
               onChange={(e) => setApiKey(e.target.value)} required />
      </label>

      <label className="field">
        <span className="field-label">Sekret API</span>
        <input className="input-field mono" type="password" value={apiSecret} autoComplete="off"
               onChange={(e) => setApiSecret(e.target.value)} required />
      </label>

      {error && <div className="form-error">{error}</div>}

      <button className="btn-primary" type="submit"
              disabled={busy || !label || !apiKey || !apiSecret}
              style={{ width: 'auto', padding: '12px 26px' }}>
        {busy ? 'Zapisywanie…' : 'Zapisz klucz'}
      </button>
    </form>
  );
}

export default function Settings({ user, onUserChanged }) {
  const [creds, setCreds] = useState(null);
  const [venues, setVenues] = useState(null);
  const [tests, setTests] = useState({});
  const [busyId, setBusyId] = useState(null);
  // Klucz czekajacy na potwierdzenie usuniecia - patrz `Potwierdzenie.jsx`.
  const [doUsuniecia, setDoUsuniecia] = useState(null);
  const [loadErr, setLoadErr] = useState('');

  const load = useCallback(async () => {
    // Nieudany odczyt zostawial `creds` jako null, a karta wypisywala wtedy
    // "Brak zapisanych kluczy" razem z opisem, co system w zwiazku z tym robi
    // (zbiera tylko dane publiczne, nie ma dostepu do salda). To jest
    // twierdzenie o stanie systemu postawione na podstawie awarii odczytu -
    // grozniejsze niz sama awaria, bo brzmi jak pomiar.
    setLoadErr('');
    try {
      setCreds(await get('/api/credentials'));
      setVenues(await get('/api/credentials/venues'));
    } catch (e) {
      setCreds(null);
      setLoadErr(e?.message || 'Nie udało się odczytać kluczy');
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const runTest = async (id) => {
    setBusyId(id);
    try {
      const result = await post(`/api/credentials/${id}/test`);
      setTests((t) => ({ ...t, [id]: result }));
      await load();
    } catch (err) {
      setTests((t) => ({ ...t, [id]: { ok: false, message: err.message } }));
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (id) => {
    setBusyId(id);
    try {
      await del(`/api/credentials/${id}`);
      await load();
    } catch (err) {
      // Bez tego wyjatek przerywal funkcje przed odswiezeniem listy: klucz
      // zostawal na ekranie, przycisk sie odmrazal i nic nie mowilo, ze
      // usuniecie sie nie udalo. Wlasciciel wychodzil przekonany, ze usunal.
      setTests((t) => ({
        ...t,
        [id]: { ok: false, message: err?.message || 'Nie udało się usunąć klucza' },
      }));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="grid" style={{ gap: 16 }}>
      <div className="notice">
        <strong>Zanim dodasz klucz.</strong> Utwórz go na koncie Bybit w trybie
        <em> Demo Trading</em> (nie na testnecie — testnet ma własny, płytki orderbook
        i inne ceny niż rynek). Nadaj wyłącznie uprawnienia do <em>odczytu</em> i
        <em> handlu</em>. <strong>Nigdy nie nadawaj prawa wypłaty</strong> — system go nie
        potrzebuje, a klucz z takim uprawnieniem zostanie tu automatycznie
        zdezaktywowany. Jeśli giełda na to pozwala, ogranicz klucz do adresu IP
        serwera: <span className="mono">185.201.112.104</span>.
      </div>

      {loadErr && (
        <div className="form-error" style={{ marginBottom: 0 }}>
          <strong>Nie udało się odczytać kluczy API:</strong> {loadErr}.
          Nie wiadomo, ile kluczy jest zapisanych ani czy są aktywne — odśwież stronę.
        </div>
      )}

      <div className="glass-card">
        <div className="card-title">
          Dodaj klucz API
          <span className="hint">tryb demo · live zablokowany do końca fazy testowej</span>
        </div>
        {venues ? <AddForm venues={venues} onAdded={load} /> : (
          <div className="empty">
            {loadErr
              ? 'Lista giełd niedostępna — bez niej formularz nie zapisałby klucza poprawnie.'
              : 'Wczytywanie…'}
          </div>
        )}
      </div>

      <div className="glass-card">
        <div className="card-title">
          Zapisane klucze
          <span className="hint">{creds ? creds.length : '—'}</span>
        </div>
        {loadErr ? (
          <div className="empty">Stan kluczy nieznany — odczyt nie powiódł się.</div>
        ) : creds === null ? (
          <div className="empty">Wczytywanie…</div>
        ) : creds.length ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {creds.map((c) => (
              <div key={c.id} className="row-item">
                <div className="row-head">
                  <span className="row-title">
                    {c.venue_name} · {c.label}
                  </span>
                  <span style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                    <span className="badge">{c.mode}</span>
                    {c.can_withdraw === true
                      ? <span className="badge bad">PRAWO WYPŁATY</span>
                      : c.can_withdraw === false
                        ? <span className="badge ok">bez wypłat</span>
                        /* Podmiot unijny nie wystawia metadanych klucza, więc
                           uprawnień nie da się odczytać. „Bez wypłat" byłoby
                           tu zapewnieniem, którego nikt nie sprawdził. */
                        : <span className="badge warn">uprawnienia nieznane</span>}
                    <span className={`badge ${c.active ? 'ok' : 'warn'}`}>
                      {c.active ? 'aktywny' : 'nieaktywny'}
                    </span>
                  </span>
                </div>
                <div className="row-meta">
                  <span className="mono">{c.key_hint}</span>
                  <span>dodany {fmtTime(c.created_at)}</span>
                  {c.last_used_at && <span>ostatnio użyty {fmtAgo(c.last_used_at)}</span>}
                  {c.days_left != null && (
                    <span className={c.days_left <= 21 ? 'badge warn' : undefined}>
                      {c.days_left > 0
                        ? `wygasa za ${c.days_left} dni (${fmtTime(c.expires_at)})`
                        : 'KLUCZ WYGASŁ — wygeneruj nowy'}
                    </span>
                  )}
                </div>
                {c.last_error && (
                  <div className="row-body" style={{ color: 'var(--danger-light)' }}>
                    {c.last_error}
                  </div>
                )}
                <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
                  <button className="btn-ghost" disabled={busyId === c.id}
                          onClick={() => runTest(c.id)}>
                    {busyId === c.id ? 'Sprawdzam…' : 'Sprawdź połączenie'}
                  </button>
                  <button className="btn-ghost" disabled={busyId === c.id}
                          onClick={() => setDoUsuniecia(c)}
                          style={{ borderColor: 'rgba(239,68,68,0.3)', color: 'var(--danger-light)' }}>
                    Usuń
                  </button>
                </div>
                <TestResult result={tests[c.id]} />
              </div>
            ))}
          </div>
        ) : (
          <div className="empty">
            Brak zapisanych kluczy. Bez nich system zbiera wyłącznie dane publiczne
            (świece, orderbook) i nie ma dostępu do salda ani składania zleceń.
          </div>
        )}
      </div>

      {doUsuniecia && (
        <Potwierdzenie
          tytul="Usunąć klucz giełdy?"
          etykieta="Usuń klucz"
          zajete={busyId === doUsuniecia.id}
          onAnuluj={() => setDoUsuniecia(null)}
          onPotwierdz={async () => { await remove(doUsuniecia.id); setDoUsuniecia(null); }}>
          <strong>{doUsuniecia.venue_name} · {doUsuniecia.label}</strong>{' '}
          (<span className="mono">{doUsuniecia.key_hint}</span>, {doUsuniecia.mode}).
          Silnik straci dostęp do tego konta: nie złoży ani nie zamknie zlecenia,
          dopóki nie dodasz nowego klucza. Tego nie da się cofnąć.
        </Potwierdzenie>
      )}

      <SecretsPanel />

      <WalletsPanel />

      <div className="glass-card">
        <div className="card-title">
          Weryfikacja dwuetapowa
          <span className="hint">
            {user?.totp_enabled ? 'włączona' : 'wyłączona — wymagana przed przejściem na live'}
          </span>
        </div>
        <TotpPanel enabled={user?.totp_enabled} onChanged={onUserChanged} />
      </div>
    </div>
  );
}

function TotpPanel({ enabled, onChanged }) {
  const [setup, setSetup] = useState(null);
  const [code, setCode] = useState('');
  const [password, setPassword] = useState('');
  const [codes, setCodes] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const start = async () => {
    setBusy(true); setError('');
    // Haslo jest wymagane: konfiguracja z samej sesji pozwalala przypiac
    // konto do cudzej aplikacji uwierzytelniajacej.
    try { setSetup(await post('/api/auth/totp/setup', { current_password: password })); setPassword(''); }
    catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };

  const confirm = async () => {
    setBusy(true); setError('');
    try {
      const r = await post('/api/auth/totp/confirm', { code });
      setCodes(r.backup_codes);
      setSetup(null);
      onChanged?.();
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };

  if (codes) {
    return (
      <div>
        <div className="notice" style={{ marginBottom: 12 }}>
          <strong>Zapisz te kody teraz.</strong> Każdy działa raz i zastępuje kod
          z aplikacji, gdy stracisz do niej dostęp. W bazie są wyłącznie ich skróty —
          nie da się ich odzyskać.
        </div>
        <div className="mono" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 8 }}>
          {codes.map((c) => (
            <span key={c} style={{ background: 'var(--bg-inset)', padding: '8px 12px',
                                   borderRadius: 'var(--radius-sm)', fontSize: '0.85rem' }}>{c}</span>
          ))}
        </div>
      </div>
    );
  }

  if (enabled) {
    return (
      <div className="row-body">
        Drugi składnik jest włączony. Logowanie wymaga kodu z aplikacji
        uwierzytelniającej albo jednego z kodów zapasowych.
      </div>
    );
  }

  if (setup) {
    return (
      <div>
        <div className="grid grid-2" style={{ alignItems: 'center' }}>
          <img src={`data:image/png;base64,${setup.qr_png_base64}`} alt="Kod QR do konfiguracji 2FA"
               style={{ width: 190, background: '#fff', padding: 10, borderRadius: 'var(--radius-md)' }} />
          <div>
            <div className="row-body" style={{ marginTop: 0 }}>
              Zeskanuj kod w aplikacji uwierzytelniającej, a jeśli nie możesz —
              wpisz sekret ręcznie:
            </div>
            <div className="mono" style={{ background: 'var(--bg-inset)', padding: '10px 12px',
                                           borderRadius: 'var(--radius-sm)', margin: '10px 0',
                                           wordBreak: 'break-all', fontSize: '0.82rem' }}>
              {setup.secret}
            </div>
            <label className="field">
              <span className="field-label">Kod z aplikacji</span>
              <input className="input-field code-input" value={code} inputMode="numeric"
                     maxLength={6} onChange={(e) => setCode(e.target.value)} />
            </label>
            {error && <div className="form-error">{error}</div>}
            <button className="btn-primary" disabled={busy || code.length < 6} onClick={confirm}
                    style={{ width: 'auto', padding: '12px 26px' }}>
              {busy ? 'Sprawdzam…' : 'Potwierdź i włącz'}
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="row-body" style={{ marginTop: 0, marginBottom: 12 }}>
        Drugi składnik jest wyłączony. Do końca fazy demonstracyjnej to dopuszczalne,
        ale musi zostać włączony przed przejściem na tryb live.
      </div>
      <label className="field">
        <span className="field-label">Bieżące hasło</span>
        <input className="input-field" type="password" value={password}
               autoComplete="current-password" onChange={(e) => setPassword(e.target.value)} />
      </label>
      {error && <div className="form-error">{error}</div>}
      <button className="btn-ghost" onClick={start} disabled={busy || !password}>
        {busy ? 'Przygotowuję…' : 'Włącz weryfikację dwuetapową'}
      </button>
    </div>
  );
}


/* ------------------------------------------------------------------ sekrety */
function SecretsPanel() {
  const [secrets, setSecrets] = useState(null);
  const [values, setValues] = useState({});
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState({});
  const [saved, setSaved] = useState({});
  const [tested, setTested] = useState({});
  const [tryb, setTryb] = useState(null);
  const [doUsuniecia, setDoUsuniecia] = useState(null);
  const [loadErr, setLoadErr] = useState('');

  const load = useCallback(async () => {
    // Bez tego catcha awaria odczytu renderowala karte z samymi poradami i bez
    // jednego pola - ekran wygladal na kompletny, tylko "nie ma tu nic do
    // ustawienia". Brak pol ma znaczyc brak pol, a nie brak odpowiedzi.
    setLoadErr('');
    try {
      setSecrets(await get('/api/settings/secrets'));
    } catch (e) {
      setSecrets(null);
      setLoadErr(e?.message || 'Nie udało się odczytać kluczy usług');
    }
    // Tryb liczy SERWER ta sama funkcja, ktorej uzywa worker. Druga kopia
    // reguly tutaj rozjechalaby sie przy pierwszej zmianie - i to wlasnie ten
    // ekran zapewnialby wtedy, ze jest inaczej niz jest.
    try { setTryb(await get('/api/settings/analiza-tryb')); } catch { setTryb(null); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const save = async (name) => {
    setBusy(name); setError((e) => ({ ...e, [name]: '' }));
    try {
      await put(`/api/settings/secrets/${name}`, { value: values[name] });
      setValues((v) => ({ ...v, [name]: '' }));
      setSaved((s) => ({ ...s, [name]: true }));
      await load();
    } catch (err) {
      setError((e) => ({ ...e, [name]: err.message }));
    } finally { setBusy(null); }
  };

  const clear = async (name) => {
    setBusy(name); setError((e) => ({ ...e, [name]: '' }));
    try { await del(`/api/settings/secrets/${name}`); await load(); }
    // Nieudane usuniecie musi byc widoczne: klucz nadal dziala, a przy cichym
    // bledzie wlasciciel wychodzi z ekranu przekonany, ze go wycofal.
    catch (err) { setError((e) => ({ ...e, [name]: err?.message || 'Nie udało się usunąć klucza' })); }
    finally { setBusy(null); }
  };

  const test = async (name) => {
    setBusy(name); setError((e) => ({ ...e, [name]: '' }));
    try {
      const r = await post(`/api/settings/secrets/${name}/test`);
      setTested((t) => ({ ...t, [name]: r }));
    } catch (err) {
      setTested((t) => ({ ...t, [name]: { ok: false, message: err.message } }));
    } finally { setBusy(null); }
  };

  return (
    <div className="glass-card">
      <div className="card-title">
        Klucze usług
        <span className="hint">szyfrowane przed zapisem · nigdy nie zwracane przez API</span>
      </div>

      {tryb && (
        <div className={tryb.aktywny ? 'notice info' : 'notice warn'}
             style={{ marginBottom: 16 }}>
          {!tryb.aktywny && <>{tryb.opis}</>}
          {tryb.aktywny && tryb.mieszanka && (
            <>
              <strong>Oba klucze pracują RAZEM.</strong> Filtrowanie artykułów robi{' '}
              <span className="mono">{tryb.model_triazu}</span>, a analizę tych, które
              przeszły filtr — <span className="mono">{tryb.model_analizy}</span>.
              <br /><br />
              Filtrowanie to około 94% wywołań i tam Gemini jest 5,5× tańszy; analiza
              kosztuje u obu podobnie, więc decyduje o niej jakość. Gdy Gemini wyczerpie
              limit, filtrowanie <em>samo</em> przechodzi na Anthropica i wraca, gdy limit
              się odnowi — bez zmiany ustawień.
            </>
          )}
          {tryb.aktywny && !tryb.mieszanka && (
            <>
              Analiza wiadomości działa na jednym dostawcy
              (<span className="mono">{tryb.dostawca}</span>): filtrowanie{' '}
              <span className="mono">{tryb.model_triazu}</span>, analiza{' '}
              <span className="mono">{tryb.model_analizy}</span>.
              <br /><br />
              <strong>Dodanie drugiego klucza obniża koszt</strong>, a nie tylko dokłada
              zapas: filtrowanie przeniesie się wtedy na tańszy model, a analiza zostanie
              na mocniejszym.
            </>
          )}
        </div>
      )}

      <div className="notice info" style={{ marginBottom: 16 }}>
        <strong>Subskrypcja Claude.ai to nie to samo co dostęp do API.</strong> Plan Pro
        ani Max nie obejmuje wywołań API — to osobny produkt z osobnym rozliczeniem
        i nie da się zalogować subskrypcją do aplikacji zewnętrznej.
        <br /><br />
        Klucz utworzysz tak: załóż konto w{' '}
        <a className="src-link" href="https://platform.claude.com" target="_blank"
           rel="noopener noreferrer">Claude Console ↗</a>{' '}
        (inne niż claude.ai), doładuj je w <em>Billing</em>, a potem wejdź w{' '}
        <a className="src-link" href="https://platform.claude.com/settings/keys" target="_blank"
           rel="noopener noreferrer">Settings → API Keys ↗</a>{' '}
        i użyj <em>Create Key</em>. Klucz zaczyna się od <span className="mono">sk-ant-</span>
        {' '}i widać go tylko raz.
        <br /><br />
        Skaner filtruje artykuły tanim modelem, a analizuje tylko te, które przejdą filtr —
        przy dziesięciu kanałach RSS to rząd kilku dolarów miesięcznie.
      </div>

      {loadErr && (
        <div className="form-error">
          <strong>Nie udało się odczytać stanu kluczy usług:</strong> {loadErr}.
          Nie wiadomo, które są ustawione — porady powyżej mówią, jak je dodać,
          ale nie mówią nic o tym, co jest w bazie.
        </div>
      )}
      {!loadErr && secrets === null && <div className="empty">Wczytywanie…</div>}

      {(secrets || []).map((s) => (
        <div key={s.name} style={{ marginBottom: 18 }}>
          <div className="row-head" style={{ marginBottom: 6 }}>
            <span className="row-title">{s.label}</span>
            {s.is_set
              ? <span className="badge ok">ustawiony <span className="mono">{s.hint}</span></span>
              : <span className="badge warn">brak</span>}
          </div>
          <div className="row-body" style={{ marginTop: 0, marginBottom: 10 }}>
            {s.description}
            {s.is_set && s.updated_at && ` Ostatnia zmiana: ${fmtTime(s.updated_at)}.`}
          </div>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            <input
              className="input-field mono"
              type="password"
              autoComplete="off"
              placeholder={s.is_set ? 'wpisz nowy, aby wymienić' : 'sk-ant-…'}
              value={values[s.name] || ''}
              onChange={(e) => setValues((v) => ({ ...v, [s.name]: e.target.value }))}
              style={{ flex: '1 1 320px' }}
            />
            <button className="btn-ghost" disabled={busy === s.name || !(values[s.name] || '').trim()}
                    onClick={() => save(s.name)}>
              {busy === s.name ? 'Zapisywanie…' : 'Zapisz'}
            </button>
            {s.is_set && (
              <button className="btn-ghost" disabled={busy === s.name} onClick={() => test(s.name)}>
                {busy === s.name ? 'Sprawdzam…' : 'Sprawdź klucz'}
              </button>
            )}
            {s.is_set && (
              <button className="btn-ghost" disabled={busy === s.name} onClick={() => setDoUsuniecia(s)}
                      style={{ borderColor: 'rgba(239,68,68,0.3)', color: 'var(--danger-light)' }}>
                Usuń
              </button>
            )}
          </div>
          {error[s.name] && <div className="form-error" style={{ marginTop: 10 }}>{error[s.name]}</div>}
          {saved[s.name] && !error[s.name] && !tested[s.name] && (
            <div className="notice info" style={{ marginTop: 10 }}>
              Zapisano. Kliknij „Sprawdź klucz”, żeby potwierdzić, że działa —
              poprawny format nic nie dowodzi.
            </div>
          )}
          {tested[s.name] && (
            <div className={tested[s.name].ok ? 'notice info' : 'form-error'}
                 style={{ marginTop: 10 }}>
              {tested[s.name].message}
            </div>
          )}
        </div>
      ))}

      {doUsuniecia && (
        <Potwierdzenie
          tytul="Usunąć klucz usługi?"
          etykieta="Usuń klucz"
          zajete={busy === doUsuniecia.name}
          onAnuluj={() => setDoUsuniecia(null)}
          onPotwierdz={async () => { await clear(doUsuniecia.name); setDoUsuniecia(null); }}>
          <strong>{doUsuniecia.label}</strong>
          {doUsuniecia.hint && <> (<span className="mono">{doUsuniecia.hint}</span>)</>}.
          Usługa przestanie działać od następnego wywołania. Tego nie da się cofnąć —
          klucz trzeba będzie wkleić od nowa.
        </Potwierdzenie>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ portfele */
function WalletsPanel() {
  const [wallets, setWallets] = useState(null);
  const [chain, setChain] = useState('bitcoin');
  const [label, setLabel] = useState('');
  const [ident, setIdent] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [loadErr, setLoadErr] = useState('');
  const [doUsuniecia, setDoUsuniecia] = useState(null);

  const load = useCallback(async () => {
    // Pusta lista portfeli i NIEODCZYTANA lista portfeli wygladaly identycznie,
    // a pierwsza z nich to zdanie "nie obserwujemy zadnego adresu".
    setLoadErr('');
    try { setWallets(await get('/api/settings/wallets')); }
    catch (e) { setWallets(null); setLoadErr(e?.message || 'Nie udało się odczytać portfeli'); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const add = async (e) => {
    e.preventDefault();
    setBusy(true); setError('');
    try {
      await post('/api/settings/wallets', { chain, label: label.trim(), identifier: ident.trim() });
      setLabel(''); setIdent('');
      await load();
    } catch (err) {
      setError(err.message);
    } finally { setBusy(false); }
  };

  const remove = async (id) => {
    setBusy(true); setError('');
    try { await del(`/api/settings/wallets/${id}`); await load(); }
    catch (e) { setError(e?.message || 'Nie udało się usunąć portfela'); }
    finally { setBusy(false); }
  };

  return (
    <div className="glass-card">
      <div className="card-title">
        Portfele
        <span className="hint">wyłącznie obserwowane</span>
      </div>

      <div className="notice" style={{ marginBottom: 16 }}>
        <strong>Podaj adres albo klucz publiczny (xpub) — nic więcej.</strong> Ten system
        celowo nie przyjmuje kluczy prywatnych ani fraz odzyskiwania: aplikacja wystawiona
        na internet to najgorsze miejsce na dane dające prawo wydania środków. Do śledzenia
        salda wystarczy adres, a handel i tak odbywa się na giełdzie, przez klucz API
        bez prawa wypłaty. Próba wklejenia frazy albo klucza prywatnego zostanie odrzucona.
      </div>

      <form onSubmit={add}>
        <div className="grid grid-3" style={{ gap: 12, marginBottom: 12 }}>
          <label className="field" style={{ marginBottom: 0 }}>
            <span className="field-label">Sieć</span>
            <select className="input-field" value={chain} onChange={(e) => setChain(e.target.value)}>
              <option value="bitcoin">Bitcoin</option>
              <option value="ethereum">Ethereum</option>
            </select>
          </label>
          <label className="field" style={{ marginBottom: 0 }}>
            <span className="field-label">Etykieta</span>
            <input className="input-field" value={label} onChange={(e) => setLabel(e.target.value)}
                   placeholder="np. portfel-glowny" required />
          </label>
          <label className="field" style={{ marginBottom: 0 }}>
            <span className="field-label">Adres lub xpub</span>
            <input className="input-field mono" value={ident} spellCheck="false" autoComplete="off"
                   onChange={(e) => setIdent(e.target.value)} placeholder="bc1…" required />
          </label>
        </div>
        {error && <div className="form-error">{error}</div>}
        <button className="btn-primary" type="submit" disabled={busy || !label || !ident}
                style={{ width: 'auto', padding: '12px 26px' }}>
          {busy ? 'Zapisywanie…' : 'Dodaj portfel'}
        </button>
      </form>

      {loadErr && (
        <div className="form-error" style={{ marginTop: 16, marginBottom: 0 }}>
          <strong>Nie udało się odczytać listy portfeli:</strong> {loadErr}.
          Nie wiadomo, które adresy są obserwowane.
        </div>
      )}

      {wallets?.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 18 }}>
          {wallets.map((w) => (
            <div key={w.id} className="row-item">
              <div className="row-head">
                <span className="row-title">{w.label}</span>
                <span style={{ display: 'flex', gap: 6 }}>
                  <span className="badge">{w.chain}</span>
                  <span className="badge info">{w.kind}</span>
                  <span className="badge ok">tylko odczyt</span>
                </span>
              </div>
              <div className="row-meta">
                <span className="mono">{w.identifier}</span>
                {w.balance != null && <span>saldo: {w.balance}</span>}
                <span>dodany {fmtTime(w.created_at)}</span>
              </div>
              <div style={{ marginTop: 10 }}>
                <button className="btn-ghost" disabled={busy} onClick={() => setDoUsuniecia(w)}
                        style={{ borderColor: 'rgba(239,68,68,0.3)', color: 'var(--danger-light)' }}>
                  Usuń
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {doUsuniecia && (
        <Potwierdzenie
          tytul="Usunąć portfel?"
          etykieta="Usuń portfel"
          zajete={busy}
          onAnuluj={() => setDoUsuniecia(null)}
          onPotwierdz={async () => { await remove(doUsuniecia.id); setDoUsuniecia(null); }}>
          <strong>{doUsuniecia.label}</strong> ({doUsuniecia.chain},{' '}
          <span className="mono">{doUsuniecia.identifier}</span>).
          Adres przestanie być obserwowany, a jego saldo zniknie z panelu.
        </Potwierdzenie>
      )}
    </div>
  );
}
