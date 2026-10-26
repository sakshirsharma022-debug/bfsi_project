import React, { useState, useEffect, useRef } from 'react';
import {
  signInWithPopup,
  signInWithEmailAndPassword,
  createUserWithEmailAndPassword,
  signOut,
  onAuthStateChanged,
} from 'firebase/auth';
import { auth, googleProvider } from './firebase';

// Backend address: VITE_API_BASE from frontend/.env.production when deployed,
// ?api=... in the address bar for testing, otherwise the local dev server.
const API_BASE = (
  new URLSearchParams(window.location.search).get('api') ||
  import.meta.env.VITE_API_BASE ||
  'http://localhost:8000'
).replace(/\/$/, '');

// ---------------------------------------------------------
// CURRENT CHAT IN THE URL
//
// The open chat's id is kept in the address bar (?chat=<id>),
// so a browser refresh reopens the same conversation.
// No ?chat= in the URL means a new, empty chat.
// ---------------------------------------------------------

const getChatIdFromUrl = () =>
  new URLSearchParams(window.location.search).get('chat') || null;

const writeChatIdToUrl = (chatId) => {
  const url = new URL(window.location.href);

  if (chatId) {
    url.searchParams.set('chat', chatId);
  } else {
    url.searchParams.delete('chat');
  }

  // replaceState: update the address without adding browser-history entries.
  if (url.href !== window.location.href) {
    window.history.replaceState(null, '', url);
  }
};

const SHOW = {
  sourceList: true,
  figureChips: false,
  sourcesInText: false,
};

export default function App() {
  // ---------------------------------------------------------
  // CHAT / SESSION STATE
  // ---------------------------------------------------------

  // Start from the chat in the URL (if any), so a refresh keeps it.
  const [sessionId, setSessionId] = useState(getChatIdFromUrl);
  const [isRestoringChat, setIsRestoringChat] = useState(
    () => Boolean(getChatIdFromUrl())
  );
  const [chats, setChats] = useState([]);
  const [user, setUser] = useState(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [authMode, setAuthMode] = useState('login');
  const [authEmail, setAuthEmail] = useState('');
  const [authPassword, setAuthPassword] = useState('');
  const [authSubmitting, setAuthSubmitting] = useState(false);
  const [authError, setAuthError] = useState(''); // NEW: inline error on login screen
  const [turns, setTurns] = useState([]);

  const [question, setQuestion] = useState('');
  const [isSending, setIsSending] = useState(false);

  const textareaRef = useRef(null);
  const conversationEndRef = useRef(null);

  // ---------------------------------------------------------
  // BACKEND / FILINGS STATE
  // ---------------------------------------------------------

  const [allFilings, setAllFilings] = useState([]);
  const [isHealthy, setIsHealthy] = useState(false);

  const [selectedCompany, setSelectedCompany] = useState('');
  const [selectedPeriod, setSelectedPeriod] = useState('');

  // ---------------------------------------------------------
  // UI STATE
  // ---------------------------------------------------------

  const [toastMessage, setToastMessage] = useState('');
  const [toastShow, setToastShow] = useState(false);
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(
    window.innerWidth <= 680
  );
  const [reportLoading, setReportLoading] = useState(false);
  const [openChatMenuId, setOpenChatMenuId] = useState(null);

  // ADD NEW FILING is collapsed by default so that
  // recent chat history gets more vertical space.
  const [isIngestOpen, setIsIngestOpen] = useState(false);

  // Only administrators (INGEST_ADMIN_EMAILS on the backend) see "Add new filing".
  const [canIngest, setCanIngest] = useState(false);

  // ---------------------------------------------------------
  // INGESTION STATE
  // ---------------------------------------------------------

  const [ingestTicker, setIngestTicker] = useState('');
  const [ingestYear, setIngestYear] = useState('');
  const [ingestQuarter, setIngestQuarter] = useState('');
  const [ingestProgress, setIngestProgress] = useState('');

  // ---------------------------------------------------------
  // TOAST
  // ---------------------------------------------------------

  const showToast = (msg) => {
    setToastMessage(msg);
    setToastShow(true);

    setTimeout(() => {
      setToastShow(false);
    }, 3200);
  };

  // ---------------------------------------------------------
  // GENERIC API HELPER
  // ---------------------------------------------------------

  const callApi = async (path, options = {}) => {
    const token = user ? await user.getIdToken() : null;

    // FIXED: pull headers out of options so they merge with
    // (instead of overwriting) the Authorization header.
    const { headers: extraHeaders, ...rest } = options;

    const res = await fetch(`${API_BASE}${path}`, {
      ...rest,
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(extraHeaders || {}),
      },
    });

    if (!res.ok) {
      let detail = `Request failed (${res.status})`;

      try {
        const body = await res.json();
        detail = body.detail || detail;
      } catch {}

      throw new Error(detail);
    }

    return res.json();
  };

  // ---------------------------------------------------------
  // FIREBASE AUTHENTICATION
  // ---------------------------------------------------------

  useEffect(() => {
    const unsubscribe = onAuthStateChanged(auth, (currentUser) => {
      setUser(currentUser);
      setAuthLoading(false);
    });

    return () => unsubscribe();
  }, []);

  const handleGoogleLogin = async () => {
    setAuthError('');

    try {
      await signInWithPopup(auth, googleProvider);
    } catch (err) {
      console.error('Google login failed:', err);
      setAuthError(`Google login failed: ${err.message}`);
    }
  };

  const handleEmailAuth = async (e) => {
    e.preventDefault();

    const email = authEmail.trim();
    const password = authPassword;

    setAuthError('');

    if (!email || !password) {
      setAuthError('Please enter your email and password.');
      return;
    }

    if (password.length < 6) {
      setAuthError('Password must be at least 6 characters.');
      return;
    }

    setAuthSubmitting(true);

    try {
      if (authMode === 'signup') {
        await createUserWithEmailAndPassword(auth, email, password);
      } else {
        await signInWithEmailAndPassword(auth, email, password);
      }
    } catch (err) {
      console.error('Email authentication failed:', err.code, err.message);

      const messages = {
        'auth/invalid-credential':
          'Invalid email or password. If you originally signed up with Google, use "Continue with Google".',
        'auth/user-not-found': 'No account exists with this email.',
        'auth/wrong-password': 'Incorrect password.',
        'auth/email-already-in-use': 'An account already exists with this email.',
        'auth/invalid-email': 'Please enter a valid email address.',
        'auth/weak-password': 'Password must be at least 6 characters.',
        'auth/operation-not-allowed':
          'Email/password sign-in is not enabled in Firebase.',
        'auth/too-many-requests': 'Too many attempts. Try again later.',
        'auth/network-request-failed':
          'Network error. Check your connection.',
      };

      setAuthError(
        messages[err.code] || err.message || 'Authentication failed.'
      );
    } finally {
      setAuthSubmitting(false);
    }
  };

  const handleLogout = async () => {
    try {
      await signOut(auth);
      setSessionId(null);
      setTurns([]);
      setChats([]);
      setCanIngest(false);
    } catch (err) {
      console.error('Logout failed:', err);
      showToast(`Logout failed: ${err.message}`);
    }
  };

  // ---------------------------------------------------------
  // KEEP THE URL IN SYNC WITH THE OPEN CHAT
  // ---------------------------------------------------------

  useEffect(() => {
    writeChatIdToUrl(sessionId);
  }, [sessionId]);

  // Reopen the chat named in the URL after a refresh.
  const restoreChat = async (id) => {
    setIsRestoringChat(true);

    try {
      const data = await callApi(
        `/api/sessions/${encodeURIComponent(id)}`
      );

      setSessionId(id);
      setTurns(data.turns || []);
      applyChatFilters(data.filters);
    } catch {
      // Deleted, or belongs to another account: fall back to a new chat.
      setSessionId(null);
      setTurns([]);
      showToast('That chat could not be opened. Starting a new one.');
    } finally {
      setIsRestoringChat(false);
    }
  };

  // ---------------------------------------------------------
  // INITIAL DATA
  // ---------------------------------------------------------

  useEffect(() => {
    if (authLoading || !user) {
      return;
    }
    const checkHealth = async () => {
      try {
        await callApi('/api/health');
        setIsHealthy(true);
      } catch {
        setIsHealthy(false);
      }
    };

    const loadInitialData = async () => {
      // Start reopening the current chat right away, alongside the rest.
      const chatToRestore = getChatIdFromUrl();
      const restoring = chatToRestore
        ? restoreChat(chatToRestore)
        : Promise.resolve();

      await checkHealth();

      try {
        const me = await callApi('/api/me');
        setCanIngest(Boolean(me.can_ingest));
      } catch {
        setCanIngest(false);
      }

      try {
        const filings = await callApi('/api/filings');
        setAllFilings(filings);
      } catch {
        showToast('Unable to load filings library');
      }

      await loadHistory();
      await restoring;
    };

    loadInitialData();

    const interval = setInterval(checkHealth, 30000);

    return () => clearInterval(interval);
  }, [authLoading, user]);

  // ---------------------------------------------------------
  // CHAT HISTORY
  // ---------------------------------------------------------

  const loadHistory = async () => {
    try {
      const historyList = await callApi('/api/sessions');
      setChats(historyList.slice(0, 30));
    } catch {}
  };

  // Put a chat's saved company/period back in the filter bar.
  const applyChatFilters = (filters) => {
    setSelectedCompany((filters?.ticker || '').toUpperCase());
    setSelectedPeriod(filters?.fiscal_period || '');
  };

  const openSession = async (id) => {
    setOpenChatMenuId(null);

    try {
      const data = await callApi(
        `/api/sessions/${encodeURIComponent(id)}`
      );

      setSessionId(id);
      setTurns(data.turns || []);
      applyChatFilters(data.filters);
    } catch (err) {
      showToast(err.message);
    }
  };

  const resetChat = () => {
    setSessionId(null);
    setTurns([]);
  };

  const deleteChat = async (id, e) => {
    e.stopPropagation();
    setOpenChatMenuId(null);

    if (!window.confirm('Delete this chat? This cannot be undone.')) {
      return;
    }

    try {
      await callApi(
        `/api/sessions/${encodeURIComponent(id)}`,
        {
          method: 'DELETE',
        }
      );

      if (id === sessionId) {
        resetChat();
      }

      await loadHistory();

      showToast('Chat deleted');
    } catch (err) {
      showToast(err.message);
    }
  };

  const clearAllHistory = async () => {
    if (!chats.length) {
      return;
    }

    if (!window.confirm(`Delete all ${chats.length} chats?`)) {
      return;
    }

    await Promise.allSettled(
      chats.map((c) =>
        callApi(
          `/api/sessions/${encodeURIComponent(c.session_id)}`,
          {
            method: 'DELETE',
          }
        )
      )
    );

    resetChat();

    await loadHistory();

    showToast('All chats deleted');
  };

  // ---------------------------------------------------------
  // COMPANY / PERIOD FILTERS
  // ---------------------------------------------------------

  const companiesMap = new Map();

  allFilings.forEach((f) => {
    const ticker = (f.ticker || '').toUpperCase();

    if (ticker && !companiesMap.has(ticker)) {
      companiesMap.set(ticker, {
        ticker,
        company: f.company,
      });
    }
  });

  const companies = [...companiesMap.values()];

  // Changing company keeps the period only if that company has it.
  const changeCompany = (ticker) => {
    setSelectedCompany(ticker);

    const companyPeriods = allFilings
      .filter((f) => !ticker || (f.ticker || '').toUpperCase() === ticker)
      .map((f) => f.fiscal_period);

    if (selectedPeriod && !companyPeriods.includes(selectedPeriod)) {
      setSelectedPeriod('');
    }
  };

  const periods = [
    ...new Set(
      allFilings
        .filter(
          (f) =>
            !selectedCompany ||
            (f.ticker || '').toUpperCase() === selectedCompany
        )
        .map((f) => f.fiscal_period)
        .filter(Boolean)
    ),
  ];

  const filteredFilings = allFilings.filter((f) => {
    const matchTicker =
      !selectedCompany ||
      (f.ticker || '').toUpperCase() === selectedCompany;

    const matchPeriod =
      !selectedPeriod ||
      f.fiscal_period === selectedPeriod;

    return matchTicker && matchPeriod;
  });

  // ---------------------------------------------------------
  // FILE / PDF HELPERS
  // ---------------------------------------------------------

  const humanDate = (value) => {
    if (!value) {
      return 'Filing date unavailable';
    }

    const d = new Date(value);

    return Number.isNaN(+d)
      ? String(value)
      : new Intl.DateTimeFormat(undefined, {
          year: 'numeric',
          month: 'short',
          day: 'numeric',
        }).format(d);
  };

  const fileUrl = (filingId) =>
    `${API_BASE}/api/pdf/${encodeURIComponent(filingId)}`;

  // ---------------------------------------------------------
  // SUBMIT QUESTION
  //
  // IMPORTANT:
  // AI-generated suggestions use this SAME function.
  //
  // Therefore clicking a suggestion does not create another
  // agent or another workflow. It simply behaves exactly like
  // the user typed that question into the composer.
  // ---------------------------------------------------------

  const submitQuestion = async (overrideQuestion) => {
    const q = (overrideQuestion ?? question).trim();

    if (!q || isSending) {
      return;
    }

    setQuestion('');

    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }

    setIsSending(true);

    // Earlier answered turns, sent to /api/suggestions as context.
    const previousTurns = turns
      .filter((t) => !t.loading && t.answer)
      .slice(-4)
      .map((t) => ({ question: t.question, answer: t.answer }));

    const newTurn = {
      question: q,
      loading: true,
    };

    setTurns((prev) => [...prev, newTurn]);

    try {
      const result = await callApi('/api/ask', {
        method: 'POST',
        body: JSON.stringify({
          question: q,
          session_id: sessionId,
          ticker: selectedCompany,
          fiscal_period: selectedPeriod,
        }),
      });

      setSessionId(result.session_id);

      setTurns((prev) => {
        const copy = [...prev];

        copy[copy.length - 1] = {
          question: q,
          ...result,
          loading: false,
        };

        return copy;
      });

      // The answer is already on screen. Fetch follow-up suggestions in the
      // background and add them to this answer when they arrive.
      if (result.status === 'answered') {
        callApi('/api/suggestions', {
          method: 'POST',
          body: JSON.stringify({
            session_id: result.session_id,
            question: q,
            answer: result.answer,
            citations: result.citations || [],
            previous_turns: previousTurns,
          }),
        })
          .then((data) => {
            const suggested = data?.suggested_questions || [];

            if (!suggested.length) {
              return;
            }

            setTurns((prev) =>
              prev.map((t) =>
                t.session_id === result.session_id &&
                t.question === q &&
                t.answer === result.answer
                  ? { ...t, suggested_questions: suggested }
                  : t
              )
            );
          })
          .catch(() => {
            // Suggestions are optional; ignore failures.
          });
      }

      loadHistory();
    } catch (err) {
      setTurns((prev) => {
        const copy = [...prev];

        copy[copy.length - 1] = {
          question: q,
          answer: `Error: ${err.message}`,
          status: 'error',
          loading: false,
        };

        return copy;
      });

      setIsHealthy(false);
    } finally {
      setIsSending(false);
    }
  };

  // ---------------------------------------------------------
  // INGEST NEW FILING
  // ---------------------------------------------------------

  const handleIngest = async (e) => {
    e.preventDefault();

    const ticker = ingestTicker.trim().toUpperCase();
    const fiscal_year = ingestYear.trim();
    const quarter = ingestQuarter.trim().toUpperCase();

    setIngestProgress('Queueing filing ingestion...');

    try {
      const res = await callApi('/api/ingest', {
        method: 'POST',
        body: JSON.stringify({
          ticker,
          fiscal_year,
          quarter,
        }),
      });

      if (res.status === 'already_exists') {
        setIngestProgress('✅ Filing is already ready!');

        showToast(`Filing ${res.filing_id} is ready!`);

        return;
      }

      showToast(`Ingestion started for ${res.filing_id}`);

      const pollInterval = setInterval(async () => {
        try {
          const statusRes = await callApi(
            `/api/ingest/status/${encodeURIComponent(
              res.filing_id
            )}`
          );

          setIngestProgress(`⚡ ${statusRes.step}`);

          if (statusRes.status === 'success') {
            clearInterval(pollInterval);

            showToast(
              `✅ Successfully ingested ${res.filing_id}!`
            );

            setIngestProgress('✓ Ready for Q&A');

            const filings = await callApi('/api/filings');

            setAllFilings(filings);
          } else if (statusRes.status === 'failed') {
            clearInterval(pollInterval);

            showToast('❌ Ingestion failed!');

            setIngestProgress(
              `❌ Error: ${statusRes.error}`
            );
          }
        } catch {}
      }, 2000);
    } catch (err) {
      setIngestProgress(`❌ Failed: ${err.message}`);

      showToast(
        `Ingestion failed: ${err.message}`
      );
    }
  };

  // ---------------------------------------------------------
  // WRAP-UP REPORT
  // ---------------------------------------------------------

  const handleWrapUpReport = async () => {
    if (!sessionId || !turns.length || reportLoading) {
      return;
    }

    setReportLoading(true);

    try {
      const result = await callApi(
        `/api/sessions/${encodeURIComponent(sessionId)}/report`,
        {
          method: 'POST',
        }
      );

      if (!result?.download_url) {
        throw new Error(
          'Report was generated but no download link was returned.'
        );
      }

      const downloadUrl = result.download_url.startsWith('http')
        ? result.download_url
        : `${API_BASE}${result.download_url}`;

      window.open(
        downloadUrl,
        '_blank',
        'noopener,noreferrer'
      );

      showToast('✓ Research wrap-up report is ready');
    } catch (err) {
      console.error(
        'Report generation failed:',
        err
      );

      showToast(
        `Report generation failed: ${err.message}`
      );
    } finally {
      setReportLoading(false);
    }
  };

  // ---------------------------------------------------------
  // UI
  // ---------------------------------------------------------

  if (authLoading) {
    return (
      <div className="app-shell" style={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}>
        <div style={{ textAlign: 'center' }}>
          <div className="eyebrow" style={{ justifyContent: 'center' }}>
            <span className="eyebrow-line"></span>
            SEC RESEARCH
          </div>
          <p>Loading your workspace…</p>
        </div>
      </div>
    );
  }

  if (!user) {
    return (
      <div className="auth-screen">
        {/* LEFT: product intro */}
        <section className="auth-hero">
          <div className="brand auth-brand">
            <span className="brand-mark">Q</span>
            <span>Quarterly<span className="brand-dot">.</span></span>
          </div>

          <div className="auth-hero-content">
            <div className="eyebrow">
              <span className="eyebrow-line"></span>
              SEC FINANCIAL RESEARCH
            </div>

            <h1 className="auth-title">
              Research financial filings with <em>confidence.</em>
            </h1>

            <p className="auth-intro">
              Quarterly turns SEC 10-Q filings into a focused research workspace.
              Ask in plain language, trace every answer to the exact filing page,
              and keep your research in one place.
            </p>

            <div className="auth-feats">
              {[
                ['✦', 'Verified answers', 'Every number cited to its filing page.'],
                ['↗', 'Faster analysis', 'Metrics, trends and drivers in seconds.'],
                ['◷', 'Research history', 'Pick up any conversation where you left it.'],
              ].map(([icon, title, text]) => (
                <div className="auth-feat glass" key={title}>
                  <span className="auth-feat-icon">{icon}</span>
                  <strong>{title}</strong>
                  <span>{text}</span>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* RIGHT: sign-in card, centred so everything fits on screen */}
        <section className="auth-panel">
          <div className="auth-card glass">
            <div className="eyebrow">
              <span className="eyebrow-line"></span>
              RESEARCH WORKSPACE
            </div>

            <h2 className="auth-heading">
              {authMode === 'login' ? 'Welcome back.' : 'Create your account.'}
            </h2>
            <p className="auth-sub">
              {authMode === 'login'
                ? 'Sign in to continue your financial research.'
                : 'Create an account to save and access your research history.'}
            </p>

            <form onSubmit={handleEmailAuth}>
              <label className="auth-label" htmlFor="auth-email">EMAIL</label>
              <input
                id="auth-email"
                className="auth-input"
                type="email"
                value={authEmail}
                onChange={(e) => setAuthEmail(e.target.value)}
                placeholder="you@example.com"
                autoComplete="email"
                required
              />

              <label className="auth-label" htmlFor="auth-password">PASSWORD</label>
              <input
                id="auth-password"
                className="auth-input"
                type="password"
                value={authPassword}
                onChange={(e) => setAuthPassword(e.target.value)}
                placeholder="Enter your password"
                autoComplete={authMode === 'login' ? 'current-password' : 'new-password'}
                required
              />

              {authError && (
                <div className="auth-error" role="alert">
                  {authError}
                </div>
              )}

              <button type="submit" className="auth-primary" disabled={authSubmitting}>
                {authSubmitting
                  ? 'Please wait…'
                  : authMode === 'login' ? 'Sign in with email' : 'Create account'}
              </button>
            </form>

            <div className="auth-or">
              <span></span>OR<span></span>
            </div>

            <button
              type="button"
              className="auth-google"
              onClick={handleGoogleLogin}
              disabled={authSubmitting}
            >
              <svg width="17" height="17" viewBox="0 0 48 48" aria-hidden="true">
                <path fill="#FFC107" d="M43.6 20.1H42V20H24v8h11.3C33.7 32.7 29.2 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.6-.4-3.9z" />
                <path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z" />
                <path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.2 0-9.6-3.3-11.3-8l-6.5 5C9.5 39.6 16.2 44 24 44z" />
                <path fill="#1976D2" d="M43.6 20.1H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C37 39.2 44 34 44 24c0-1.3-.1-2.6-.4-3.9z" />
              </svg>
              Continue with Google
            </button>

            <button
              type="button"
              className="auth-switch"
              onClick={() => {
                setAuthMode((mode) => (mode === 'login' ? 'signup' : 'login'));
                setAuthPassword('');
                setAuthError('');
              }}
            >
              {authMode === 'login' ? (
                <>New here? <b>Create an account</b></>
              ) : (
                <>Already have an account? <b>Sign in</b></>
              )}
            </button>
          </div>
        </section>
      </div>
    );
  }

  return (
    <div className={`app-shell ${isSidebarCollapsed ? 'sidebar-collapsed' : ''}`}>

      {/* =====================================================
          SIDEBAR
          ===================================================== */}

      <aside className={`sidebar ${isSidebarCollapsed ? 'is-collapsed' : ''}`}>

        {/* BRAND + SIDEBAR TOGGLE */}
        <div className="sidebar-brand-row">
          <a className="brand" href="#" aria-label="Quarterly home">
            <span className="brand-mark">Q</span>

            <span>
              Quarterly
              <span className="brand-dot">.</span>
            </span>
          </a>

          <button
            type="button"
            className="sidebar-toggle"
            onClick={() => setIsSidebarCollapsed((prev) => !prev)}
            aria-label={isSidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            title={isSidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            aria-expanded={!isSidebarCollapsed}
          >
            {isSidebarCollapsed ? '›' : '‹'}
          </button>
        </div>

        {/* NEW CHAT */}
        <button
          className="new-chat"
          onClick={resetChat}
          title="New research"
        >
          <span className="plus">＋</span>
          <span className="new-chat-label">New research</span>
        </button>

        {/* Add new filing: administrators only (enforced by the backend too). */}
        {canIngest && (
          <>
        {/* =================================================
            COLLAPSIBLE ADD NEW FILING
            ================================================= */}

        <div
          className={`ingest-box ${
            isIngestOpen ? 'expanded' : 'collapsed'
          }`}
          style={{
            width: '100%',
            boxSizing: 'border-box',
          }}
        >
          <button
            type="button"
            className="ingest-toggle"
            title="Add new filing"
            onClick={() => {
              // In the collapsed rail there is no room for the form,
              // so expand the sidebar and open it.
              if (isSidebarCollapsed) {
                setIsSidebarCollapsed(false);
                setIsIngestOpen(true);
                return;
              }

              setIsIngestOpen((prev) => !prev);
            }}
          >
            <span
              className="ingest-toggle-left"
              style={{
                display: 'flex',
                alignItems: 'center',
                minWidth: 0,
              }}
            >
              <span className="ingest-toggle-icon">
                ＋
              </span>

              <span className="side-label">
                ADD NEW FILING
              </span>
            </span>

            <span
              className={`ingest-chevron ${
                isIngestOpen ? 'open' : ''
              }`}
            >
              ▾
            </span>
          </button>

          {isIngestOpen && (
            <div
              className="ingest-dropdown-content"
              style={{
                width: '100%',
                boxSizing: 'border-box',
              }}
            >
              <form
                className="ingest-form"
                style={{
                  width: '100%',
                  boxSizing: 'border-box',
                }}
                onSubmit={handleIngest}
              >
                <input
                  type="text"
                  className="ingest-input"
                  style={{ boxSizing: 'border-box', width: '100%' }}
                  placeholder="Ticker (e.g. AAPL)"
                  value={ingestTicker}
                  onChange={(e) =>
                    setIngestTicker(e.target.value)
                  }
                  required
                />

                <input
                  type="text"
                  className="ingest-input"
                  style={{ boxSizing: 'border-box', width: '100%' }}
                  placeholder="Year (e.g. 2025)"
                  value={ingestYear}
                  onChange={(e) =>
                    setIngestYear(e.target.value)
                  }
                  required
                />

                <input
                  type="text"
                  className="ingest-input"
                  style={{ boxSizing: 'border-box', width: '100%' }}
                  placeholder="Quarter (e.g. Q1)"
                  value={ingestQuarter}
                  onChange={(e) =>
                    setIngestQuarter(e.target.value)
                  }
                  required
                />

                <button
                  type="submit"
                  className="ingest-btn"
                  style={{ width: '100%', boxSizing: 'border-box' }}
                >
                  Ingest Filing
                </button>
              </form>

              {ingestProgress && (
                <div className="ingest-progress">
                  {ingestProgress}
                </div>
              )}
            </div>
          )}
        </div>
          </>
        )}

        {/* WORKSPACE */}
        <div className="side-label">
          WORKSPACE
        </div>

        <a
          className="side-link active"
          href="#"
        >
          <span className="side-icon">◈</span>
          Research assistant
        </a>

        <a
          className="side-link"
          href="#filings"
        >
          <span className="side-icon">▤</span>
          Filing library
          <span className="side-count">
            {allFilings.length}
          </span>
        </a>

        {/* =================================================
            RECENT CHAT HISTORY
            ================================================= */}

        <div className="history-heading">
          <span className="side-label">
            RECENT RESEARCH
          </span>

          <button
            className="icon-button"
            onClick={loadHistory}
          >
            ↻
          </button>
        </div>

        <div className="history-list">
          {chats.length === 0 ? (
            <div className="side-muted">
              Your conversations will appear here.
            </div>
          ) : (
            chats.map((c) => (
              <div
                className="history-row"
                key={c.session_id}
              >
                <button
                  type="button"
                  className={`history-item ${
                    c.session_id === sessionId
                      ? 'selected'
                      : ''
                  }`}
                  onClick={() =>
                    openSession(c.session_id)
                  }
                >
                  {c.title || 'Untitled research'}
                </button>

                <div className="history-actions">
                  <button
                    type="button"
                    className="history-menu-trigger"
                    aria-label={`More options for ${c.title || 'chat'}`}
                    aria-expanded={openChatMenuId === c.session_id}
                    title="Chat options"
                    onClick={(e) => {
                      e.stopPropagation();
                      setOpenChatMenuId((currentId) =>
                        currentId === c.session_id ? null : c.session_id
                      );
                    }}
                  >
                    ⋯
                  </button>

                  {openChatMenuId === c.session_id && (
                    <div className="history-menu" role="menu">
                      <button
                        type="button"
                        className="history-menu-delete"
                        role="menuitem"
                        onClick={(e) => deleteChat(c.session_id, e)}
                      >
                        Delete
                      </button>
                    </div>
                  )}
                </div>
              </div>
            ))
          )}
        </div>

        {chats.length > 0 && (
          <button
            type="button"
            className="history-clear"
            onClick={clearAllHistory}
          >
            Clear all
          </button>
        )}

        {/* SIDEBAR BOTTOM */}
        <div className="sidebar-bottom">
          <div className="user-avatar">
            {(user?.displayName || user?.email || 'A').charAt(0).toUpperCase()}
          </div>

          <div className="user-info">
            <div className="user-name">
              {user?.displayName || 'Financial analyst'}
            </div>

            <div className="user-role">
              {user?.email || 'Research workspace'}
            </div>
          </div>

          <button
            type="button"
            className="logout-button"
            onClick={handleLogout}
            title="Sign out"
            aria-label="Sign out"
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
              <path d="M16 17l5-5-5-5" />
              <path d="M21 12H9" />
            </svg>
          </button>
        </div>
      </aside>

      {/* =====================================================
          MAIN CONTENT
          ===================================================== */}

      <main className="main-area">

        {/* TOPBAR */}
        <header className="topbar">
          <div className="breadcrumb">
            Workspace
            <span>/</span>
            <strong>
              Research assistant
            </strong>
          </div>

          <div className="topbar-right">

            <button
              type="button"
              className="wrap-up-report-button"
              onClick={handleWrapUpReport}
              disabled={
                !sessionId ||
                !turns.length ||
                reportLoading
              }
            >
              <span>✦</span>
              {reportLoading
                ? 'Generating report…'
                : 'Wrap Up Report'}
            </button>

            <div
              className={`api-status ${
                isHealthy
                  ? 'online'
                  : 'offline'
              }`}
            >
              <i></i>

              <span>
                {isHealthy
                  ? 'Backend connected'
                  : 'Backend unavailable'}
              </span>
            </div>
          </div>
        </header>

        <div className="content-wrap">

          {/* =================================================
              WELCOME SCREEN
              ================================================= */}

          <section
            className={`welcome ${
              turns.length > 0 || isRestoringChat
                ? 'hidden'
                : ''
            }`}
          >
            <div className="eyebrow">
              <span className="eyebrow-line"></span>
              FILINGS, MADE CLEAR
            </div>

            <h1>
              Good research starts
              <br />
              with <em>good questions.</em>
            </h1>

            <p className="intro">
              Select a company to analyze core
              financial statements or ask custom
              research questions.
            </p>

            {/* FILTER BAR */}
            <FilterBar
              companies={companies}
              periods={periods}
              selectedCompany={selectedCompany}
              selectedPeriod={selectedPeriod}
              onCompanyChange={changeCompany}
              onPeriodChange={setSelectedPeriod}
            />

            {/* STARTING QUESTIONS */}
            <div className="suggestion-label">
              START WITH A QUESTION
            </div>

            <div className="suggestions">

              <button
                className="suggestion"
                onClick={() =>
                  submitQuestion(
                    'How did total net sales change year over year in the latest quarter?'
                  )
                }
              >
                <span className="suggestion-icon green">
                  ↗
                </span>

                <span>
                  <b>
                    Revenue Drivers
                  </b>

                  <small>
                    How did total net sales change YoY?
                  </small>
                </span>
              </button>

              <button
                className="suggestion"
                onClick={() =>
                  submitQuestion(
                    'What were the primary drivers for cash generated by operating activities?'
                  )
                }
              >
                <span className="suggestion-icon blue">
                  ⌁
                </span>

                <span>
                  <b>
                    Operating Cash Flow
                  </b>

                  <small>
                    What drove changes in cash flow?
                  </small>
                </span>
              </button>

              <button
                className="suggestion"
                onClick={() =>
                  submitQuestion(
                    'Compare operating margins and research expenses across tech peers.'
                  )
                }
              >
                <span className="suggestion-icon amber">
                  ⇄
                </span>

                <span>
                  <b>
                    Margin Analysis
                  </b>

                  <small>
                    Compare gross and operating margins
                  </small>
                </span>
              </button>
            </div>
          </section>

          {/* =================================================
              CHAT CONVERSATION
              ================================================= */}

          <section
            className={`conversation ${
              turns.length === 0 && !isRestoringChat
                ? 'hidden'
                : ''
            }`}
          >
            {isRestoringChat && turns.length === 0 && (
              <div className="loading-turn" style={{ padding: '20px 0' }}>
                Loading your conversation…
              </div>
            )}

            {turns.map((t, idx) => (
              <article
                className="turn"
                key={idx}
              >
                {/* USER QUESTION */}
                <div className="user-question">
                  <span className="user-bubble">
                    {(user?.displayName || user?.email || 'A').charAt(0).toUpperCase()}
                  </span>

                  <span>
                    {t.question}
                  </span>
                </div>

                {/* ANSWER */}
                <div className="answer-block">
                  {t.loading ? (
                    <div className="loading-turn">
                      Searching primary statements
                      and verifying citations…
                    </div>
                  ) : (
                    <AnswerBlock
                      result={t}
                      fileUrl={fileUrl}
                      onSuggestionClick={
                        submitQuestion
                      }
                      isSending={isSending}
                    />
                  )}
                </div>
              </article>
            ))}

            <div ref={conversationEndRef} />
          </section>

          {/* =================================================
              COMPOSER
              ================================================= */}

          <div className="composer-area">
            {/* Filters stay visible and editable during a conversation.
                They are attached to every question you send. */}
            {(turns.length > 0 || isRestoringChat) && (
              <FilterBar
                compact
                companies={companies}
                periods={periods}
                selectedCompany={selectedCompany}
                selectedPeriod={selectedPeriod}
                onCompanyChange={changeCompany}
                onPeriodChange={setSelectedPeriod}
              />
            )}

            <form
              className="composer"
              onSubmit={(e) => {
                e.preventDefault();
                submitQuestion();
              }}
            >
              <textarea
                ref={textareaRef}
                rows="1"
                placeholder="Ask about a line item, metric, or trend…"
                value={question}
                onChange={(e) => {
                  setQuestion(
                    e.target.value
                  );

                  e.target.style.height =
                    'auto';

                  e.target.style.height = `${Math.min(
                    e.target.scrollHeight,
                    130
                  )}px`;
                }}
                onKeyDown={(e) => {
                  if (
                    e.key === 'Enter' &&
                    !e.shiftKey
                  ) {
                    e.preventDefault();
                    submitQuestion();
                  }
                }}
              />

              <div className="composer-bottom">

                <div className="composer-note">
                  <span className="sparkle">
                    ✳
                  </span>

                  Answers verified against
                  SEC 10-Q filings
                </div>

                <button
                  className="send-button"
                  type="submit"
                  disabled={isSending}
                >
                  ↑
                </button>
              </div>
            </form>
          </div>

          {/* =================================================
              FILING LIBRARY
              ================================================= */}

          <section
            className="filings-section"
            id="filings"
          >
            <div className="section-head">
              <div>
                <div className="eyebrow">
                  <span className="eyebrow-line"></span>
                  INDEXED REPORTS
                </div>

                <h2>
                  Filing library
                </h2>
              </div>

              <span className="filing-total">
                {filteredFilings.length}
                {' '}
                reports available
              </span>
            </div>

            <div className="filing-grid">

              {filteredFilings.length === 0 ? (
                <div className="loading-card">
                  No filings found matching
                  your filters.
                </div>
              ) : (
                filteredFilings.map((f) => (
                  <article
                    className="filing-card"
                    key={f.filing_id}
                  >
                    <div className="filing-card-top">

                      <span className="ticker-pill">
                        {(f.ticker || '—').toUpperCase()}
                      </span>

                      <span className="form-pill">
                        {f.form || '10-Q'}
                      </span>
                    </div>

                    <div className="filing-company">
                      {f.company ||
                        f.ticker ||
                        'Company'}
                    </div>

                    <div className="filing-period">
                      {f.fiscal_period ||
                        'Period unavailable'}
                    </div>

                    <div className="filing-date">
                      PERIOD END ·{' '}
                      {humanDate(f.period_end)}
                    </div>

                    {/* Only PDF link remains.
                        Executive Briefing has been removed. */}
                    <div
                      className="filing-actions"
                      style={{
                        marginTop: '12px',
                      }}
                    >
                      <a
                        href={fileUrl(
                          f.filing_id
                        )}
                        target="_blank"
                        rel="noopener"
                      >
                        ↗ View PDF
                      </a>
                    </div>
                  </article>
                ))
              )}
            </div>
          </section>

          {/* FOOTER */}
          <footer className="footer">
            <span>
              Quarterly
              <span className="brand-dot">
                ·
              </span>{' '}
              FINANCIAL RESEARCH
            </span>

            <span>
              Grounded in verified SEC filings.
            </span>
          </footer>
        </div>
      </main>

      {/* TOAST */}
      <div
        className={`toast ${
          toastShow ? 'show' : ''
        }`}
      >
        {toastMessage}
      </div>
    </div>
  );
}


/* =============================================================
   FILTER BAR
   Full size on the welcome screen; compact above the composer
   during a conversation.
   ============================================================= */

function FilterBar({
  companies,
  periods,
  selectedCompany,
  selectedPeriod,
  onCompanyChange,
  onPeriodChange,
  compact = false,
}) {
  const hasFilter = Boolean(selectedCompany || selectedPeriod);

  return (
    <div className={`filter-bar ${compact ? 'compact' : ''}`}>
      <label className="filter-field">
        <span>COMPANY</span>

        <select
          value={selectedCompany}
          onChange={(e) => onCompanyChange(e.target.value)}
        >
          <option value="">All companies</option>

          {companies.map((c) => (
            <option key={c.ticker} value={c.ticker}>
              {c.company} · {c.ticker}
            </option>
          ))}
        </select>
      </label>

      <span className="filter-divider"></span>

      <label className="filter-field period-field">
        <span>FISCAL PERIOD</span>

        <select
          value={selectedPeriod}
          onChange={(e) => onPeriodChange(e.target.value)}
        >
          <option value="">Any period</option>

          {periods.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      </label>

      {compact && (
        <span className="filter-hint">
          {hasFilter ? 'Used for every question' : 'No filter: all filings'}
        </span>
      )}

      {compact && hasFilter && (
        <button
          type="button"
          className="filter-clear"
          onClick={() => {
            onCompanyChange('');
            onPeriodChange('');
          }}
          title="Clear filters"
        >
          Clear
        </button>
      )}
    </div>
  );
}


/* =============================================================
   ANSWER BLOCK
   ============================================================= */

function AnswerBlock({
  result,
  fileUrl,
  onSuggestionClick,
  isSending,
}) {
  const answer =
    result.answer ||
    result.message ||
    'No response returned.';

  // The backend may include a plain-text "Sources:" section
  // in the answer as well as structured citations. Keep the
  // answer text clean and render sources only in VERIFIED SOURCES.
  const displayAnswer = String(answer)
    .replace(/(?:^|\r?\n)\s*Sources:\s*[\s\S]*$/i, '')
    // Convert escaped asterisks into normal asterisks
    .replace(/\\\*/g, '*')
    // Convert list markers into proper bullet points
    .replace(/\s+\*\s+(?=\*\*|[A-Z])/g, '\n• ')
    // Remove visible bold markers
    .replace(/\*\*/g, '')
    // Clean excessive blank lines
    .replace(/\n{3,}/g, '\n\n')
    .trim();

  const citations =
    result.citations || [];

  // Opens the filing at the cited page with the supporting numbers or
  // sentences highlighted (the backend adds the highlights).
  const citationUrl = (c) => {
    const params = new URLSearchParams();
    const page = c.page || 1;

    params.set('page', page);
    (c.highlights || []).forEach((term) => params.append('hl', term));

    return `${fileUrl(c.filing_id)}?${params.toString()}#page=${page}`;
  };

  const figures =
    result.figures || [];

  /*
   * The backend/agent returns:
   *
   * suggested_questions: [
   *   "...",
   *   "...",
   *   "..."
   * ]
   *
   * We only display a maximum of three.
   */
  const suggestions = Array.isArray(
    result.suggested_questions
  )
    ? result.suggested_questions
        .filter(
          (q) =>
            typeof q === 'string' &&
            q.trim().length > 0
        )
        .slice(0, 3)
    : [];

  /*
   * Suggestions are intentionally collapsed initially.
   *
   * Each answer gets its own state, so opening the
   * suggestions for one answer does not open every
   * suggestion panel in the conversation.
   */
  const [
    showSuggestions,
    setShowSuggestions,
  ] = useState(false);

  return (
    <div>

      {/* =====================================================
          MAIN ANSWER
          ===================================================== */}

      <div className="answer-label">
        QUARTERLY ANALYSIS
      </div>

      <div className="answer-copy">
        {displayAnswer}
      </div>

      {/* =====================================================
          VERIFIED SOURCES
          ===================================================== */}

      {SHOW.sourceList &&
        citations.length > 0 && (
          <>
            <div className="evidence-label">
              VERIFIED SOURCES
            </div>

            <div className="citation-list">
              {citations.map((c, i) => (
                <a
                  key={i}
                  className="citation"
                  href={citationUrl(c)}
                  target="_blank"
                  rel="noopener"
                >
                  <span className="citation-icon">
                    ↗
                  </span>

                  {c.citation ||
                    '10-Q Statement'}
                </a>
              ))}
            </div>
          </>
        )}

      {/* =====================================================
          FIGURES
          ===================================================== */}

      {SHOW.figureChips &&
        figures.length > 0 && (
          <div className="figure-list">
            {figures
              .slice(0, 6)
              .map((f, i) => (
                <div
                  className="figure-chip"
                  key={i}
                >
                  {f.item}

                  <b>
                    {f.value}{' '}
                    {f.units || ''}
                  </b>
                </div>
              ))}
          </div>
        )}

      {/* =====================================================
          AI-GENERATED FOLLOW-UP QUESTIONS
          ===================================================== */}

      {suggestions.length > 0 && (
        <div className="ai-suggestions-wrapper">

          {/* -----------------------------------------------
              COLLAPSED BUTTON
              ----------------------------------------------- */}

          <button
            type="button"
            className={`ai-suggestions-toggle ${
              showSuggestions
                ? 'active'
                : ''
            }`}
            onClick={() =>
              setShowSuggestions(
                (prev) => !prev
              )
            }
            disabled={isSending}
          >
            <span className="ai-suggestions-toggle-left">

              <span className="ai-sparkle">
                ✦
              </span>

              <span>
                <strong>
                  AI follow-up questions
                </strong>

                <small>
                  Explore related research
                </small>
              </span>

            </span>

            <span
              className={`ai-toggle-arrow ${
                showSuggestions
                  ? 'open'
                  : ''
              }`}
            >
              ↓
            </span>
          </button>

          {/* -----------------------------------------------
              EXPANDED AI PANEL
              ----------------------------------------------- */}

          {showSuggestions && (
            <div className="ai-suggestions-panel">

              <div className="ai-suggestions-header">

                <div className="ai-panel-icon">
                  ✦
                </div>

                <div>
                  <div className="ai-panel-title">
                    Suggested research
                  </div>

                  <div className="ai-panel-description">
                    Based on your question
                    and the current filing
                  </div>
                </div>

              </div>

              <div className="ai-question-list">

                {suggestions.map(
                  (suggestion, index) => (
                    <button
                      key={`${suggestion}-${index}`}
                      type="button"
                      className="ai-question"
                      disabled={isSending}
                      onClick={() =>
                        onSuggestionClick(
                          suggestion
                        )
                      }
                    >

                      <span className="ai-question-number">
                        {index + 1}
                      </span>

                      <span className="ai-question-text">
                        {suggestion}
                      </span>

                      <span className="ai-question-arrow">
                        →
                      </span>

                    </button>
                  )
                )}

              </div>

              <div className="ai-panel-footer">
                ✦ Generated from the current
                conversation context
              </div>

            </div>
          )}

        </div>
      )}
    </div>
  );
}