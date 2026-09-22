/* Zakladka PROFIL: konto, bezpieczenstwo, sesje, powiadomienia, aktywnosc.
   Kazda sekcja zapisuje sie osobno i daje wlasna informacje zwrotna - jeden
   przycisk "zapisz wszystko" ukrywa, co sie faktycznie zmienilo. */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { get, post, put, del } from '../utils/api';
import { fmtTime, fmtAgo } from '../utils/format';

const CATEGORY_LABEL = {
  ryzyko: 'Ryzyko', handel: 'Handel', uczenie: 'Uczenie',
  konto: 'Konto', system: 'System',
};

/* Odstep miedzy powiadomieniami tego samego typu. Zaokraglanie do minut dawalo
   "nie czesciej niz co 0 min" dla 20 s - czyli komunikat, ze odstepu nie ma. */
function fmtOdstep(s) {
  if (s == null) return '—';
  if (s < 60) return `${s} s`;
  const min = Math.floor(s / 60);
  const reszta = s % 60;
  return reszta ? `${min} min ${reszta} s` : `${min} min`;
}

function Section({ title, hint, children }) {
  return (
    <div className="glass-card">
      <div className="card-title">{title}{hint && <span className="hint">{hint}</span>}</div>
      {children}
    </div>
  );
}

function Feedback({ state }) {
  if (!state) return null;
  return (
    <div className={state.ok ? 'notice info' : 'form-error'} style={{ marginTop: 12 }}>
      {state.message}
    </div>
  );
}

/* ------------------------------------------------------------------ konto */
function AccountSection({ profile, reload }) {
  const [name, setName] = useState('');
  const [tz, setTz] = useState('Europe/Warsaw');
  const [state, setState] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (profile) { setName(profile.display_name || ''); setTz(profile.timezone); }
  }, [profile]);

  const save = async () => {
    setBusy(true); setState(null);
    try {
      await put('/api/account/profile', { display_name: name || null, timezone: tz, locale: 'pl' });
      setState({ ok: true, message: 'Zapisano.' });
      reload();
    } catch (e) { setState({ ok: false, message: e.message }); }
    finally { setBusy(false); }
  };

  return (
    <Section title="Konto" hint={profile ? `utworzone ${fmtTime(profile.created_at)}` : ''}>
      <div className="grid grid-3" style={{ gap: 12 }}>
        <label className="field" style={{ marginBottom: 0 }}>
          <span className="field-label">Login</span>
          <input className="input-field" value={profile?.login || ''} disabled />
        </label>
        <label className="field" style={{ marginBottom: 0 }}>
          <span className="field-label">Nazwa wyświetlana</span>
          <input className="input-field" value={name} onChange={(e) => setName(e.target.value)}
                 placeholder="opcjonalnie" />
        </label>
        <label className="field" style={{ marginBottom: 0 }}>
          <span className="field-label">Strefa czasowa</span>
          <select className="input-field" value={tz} onChange={(e) => setTz(e.target.value)}>
            {['Europe/Warsaw', 'Europe/London', 'America/New_York', 'Asia/Singapore', 'UTC']
              .map((z) => <option key={z} value={z}>{z}</option>)}
          </select>
        </label>
      </div>
      <div className="row-body">
        Strefa czasowa steruje godzinami ciszy powiadomień i formatem dat w interfejsie.
      </div>
      <button className="btn-ghost" onClick={save} disabled={busy || !profile} style={{ marginTop: 6 }}>
        {busy ? 'Zapisywanie…' : 'Zapisz'}
      </button>
      <Feedback state={state} />
    </Section>
  );
}

/* ------------------------------------------------------------------ haslo */
function PasswordSection() {
  const [cur, setCur] = useState('');
  const [next, setNext] = useState('');
  const [again, setAgain] = useState('');
  const [state, setState] = useState(null);
  const [busy, setBusy] = useState(false);

  const save = async () => {
    if (next !== again) { setState({ ok: false, message: 'Nowe hasła nie są identyczne.' }); return; }
    setBusy(true); setState(null);
    try {
      await post('/api/auth/password', { current_password: cur, new_password: next });
      setCur(''); setNext(''); setAgain('');
      setState({ ok: true, message: 'Hasło zmienione. Wszystkie pozostałe sesje zostały unieważnione.' });
    } catch (e) { setState({ ok: false, message: e.message }); }
    finally { setBusy(false); }
  };

  return (
    <Section title="Hasło" hint="zmiana wylogowuje pozostałe urządzenia">
      <div className="grid grid-3" style={{ gap: 12 }}>
        <label className="field" style={{ marginBottom: 0 }}>
          <span className="field-label">Obecne hasło</span>
          <input className="input-field" type="password" value={cur} autoComplete="current-password"
                 onChange={(e) => setCur(e.target.value)} />
        </label>
        <label className="field" style={{ marginBottom: 0 }}>
          <span className="field-label">Nowe hasło</span>
          <input className="input-field" type="password" value={next} autoComplete="new-password"
                 onChange={(e) => setNext(e.target.value)} />
        </label>
        <label className="field" style={{ marginBottom: 0 }}>
          <span className="field-label">Powtórz nowe</span>
          <input className="input-field" type="password" value={again} autoComplete="new-password"
                 onChange={(e) => setAgain(e.target.value)} />
        </label>
      </div>
      <div className="row-body">Minimum 12 znaków. Hasło jest hashowane algorytmem Argon2id.</div>
      <button className="btn-ghost" onClick={save} disabled={busy || !cur || next.length < 12}
              style={{ marginTop: 6 }}>
        {busy ? 'Zmieniam…' : 'Zmień hasło'}
      </button>
      <Feedback state={state} />
    </Section>
  );
}

/* ------------------------------------------------------------------ sesje */
function SessionsSection() {
  const [sessions, setSessions] = useState(null);
  const [password, setPassword] = useState('');
  const [state, setState] = useState(null);
  const [busy, setBusy] = useState(false);
  const [loadErr, setLoadErr] = useState('');

  const load = useCallback(async () => {
    // "Brak aktywnych sesji" na ekranie, ktory da sie otworzyc WYLACZNIE
    // z aktywna sesja, to stan logicznie niemozliwy: /account/sessions zwraca
    // co najmniej biezaca. Jedyna droga do tego napisu byla awaria odczytu.
    setLoadErr('');
    try { setSessions(await get('/api/account/sessions')); }
    catch (e) { setSessions(null); setLoadErr(e?.message || 'Nie udało się odczytać sesji'); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const revoke = async (id) => {
    setState(null);
    try { await del(`/api/account/sessions/${id}`); await load(); }
    catch (e) { setState({ ok: false, message: e?.message || 'Nie udało się unieważnić sesji' }); }
  };

  const revokeAll = async () => {
    setBusy(true); setState(null);
    try {
      await post('/api/account/sessions/revoke-others', { password });
      setPassword('');
      setState({ ok: true, message: 'Pozostałe urządzenia zostały wylogowane.' });
      await load();
    } catch (e) { setState({ ok: false, message: e.message }); }
    finally { setBusy(false); }
  };

  return (
    <Section title="Aktywne sesje" hint={sessions ? `${sessions.length}` : '—'}>
      {loadErr && (
        <div className="form-error">
          <strong>Nie udało się odczytać listy sesji:</strong> {loadErr}.
          Co najmniej bieżąca sesja istnieje — inaczej nie widziałbyś tego ekranu.
        </div>
      )}
      {sessions?.length ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {sessions.map((s) => (
            <div key={s.id} className="row-item">
              <div className="row-head">
                <span className="row-title" style={{ fontSize: '0.85rem' }}>
                  {s.current ? 'To urządzenie' : 'Inne urządzenie'}
                </span>
                {s.current
                  ? <span className="badge ok">bieżąca</span>
                  : <button className="btn-ghost" style={{ padding: '5px 12px', fontSize: '0.75rem' }}
                            onClick={() => revoke(s.id)}>Unieważnij</button>}
              </div>
              <div className="row-meta">
                <span>{s.ip || 'nieznane IP'}</span>
                <span>od {fmtTime(s.created_at)}</span>
                <span>wygasa {fmtTime(s.expires_at)}</span>
              </div>
              {s.user_agent && (
                <div className="row-body" style={{ fontSize: '0.75rem' }}>{s.user_agent}</div>
              )}
            </div>
          ))}
        </div>
      ) : sessions === null ? (
        <div className="empty">{loadErr ? 'Lista sesji nieznana.' : 'Wczytywanie…'}</div>
      ) : (
        /* Pusta lista przy UDANYM odczycie tez nie powinna sie zdarzyc - mowimy
           wiec, ze cos jest nie tak, a nie ze nikt nie jest zalogowany. */
        <div className="empty">
          Serwer nie zwrócił żadnej sesji, choć ta przeglądarka jest zalogowana —
          zgłoś to, bo taki stan nie powinien wystąpić.
        </div>
      )}

      <div className="row-body" style={{ marginTop: 14 }}>
        Jeśli podejrzewasz, że ktoś ma dostęp do konta, wyloguj wszystkie inne urządzenia.
        Wymaga hasła — przejęte ciasteczko sesji nie wystarczy, żeby tego użyć.
      </div>
      <div style={{ display: 'flex', gap: 10, marginTop: 8, flexWrap: 'wrap' }}>
        <input className="input-field" type="password" placeholder="Twoje hasło"
               value={password} onChange={(e) => setPassword(e.target.value)}
               style={{ flex: '1 1 240px' }} />
        <button className="btn-ghost" disabled={busy || !password} onClick={revokeAll}
                style={{ borderColor: 'rgba(239,68,68,0.3)', color: 'var(--danger-light)' }}>
          Wyloguj inne urządzenia
        </button>
      </div>
      <Feedback state={state} />
    </Section>
  );
}

/* ------------------------------------------------------------------ powiadomienia */
function NotificationsSection() {
  const [prefs, setPrefs] = useState(null);
  const [channels, setChannels] = useState(null);
  const [quiet, setQuiet] = useState(null);
  const [state, setState] = useState(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({ kind: 'whatsapp', label: '', telefon: '', apikey: '', url: '' });
  const [errs, setErrs] = useState({});
  const [quietState, setQuietState] = useState(null);
  // Ostatnia wartosc ciszy POTWIERDZONA przez serwer. Pola edytuja `quiet`,
  // ale przy nieudanym zapisie cofamy sie wlasnie tutaj - inaczej na ekranie
  // zostawalaby godzina, ktorej w bazie nie ma.
  const quietSaved = useRef(null);

  const load = useCallback(async () => {
    // Trzy NIEZALEZNE odczyty. Wczesniej szly kolejnymi `await` bez catcha:
    // awaria pierwszego przerywala funkcje, `quiet` zostawalo nullem, a pola
    // ciszy renderowaly wtedy wartosc domyslna ('08:00') jako USTAWIONA.
    // Kazda edycja wysylala potem PUT z jednym polem, a QuietBody dopelniala
    // reszte DOMYSLNYMI - czyli nadpisywala realne ustawienia ciszy.
    const [p, c, q] = await Promise.allSettled([
      get('/api/notifications/preferences'),
      get('/api/notifications/channels'),
      get('/api/notifications/quiet'),
    ]);
    setPrefs(p.status === 'fulfilled' ? p.value : null);
    setChannels(c.status === 'fulfilled' ? c.value : null);
    if (q.status === 'fulfilled') { setQuiet(q.value); quietSaved.current = q.value; }
    else { setQuiet(null); quietSaved.current = null; }
    setErrs({
      prefs: p.status === 'rejected' ? (p.reason?.message || 'odczyt nie powiódł się') : '',
      channels: c.status === 'rejected' ? (c.reason?.message || 'odczyt nie powiódł się') : '',
      quiet: q.status === 'rejected' ? (q.reason?.message || 'odczyt nie powiódł się') : '',
    });
  }, []);
  useEffect(() => { load(); }, [load]);

  const togglePref = async (p, patch) => {
    const next = { event_code: p.code, enabled: p.enabled, channels: p.channels,
                   cooldown_s: p.cooldown_s, ...patch };
    setPrefs((xs) => xs.map((x) => (x.code === p.code ? { ...x, ...patch } : x)));
    try { await put('/api/notifications/preferences', next); }
    catch (e) { setState({ ok: false, message: e.message }); load(); }
  };

  const toggleChannel = (p, kind) => {
    const has = p.channels.includes(kind);
    togglePref(p, { channels: has ? p.channels.filter((c) => c !== kind) : [...p.channels, kind] });
  };

  const addChannel = async () => {
    setBusy(true); setState(null);
    try {
      await post('/api/notifications/channels', form);
      setForm({ kind: form.kind, label: '', telefon: '', apikey: '', url: '' });
      await load();
      setState({ ok: true, message: 'Kanał dodany. Wyślij wiadomość testową, żeby go zweryfikować.' });
    } catch (e) { setState({ ok: false, message: e.message }); }
    finally { setBusy(false); }
  };

  const testChannel = async (id) => {
    setBusy(true); setState(null);
    try {
      const r = await post(`/api/notifications/channels/${id}/test`);
      setState({ ok: r.ok, message: r.message });
      await load();
    } catch (e) {
      // Blad ma byc WIDOCZNY. Przy 500 wyjatek przerywal funkcje przed setState
      // i przycisk tylko mrugal - ten sam wzorzec, co naprawiony w Bell.markAll.
      setState({ ok: false, message: e?.message || 'Nie udało się wysłać wiadomości testowej' });
    } finally { setBusy(false); }
  };

  const removeChannel = async (id) => {
    setBusy(true); setState(null);
    try { await del(`/api/notifications/channels/${id}`); await load(); }
    catch (e) { setState({ ok: false, message: e?.message || 'Nie udało się usunąć kanału' }); }
    finally { setBusy(false); }
  };

  /* Edycja pol ciszy NIE wysyla zapisu. PUT szedl dotad na kazde `onChange`
     inputa type="time", czyli takze na polowicznie wpisanej godzinie. */
  const editQuiet = (patch) => setQuiet((q) => (q ? { ...q, ...patch } : q));

  const commitQuiet = async () => {
    const saved = quietSaved.current;
    // Bez wczytanych ustawien nie ma z czym scalic zmiany, a serwer dopelnilby
    // brakujace pola DOMYSLNYMI - kasujac realna cisze. Lepiej nie zapisac.
    if (!quiet || !saved) return;
    if (quiet.quiet_from === saved.quiet_from && quiet.quiet_to === saved.quiet_to
        && quiet.digest_at === saved.digest_at) return;
    if (!quiet.digest_at) {
      setQuiet(saved);
      setQuietState({ ok: false, message: 'Godzina podsumowania nie może być pusta.' });
      return;
    }
    const next = { ...quiet, quiet_from: quiet.quiet_from || null, quiet_to: quiet.quiet_to || null };
    try {
      await put('/api/notifications/quiet', next);
      quietSaved.current = next;
      setQuiet(next);
      // Komunikat NAZYWA zapisane wartosci. Puste pole godziny to zapis "bez
      // ciszy" - jesli ktos wyszedl z pola w polowie wpisywania, ma to zobaczyc.
      setQuietState({
        ok: true,
        message: `Zapisano: cisza ${next.quiet_from && next.quiet_to
          ? `${next.quiet_from}–${next.quiet_to}` : 'wyłączona'}, `
          + `podsumowanie dnia o ${next.digest_at}.`,
      });
    } catch (e) {
      setQuiet(saved);
      setQuietState({ ok: false, message: e?.message || 'Nie udało się zapisać godzin ciszy' });
    }
  };

  const grouped = (prefs || []).reduce((acc, p) => {
    (acc[p.category] ||= []).push(p);
    return acc;
  }, {});

  return (
    <>
      <Section title="Kanały powiadomień" hint="gdzie mają docierać alerty">
        <div className="notice info" style={{ marginBottom: 14 }}>
          Kanał <strong>w aplikacji</strong> działa zawsze. <strong>WhatsApp</strong> to
          najszybsza droga na telefon: zapisz numer <span className="mono">+34 644 51 95 23</span>{' '}
          w kontaktach, wyślij na niego <span className="mono">I allow callmebot to send me messages</span>,
          a w odpowiedzi dostaniesz klucz API przypisany do swojego numeru.
          Nie wymaga to konta Meta ani zatwierdzania szablonów.
        </div>

        {errs.channels && (
          <div className="form-error">
            <strong>Nie udało się odczytać listy kanałów:</strong> {errs.channels}.
            To, że poniżej nic nie ma, nie znaczy, że nie masz skonfigurowanych kanałów.
          </div>
        )}

        {channels?.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 16 }}>
            {channels.map((c) => (
              <div key={c.id} className="row-item">
                <div className="row-head">
                  <span className="row-title" style={{ fontSize: '0.85rem' }}>{c.label}</span>
                  <span style={{ display: 'flex', gap: 6 }}>
                    <span className="badge">{c.kind}</span>
                    {c.kind !== 'inapp' && (c.verified
                      ? <span className="badge ok">zweryfikowany</span>
                      : <span className="badge warn">niezweryfikowany</span>)}
                  </span>
                </div>
                {c.hint && <div className="row-meta"><span className="mono">{c.hint}</span>
                  {c.last_sent_at && <span>ostatnio {fmtAgo(c.last_sent_at)}</span>}</div>}
                {c.last_error && (
                  <div className="row-body" style={{ color: 'var(--danger-light)' }}>{c.last_error}</div>
                )}
                {c.kind !== 'inapp' && (
                  <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
                    <button className="btn-ghost" disabled={busy} onClick={() => testChannel(c.id)}
                            style={{ padding: '6px 14px', fontSize: '0.78rem' }}>
                      Wyślij test
                    </button>
                    <button className="btn-ghost" disabled={busy}
                            onClick={() => removeChannel(c.id)}
                            style={{ padding: '6px 14px', fontSize: '0.78rem',
                                     borderColor: 'rgba(239,68,68,0.3)', color: 'var(--danger-light)' }}>
                      Usuń
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        <div className="grid grid-2" style={{ gap: 12 }}>
          <label className="field" style={{ marginBottom: 0 }}>
            <span className="field-label">Typ kanału</span>
            <select className="input-field" value={form.kind}
                    onChange={(e) => setForm({ ...form, kind: e.target.value })}>
              <option value="whatsapp">WhatsApp</option>
              <option value="webhook">Webhook (HTTPS)</option>
            </select>
          </label>
          <label className="field" style={{ marginBottom: 0 }}>
            <span className="field-label">Etykieta</span>
            <input className="input-field" value={form.label}
                   onChange={(e) => setForm({ ...form, label: e.target.value })}
                   placeholder="np. mój telefon" />
          </label>
        </div>

        {form.kind === 'whatsapp' ? (
          <div className="grid grid-2" style={{ gap: 12, marginTop: 12 }}>
            <label className="field" style={{ marginBottom: 0 }}>
              <span className="field-label">Numer telefonu</span>
              <input className="input-field mono" value={form.telefon}
                     placeholder="+48123456789" autoComplete="off"
                     onChange={(e) => setForm({ ...form, telefon: e.target.value })} />
            </label>
            <label className="field" style={{ marginBottom: 0 }}>
              <span className="field-label">Klucz CallMeBot</span>
              <input className="input-field mono" type="password" value={form.apikey}
                     autoComplete="off"
                     onChange={(e) => setForm({ ...form, apikey: e.target.value })} />
            </label>
          </div>
        ) : (
          <label className="field" style={{ marginTop: 12, marginBottom: 0 }}>
            <span className="field-label">Adres webhooka</span>
            <input className="input-field mono" value={form.url} placeholder="https://…"
                   onChange={(e) => setForm({ ...form, url: e.target.value })} />
          </label>
        )}

        <button className="btn-ghost" style={{ marginTop: 12 }} disabled={busy || !form.label}
                onClick={addChannel}>
          {busy ? 'Zapisywanie…' : 'Dodaj kanał'}
        </button>
        <Feedback state={state} />
      </Section>

      <Section title="Godziny ciszy"
               hint={quiet ? 'alerty krytyczne przechodzą zawsze'
                           : (errs.quiet ? 'ustawienia nieodczytane' : 'wczytywanie…')}>
        {errs.quiet && (
          <div className="form-error">
            <strong>Nie udało się odczytać godzin ciszy:</strong> {errs.quiet}.
            Pola są zablokowane celowo — zapis jednego pola kazałby serwerowi dopełnić
            pozostałe wartościami domyślnymi i skasowałby to, co jest ustawione naprawdę.
          </div>
        )}
        <div className="grid grid-3" style={{ gap: 12 }}>
          <label className="field" style={{ marginBottom: 0 }}>
            <span className="field-label">Cisza od</span>
            <input className="input-field" type="time" disabled={!quiet}
                   value={quiet?.quiet_from || ''} onBlur={commitQuiet}
                   onChange={(e) => editQuiet({ quiet_from: e.target.value })} />
          </label>
          <label className="field" style={{ marginBottom: 0 }}>
            <span className="field-label">Cisza do</span>
            <input className="input-field" type="time" disabled={!quiet}
                   value={quiet?.quiet_to || ''} onBlur={commitQuiet}
                   onChange={(e) => editQuiet({ quiet_to: e.target.value })} />
          </label>
          <label className="field" style={{ marginBottom: 0 }}>
            <span className="field-label">Podsumowanie dnia</span>
            <input className="input-field" type="time" disabled={!quiet}
                   value={quiet?.digest_at || ''} onBlur={commitQuiet}
                   onChange={(e) => editQuiet({ digest_at: e.target.value })} />
          </label>
        </div>
        <div className="row-body" style={{ marginTop: 6 }}>
          Zmiana zapisuje się po wyjściu z pola, nie przy każdym wciśniętym znaku.
        </div>
        <Feedback state={quietState} />
        <div className="row-body">
          W godzinach ciszy alerty nie idą na WhatsAppa ani webhook, ale nadal trafiają
          do dzwonka. <strong>Kill-switch i martwy silnik przechodzą zawsze</strong> —
          alert krytyczny, który nie dotarł, jest groźniejszy niż jego brak.
        </div>
      </Section>

      <Section title="Co ma mnie powiadamiać"
               hint={prefs ? `${prefs.length} typów zdarzeń`
                           : (errs.prefs ? 'lista nieodczytana' : 'wczytywanie…')}>
        {errs.prefs && (
          <div className="form-error">
            <strong>Nie udało się odczytać reguł powiadamiania:</strong> {errs.prefs}.
            Pusta lista poniżej nie znaczy, że nic Cię nie powiadamia — znaczy, że
            nie wiadomo, co Cię powiadamia.
          </div>
        )}
        {Object.entries(grouped).map(([cat, list]) => (
          <div key={cat} style={{ marginBottom: 18 }}>
            <div className="stat-label" style={{ marginBottom: 8 }}>
              {CATEGORY_LABEL[cat] || cat}
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {list.map((p) => (
                <div key={p.code} className="row-item">
                  <div className="row-head">
                    <span className="row-title" style={{ fontSize: '0.85rem' }}>{p.label}</span>
                    <span style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                      <span className={`badge ${p.severity === 'critical' ? 'bad'
                        : p.severity === 'warning' ? 'warn' : 'info'}`}>{p.severity}</span>
                      {p.always_on
                        ? <span className="badge bad">zawsze włączony</span>
                        : (
                          <label className="switch">
                            <input type="checkbox" checked={p.enabled}
                                   onChange={(e) => togglePref(p, { enabled: e.target.checked })} />
                            <span className="slider" />
                          </label>
                        )}
                    </span>
                  </div>
                  <div className="row-body">{p.description}</div>
                  {p.enabled && !p.always_on && (
                    <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
                      {['inapp', 'whatsapp', 'webhook'].map((k) => (
                        <button key={k} className={`chip ${p.channels.includes(k) ? 'on' : ''}`}
                                onClick={() => toggleChannel(p, k)}>
                          {k === 'inapp' ? 'w aplikacji' : k}
                        </button>
                      ))}
                      {p.cooldown_s > 0 && (
                        <span className="dim" style={{ fontSize: '0.74rem', alignSelf: 'center' }}>
                          nie częściej niż co {fmtOdstep(p.cooldown_s)}
                        </span>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        ))}
      </Section>
    </>
  );
}

/* ------------------------------------------------------------------ aktywnosc */
function ActivitySection() {
  const [items, setItems] = useState(null);
  const [err, setErr] = useState('');
  // Catch podstawial dotad PUSTA LISTE, wiec awaria odczytu renderowala sie
  // jako "Brak zapisanych zdarzen" - dziennik bezpieczenstwa twierdzil, ze na
  // koncie nic sie nie dzialo, akurat wtedy, gdy nie dalo sie tego sprawdzic.
  useEffect(() => {
    get('/api/account/activity?limit=60')
      .then(setItems)
      .catch((e) => { setItems(null); setErr(e?.message || 'odczyt nie powiódł się'); });
  }, []);

  return (
    <Section title="Dziennik aktywności" hint="ostatnie operacje na koncie">
      {err && (
        <div className="form-error">
          <strong>Nie udało się odczytać dziennika:</strong> {err}.
        </div>
      )}
      {items?.length ? (
        <div className="row-list" style={{ maxHeight: 420 }}>
          {items.map((a) => (
            <div key={a.id} className="row-item">
              <div className="row-head">
                <span className="row-title" style={{ fontSize: '0.84rem' }}>{a.label}</span>
                {a.suspicious && <span className="badge bad">nieudana próba</span>}
              </div>
              <div className="row-meta">
                <span>{fmtTime(a.at)}</span>
                {a.ip && <span className="mono">{a.ip}</span>}
                {a.entity_id && <span>{a.entity_id}</span>}
              </div>
            </div>
          ))}
        </div>
      ) : items === null ? (
        <div className="empty">{err ? 'Zawartość dziennika nieznana.' : 'Wczytywanie…'}</div>
      ) : <div className="empty">Brak zapisanych zdarzeń</div>}
    </Section>
  );
}

export default function Profile({ user }) {
  const [profile, setProfile] = useState(null);
  const [profileErr, setProfileErr] = useState('');

  const load = useCallback(async () => {
    // Bez tego catcha `profile` zostawal nullem, a karta ponizej czytala
    // `profile?.totp_enabled` jako falsz i pokazywala na CZERWONO, ze drugi
    // skladnik jest wylaczony - przy koncie, ktore ma go wlaczony. Wlasciciel
    // widzial wtedy utrate drugiego skladnika i szedl go wlaczac ponownie.
    // "Nie wiem" i "wylaczona" to dwie rozne odpowiedzi.
    setProfileErr('');
    try { setProfile(await get('/api/account/profile')); }
    catch (e) { setProfile(null); setProfileErr(e?.message || 'Nie udało się odczytać profilu'); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const znane = profile != null;

  return (
    <div className="grid" style={{ gap: 16 }}>
      {profileErr && (
        <div className="form-error" style={{ marginBottom: 0 }}>
          <strong>Nie udało się odczytać danych konta:</strong> {profileErr}.
          Stan drugiego składnika, kodów zapasowych i ostatniego logowania jest
          nieznany — pola poniżej nie są pomiarem.
        </div>
      )}

      <AccountSection profile={profile} reload={load} />

      <Section
        title="Bezpieczeństwo"
        hint={znane
          ? (profile.totp_enabled ? 'drugi składnik włączony' : 'drugi składnik wyłączony')
          : (profileErr ? 'stan nieznany — odczyt nie powiódł się' : 'wczytywanie…')}
      >
        <div className="grid grid-3">
          <div className="stat">
            <span className="stat-label">Weryfikacja dwuetapowa</span>
            <span className={`stat-value sm ${znane ? (profile.totp_enabled ? 'profit' : 'loss') : 'dim'}`}>
              {znane ? (profile.totp_enabled ? 'Włączona' : 'Wyłączona') : '—'}
            </span>
            <span className="stat-sub">
              {znane ? 'konfiguracja w zakładce Ustawienia'
                     : 'brak odczytu — to nie znaczy „wyłączona”'}
            </span>
          </div>
          <div className="stat">
            <span className="stat-label">Kody zapasowe</span>
            <span className={`stat-value sm ${znane ? '' : 'dim'}`}>
              {znane ? `${profile.backup_codes_left} / ${profile.backup_codes_total}` : '—'}
            </span>
            <span className="stat-sub">niewykorzystane</span>
          </div>
          <div className="stat">
            <span className="stat-label">Ostatnie logowanie</span>
            <span className={`stat-value sm ${znane ? '' : 'dim'}`}>
              {znane ? fmtAgo(profile.last_login_at) : '—'}
            </span>
          </div>
        </div>
      </Section>

      <PasswordSection />
      <SessionsSection />
      <NotificationsSection />
      <ActivitySection />
    </div>
  );
}
