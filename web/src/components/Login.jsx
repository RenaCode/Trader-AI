/* Ekran logowania.
   Dwa kroki: haslo, a gdy konto ma wlaczony drugi skladnik - kod TOTP.
   Bilet miedzy krokami sam w sobie niczego nie otwiera i wygasa po 5 minutach. */
import React, { useEffect, useRef, useState } from 'react';
import { post, ApiError } from '../utils/api';

export default function Login({ onSuccess }) {
  const [step, setStep] = useState('password');     // 'password' | 'mfa'
  const [login, setLogin] = useState('');
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [challenge, setChallenge] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const loginRef = useRef(null);
  const codeRef = useRef(null);

  useEffect(() => {
    (step === 'password' ? loginRef : codeRef).current?.focus();
  }, [step]);

  const submitPassword = async (e) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      const r = await post('/api/auth/login', { login: login.trim(), password });
      if (r.mfa_required) {
        setChallenge(r.challenge);
        setStep('mfa');
        setPassword('');
      } else {
        onSuccess();
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Nie udało się połączyć z serwerem');
      setPassword('');
    } finally {
      setBusy(false);
    }
  };

  const submitMfa = async (e) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      await post('/api/auth/mfa', { challenge, code });
      onSuccess();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Nie udało się połączyć z serwerem');
      setCode('');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-shell">
      <div className="login-layout">
        {/* Lewa kolumna znika ponizej 900 px - na telefonie liczy sie sam formularz. */}
        <aside className="login-aside">
          <div className="login-brand">
            <div className="logo-icon">◈</div>
            <div>
              <div className="logo-text">Trader AI</div>
              <div className="login-sub">Autonomiczny system tradingowy</div>
            </div>
          </div>

          <p className="login-lede">
            Dwa niezależne silniki decyzyjne — kryptowaluty i giełda tradycyjna —
            z własnym dziennikiem decyzji, mierzonym postępem uczenia i twardymi
            limitami ryzyka.
          </p>

          <ul className="login-points">
            <li>
              <strong>Każda decyzja jest wyjaśniona</strong>
              Wkład każdego sygnału, cechy i artykułu zapisany przed poznaniem wyniku.
            </li>
            <li>
              <strong>Metryki wyłącznie out-of-sample</strong>
              Walk-forward z licznikiem przetestowanych hipotez — bez ładnych backtestów.
            </li>
            <li>
              <strong>Ryzyko egzekwowane w kodzie</strong>
              Limity dzienne, obsunięcie kapitału i kill-switch niezależne od modelu.
            </li>
          </ul>
        </aside>

      <div className="login-card glass-card">
        <div className="login-card-head">
          <h1>{step === 'password' ? 'Zaloguj się' : 'Weryfikacja dwuetapowa'}</h1>
          <p>{step === 'password'
            ? 'Dostęp wyłącznie dla właściciela systemu.'
            : 'Drugi składnik potwierdza, że to nadal Ty.'}</p>
        </div>

        {step === 'password' ? (
          <form onSubmit={submitPassword} noValidate>
            <label className="field">
              <span className="field-label">Login</span>
              <input
                ref={loginRef}
                className="input-field"
                type="text"
                value={login}
                onChange={(e) => setLogin(e.target.value)}
                autoComplete="username"
                autoCapitalize="none"
                spellCheck="false"
                required
              />
            </label>

            <label className="field">
              <span className="field-label">Hasło</span>
              <input
                className="input-field"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required
              />
            </label>

            {error && <div className="form-error" role="alert">{error}</div>}

            <button className="btn-primary" type="submit" disabled={busy || !login || !password}>
              {busy ? 'Weryfikacja…' : 'Zaloguj się'}
            </button>
            <button
              type="button"
              className="btn-link"
              onClick={() => {
                localStorage.setItem('trader_preview', 'true');
                onSuccess();
              }}
              style={{ marginTop: 12, opacity: 0.8 }}
            >
              👁️ Otwórz Podgląd UI Aplikacji (Tryb Demo)
            </button>
          </form>
        ) : (
          <form onSubmit={submitMfa} noValidate>
            <div className="notice info" style={{ marginBottom: 16 }}>
              Hasło poprawne. Podaj sześciocyfrowy kod z aplikacji uwierzytelniającej
              albo jeden z kodów zapasowych.
            </div>

            <label className="field">
              <span className="field-label">Kod weryfikacyjny</span>
              <input
                ref={codeRef}
                className="input-field code-input"
                type="text"
                inputMode="numeric"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                autoComplete="one-time-code"
                maxLength={14}
                required
              />
            </label>

            {error && <div className="form-error" role="alert">{error}</div>}

            <button className="btn-primary" type="submit" disabled={busy || code.length < 6}>
              {busy ? 'Sprawdzanie…' : 'Potwierdź'}
            </button>
            <button
              type="button"
              className="btn-link"
              onClick={() => { setStep('password'); setError(''); setCode(''); }}
            >
              Wróć
            </button>
          </form>
        )}

        <div className="login-foot">
          <span className="badge warn">FAZA DEMO</span>
          Handel wyłącznie na koncie demonstracyjnym
        </div>
      </div>
      </div>

      <div className="login-legal">
        Trader AI ·{' '}
        <a href="https://renacode.com" target="_blank" rel="noopener noreferrer">RenaCode</a>
      </div>
    </div>
  );
}
