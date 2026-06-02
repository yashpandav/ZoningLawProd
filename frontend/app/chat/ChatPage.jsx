'use client';

import { useSearchParams, useRouter } from 'next/navigation';
import { useState, useEffect, useRef, useCallback } from 'react';
import './chat.css';
import MarkdownMessage from '../components/MarkdownMessage';
import ParameterTweaker from '../components/ParameterTweaker';

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || 'http://localhost:8000';

// ── Suggested starter questions — mode-aware ──────────────────────────────────
function getSuggestions(parcel, mode) {
  if (!parcel) return [];
  if (mode === 'quick') {
    // Short, factual lookups — ideal for Quick instant answers
    const s = [];
    if (parcel.zone_symbol) s.push(`What is the max height in the ${parcel.zone_symbol} zone?`);
    s.push('How many dwelling units can I build here?');
    s.push('What is the maximum lot coverage?');
    s.push('What are the parking requirements?');
    return s.slice(0, 4);
  } else {
    // Complex, multi-part questions — ideal for Full Analysis
    const s = [];
    if (parcel.exception_number) s.push(`How does exception #${parcel.exception_number} affect my build envelope?`);
    s.push('What are ALL requirements for a garden suite on this lot?');
    if (parcel.zone_symbol) s.push(`Give me a complete zoning compliance summary for this ${parcel.zone_symbol} parcel`);
    s.push('What can I build here and what are all the constraints?');
    return s.slice(0, 4);
  }
}

// ── Zone colour accent ─────────────────────────────────────────────────────────
const ZC = { R:'#8B9D4A',RD:'#8B9D4A',RS:'#8B9D4A',RT:'#8B9D4A',RM:'#C4A24B',RA:'#D4AA45',
  CL:'#4B7FC4',CR:'#4B7FC4',CRE:'#3A6BAF',E:'#C0392B',EI:'#C0392B',EL:'#E74C3C',
  I:'#9B7DC4',IS:'#9B7DC4',O:'#3D9B67',ON:'#3D9B67',UT:'#6B7280',NA:'#2D9B8A' };
function zoneAccent(sym) {
  if (!sym) return '#E8A95C';
  const s = sym.trim().split(/[\s(]/)[0].toUpperCase();
  return ZC[s] || '#E8A95C';
}

// ── Copy to clipboard helper ───────────────────────────────────────────────────
function useCopyToClipboard() {
  const [copiedId, setCopiedId] = useState(null);
  const copy = useCallback((text, id) => {
    navigator.clipboard.writeText(text).then(() => {
      setCopiedId(id);
      setTimeout(() => setCopiedId(null), 1800);
    });
  }, []);
  return [copiedId, copy];
}

export default function ChatPage() {
  const searchParams = useSearchParams();
  const router       = useRouter();

  const lat    = parseFloat(searchParams.get('lat') ?? '');
  const lng    = parseFloat(searchParams.get('lng') ?? '');
  const tabParam = searchParams.get('tab'); // 'tweaker' to open tweaker on load

  const [parcel,    setParcel]    = useState(null);
  const [loadErr,   setLoadErr]   = useState(null);
  const [loading,   setLoading]   = useState(true);
  const [panelOpen, setPanelOpen] = useState(true);
  const [mode,      setMode]      = useState('quick'); // 'quick' | 'full'
  const [messages,         setMessages]         = useState([]);
  const [input,            setInput]            = useState('');
  const [busy,             setBusy]             = useState(false);
  const [streamingContent, setStreamingContent] = useState('');
  const [isStreaming,      setIsStreaming]      = useState(false);
  const [copiedId,         copy]                = useCopyToClipboard();
  // Tab state: 'chat' | 'tweaker'
  const [activeTab,            setActiveTab]            = useState(tabParam === 'tweaker' ? 'tweaker' : 'chat');
  // Change 16: excOverrides lives here (not in ParameterTweaker) so it survives tab switches.
  // excOverridesFetched prevents re-fetching when the user toggles back to the tweaker tab.
  const [excOverrides,         setExcOverrides]         = useState({});
  const [excOverridesFetched,  setExcOverridesFetched]  = useState(false);

  const bottomRef  = useRef(null);
  const inputRef   = useRef(null);

  // ── Fetch parcel on mount ──────────────────────────────────────────────────
  useEffect(() => {
    if (isNaN(lat) || isNaN(lng)) {
      setLoadErr('No parcel selected. Go back and click a parcel on the map.');
      setLoading(false);
      return;
    }
    fetch(`${API_BASE}/api/parcel?lat=${lat}&lng=${lng}`)
      .then(r => r.json())
      .then(data => {
        if (data.found === false) {
          setLoadErr(data.message || 'No zoning data found for this location.');
        } else {
          setParcel(data);
        }
      })
      .catch(() => setLoadErr('Could not reach the backend. Is FastAPI running?'))
      .finally(() => setLoading(false));
  }, [lat, lng]);

  // ── Auto-scroll (also fires on each streaming token) ──────────────────────
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, busy, streamingContent]);

  // ── Send message (SSE streaming) ───────────────────────────────────────────
  const send = useCallback(async (text) => {
    const msg = (text || input).trim();
    if (!msg || !parcel || busy) return;
    setInput('');
    const activeMode = mode;
    const endpoint   = activeMode === 'quick' ? '/api/quick-chat' : '/api/chat';
    const history    = activeMode === 'full'
      ? messages.slice(-40).map(m => ({ role: m.role, content: m.content }))
      : [];

    setMessages(prev => [...prev, { id: Date.now(), role: 'user', content: msg }]);
    setBusy(true);
    setStreamingContent('');
    setIsStreaming(false);
    const _t0 = performance.now();

    try {
      const res = await fetch(`${API_BASE}${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lat: parcel.lat, lng: parcel.lng, message: msg, history }),
      });

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData?.detail || `HTTP ${res.status}`);
      }

      const reader  = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer      = '';
      let accumulated = '';
      let doneData    = null;

      setIsStreaming(true);

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const raw = line.slice(6).trim();
          if (!raw) continue;
          try {
            const event = JSON.parse(raw);
            if (event.type === 'token') {
              accumulated += event.content;
              setStreamingContent(accumulated);
            } else if (event.type === 'done') {
              doneData = event;
            } else if (event.type === 'error') {
              throw new Error(event.message);
            }
          } catch { /* ignore individual parse errors */ }
        }
      }

      const reply = doneData?.reply || accumulated;
      console.group(`⏱ [ChatPage] ${activeMode.toUpperCase()} — ${endpoint} (SSE)`);
      console.log(`  Total round-trip (incl streaming):  ${(performance.now() - _t0).toFixed(0)} ms`);
      console.log(`  Reply length:                       ${reply.length} chars`);
      console.log(`  Chunks used:                        ${doneData?.chunks_count ?? '?'}`);
      console.log(`  Sections used:`, doneData?.sections_used ?? []);
      console.groupEnd();

      setMessages(prev => [
        ...prev,
        { id: Date.now() + 1, role: 'assistant', content: reply, mode: activeMode },
      ]);
    } catch (err) {
      console.warn(`⏱ [ChatPage] ${endpoint} FAILED after ${(performance.now() - _t0).toFixed(0)} ms:`, err.message);
      setMessages(prev => [
        ...prev,
        { id: Date.now() + 1, role: 'assistant', content: `Error: ${err.message}`, mode: activeMode, isError: true },
      ]);
    } finally {
      setStreamingContent('');
      setIsStreaming(false);
      setBusy(false);
      inputRef.current?.focus();
    }
  }, [input, parcel, busy, mode, messages]);

  // ── Fetch exception constraint overrides when tweaker tab is opened ──────
  useEffect(() => {
    if (activeTab !== 'tweaker' || !parcel?.exception_number || excOverridesFetched) return;
    setExcOverridesFetched(true);
    fetch(
      `${API_BASE}/api/exception-constraints?exception_number=${parcel.exception_number}` +
      `&zone=${encodeURIComponent(parcel.zone_symbol || '')}`
    )
      .then(r => r.ok ? r.json() : {})
      .then(data => setExcOverrides(data || {}))
      .catch(() => {/* tweaker works without overrides */});
  }, [activeTab, parcel, excOverridesFetched]);

  // ── "Ask Claude why" from tweaker → switch to chat + auto-send ───────────
  const handleAskClaude = useCallback((question) => {
    setActiveTab('chat');
    send(question);
  }, [send]);

  const accent = zoneAccent(parcel?.zone_symbol);

  // ── Loading / error states ─────────────────────────────────────────────────
  if (loading) {
    return (
      <div className="cp-root cp-center">
        <div className="cp-spinner" />
        <span className="cp-muted" style={{ marginTop: 16 }}>Loading parcel…</span>
      </div>
    );
  }
  if (loadErr) {
    return (
      <div className="cp-root cp-center">
        <div className="cp-error-card">
          <span className="cp-error-icon">⚠</span>
          <p className="cp-error-text">{loadErr}</p>
          <button className="cp-back-btn" onClick={() => router.push('/')}>← Back to map</button>
        </div>
      </div>
    );
  }

  const suggestions = getSuggestions(parcel, mode);

  return (
    <div className="cp-root">

      {/* ── TOP BAR ─────────────────────────────────────────────────────────── */}
      <header className="cp-header">
        <div className="cp-header-left">
          <button className="cp-back" onClick={() => router.push('/')} title="Back to map">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
              <path d="M9 2L4 7l5 5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
            Map
          </button>
          <div className="cp-header-divider" />
          <div className="cp-zone-pill" style={{ '--accent': accent }}>
            <span className="cp-zone-dot" />
            <span className="cp-zone-sym">{parcel.zone_symbol}</span>
            {parcel.exception_number && (
              <span className="cp-exc-badge">Exc #{parcel.exception_number}</span>
            )}
          </div>
        </div>

        <div className="cp-header-center">
          <span className="cp-title">Zoning Assistant</span>
        </div>

        <div className="cp-header-right">
          {/* Panel toggle */}
          <button
            className={`cp-icon-btn${panelOpen ? ' active' : ''}`}
            onClick={() => setPanelOpen(v => !v)}
            title={panelOpen ? 'Hide parcel data' : 'Show parcel data'}
          >
            <svg width="15" height="15" viewBox="0 0 15 15" fill="none">
              <rect x="1" y="2" width="5" height="11" rx="1.2" stroke="currentColor" strokeWidth="1.4"/>
              <path d="M9 5h4M9 7.5h4M9 10h4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"/>
            </svg>
          </button>

          {/* Mode toggle — hidden when tweaker is active */}
          {activeTab === 'chat' && (
            <div className="cp-mode-toggle">
              <button
                className={`cp-mode-btn${mode === 'quick' ? ' active-quick' : ''}`}
                onClick={() => setMode('quick')}
                title="⚡ Quick — instant plain-English answer in ~3 seconds. Each question is stateless."
              >⚡ Quick</button>
              <button
                className={`cp-mode-btn${mode === 'full' ? ' active-full' : ''}`}
                onClick={() => setMode('full')}
                title="📋 Analysis — complete by-law citations with multi-turn conversation. Best for permit research."
              >📋 Analysis</button>
            </div>
          )}
        </div>
      </header>

      {/* ── TAB BAR ──────────────────────────────────────────────────────────── */}
      <div className="cp-tab-bar">
        <button
          className={`cp-tab${activeTab === 'chat' ? ' cp-tab-active' : ''}`}
          onClick={() => setActiveTab('chat')}
        >
          <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
            <path d="M11.5 7.5c0 2.21-2.239 4-5 4a5.83 5.83 0 01-2.405-.508L1.5 11.5l.658-1.97A3.86 3.86 0 011.5 7.5c0-2.21 2.239-4 5-4s5 1.79 5 4z"
              stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round"/>
          </svg>
          Chat
        </button>
        <button
          className={`cp-tab${activeTab === 'tweaker' ? ' cp-tab-active cp-tab-tweaker' : ''}`}
          onClick={() => setActiveTab('tweaker')}
        >
          <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
            <rect x="1.5" y="5.5" width="10" height="1.5" rx="0.75" fill="currentColor" opacity="0.6"/>
            <rect x="1.5" y="2" width="10" height="1.5" rx="0.75" fill="currentColor" opacity="0.6"/>
            <rect x="1.5" y="9" width="10" height="1.5" rx="0.75" fill="currentColor" opacity="0.6"/>
            <circle cx="4.5" cy="6.25" r="1.25" fill="currentColor"/>
            <circle cx="8.5" cy="2.75" r="1.25" fill="currentColor"/>
            <circle cx="6" cy="9.75" r="1.25" fill="currentColor"/>
          </svg>
          Parameter Tweaker
          {parcel?.exception_number && (
            <span className="cp-tab-exc-dot" title="Exception overrides will be applied" />
          )}
        </button>
      </div>

      {/* ── BODY ────────────────────────────────────────────────────────────── */}
      <div className="cp-body">

        {/* LEFT: Parcel data panel — always visible regardless of tab */}
        {panelOpen && (
          <aside className="cp-panel" style={{ '--accent': accent }}>
            <div className="cp-panel-inner">

              {/* Zone headline */}
              <div className="cp-panel-hero" style={{ borderLeftColor: accent }}>
                <div className="cp-panel-zone-sym">{parcel.zone_symbol}</div>
                <div className="cp-panel-zone-label">{parcel.zone_label}</div>
                <div className="cp-panel-coords">
                  {parcel.lat?.toFixed(5)}, {parcel.lng?.toFixed(5)}
                </div>
              </div>

              {/* Status banners */}
              {parcel.zone_under_appeal && (
                <div className="cp-banner cp-banner-warn">⚠ Under appeal — not in full force</div>
              )}
              {parcel.exception_number && (
                <div className="cp-banner cp-banner-exc">
                  <span>Exception #{parcel.exception_number}</span>
                  {parcel.chapter_links?.exception_chapter && (
                    <a href={parcel.chapter_links.exception_chapter.url} target="_blank" rel="noreferrer"
                      className="cp-banner-link">Ch.900 ↗</a>
                  )}
                </div>
              )}

              {/* Data grid */}
              <div className="cp-data-section">
                <div className="cp-data-label">Lot</div>
                <div className="cp-data-grid">
                  <DataRow label="Frontage" value={parcel.lot_frontage_m ? `${parcel.lot_frontage_m} m` : '—'} />
                  <DataRow label="Area"     value={parcel.lot_area_m2    ? `${parcel.lot_area_m2} m²`   : '—'} />
                  <DataRow label="Units"    value={parcel.max_units > 0  ? parcel.max_units              : '—'} />
                </div>
              </div>

              <div className="cp-data-section">
                <div className="cp-data-label">Density</div>
                <div className="cp-data-grid">
                  <DataRow label="Total FSI"   value={parcel.floor_space_index ?? '—'} />
                  <DataRow label="Coverage"    value={parcel.base_coverage_pct ? `${parcel.base_coverage_pct}%` : '—'} />
                  <DataRow label="Max units"   value={parcel.max_units > 0 ? parcel.max_units : '—'} />
                </div>
              </div>

              <div className="cp-data-section">
                <div className="cp-data-label">Overlays</div>
                <div className="cp-data-grid">
                  <DataRow label="Height"
                    value={parcel.height_overlay_m ? `${parcel.height_overlay_m} m` : 'No overlay'}
                    highlight={!!parcel.height_overlay_m}
                    href={parcel.chapter_links?.height_overlay_chapter?.url} pill="995" />
                  <DataRow label="Coverage"
                    value={parcel.coverage_overlay_pct ? `${parcel.coverage_overlay_pct}%` : 'No overlay'}
                    highlight={!!parcel.coverage_overlay_pct}
                    href={parcel.chapter_links?.coverage_overlay_chapter?.url} pill="995" />
                  <DataRow label="Parking"
                    value={parcel.parking_zone || 'Standard'}
                    href={parcel.chapter_links?.parking_regulations_chapter?.url} pill="200" />
                  <DataRow label="Road"
                    value={parcel.road_classification ?? '—'} />
                  {parcel.downtown_setback_applies && (
                    <DataRow label="Setback" value="Downtown rules" highlight
                      href={parcel.chapter_links?.building_setback_chapter?.url} pill="600" />
                  )}
                  {parcel.retail_frontage_required && (
                    <DataRow label="Retail" value="Frontage required" highlight
                      href={parcel.chapter_links?.retail_frontage_chapter?.url} pill="600" />
                  )}
                </div>
              </div>

              {/* Chapter links */}
              {parcel.chapter_links && (
                <div className="cp-data-section">
                  <div className="cp-data-label">By-law Chapters</div>
                  <div className="cp-chapters">
                    {Object.values(parcel.chapter_links).filter(Boolean).map(ch => (
                      <a key={ch.url} href={ch.url} target="_blank" rel="noreferrer"
                        className="cp-chapter-chip" title={ch.description}>
                        {ch.file}
                      </a>
                    ))}
                  </div>
                </div>
              )}

            </div>
          </aside>
        )}

        {/* RIGHT: Parameter Tweaker tab */}
        {activeTab === 'tweaker' && (
          <main className="cp-tweaker-area">
            <ParameterTweaker
              constraints={parcel?.constraints
                ? { ...parcel.constraints, exception_overrides: excOverrides }
                : null
              }
              zoneSymbol={parcel?.zone_symbol}
              onAskClaude={handleAskClaude}
              parcel={parcel}
              apiBase={API_BASE}
            />
          </main>
        )}

        {/* RIGHT: Chat area */}
        <main className={`cp-chat${activeTab !== 'chat' ? ' cp-chat-hidden' : ''}`}>

          {/* Messages */}
          <div className="cp-messages">
            {messages.length === 0 && !busy && (
              <div className="cp-empty">
                <div className={`cp-empty-icon${mode === 'quick' ? ' cp-empty-icon-quick' : ' cp-empty-icon-full'}`}>
                  {mode === 'quick' ? (
                    <svg width="26" height="26" viewBox="0 0 24 24" fill="none">
                      <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"
                        stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                    </svg>
                  ) : (
                    <svg width="26" height="26" viewBox="0 0 24 24" fill="none">
                      <path d="M9 12h6M9 16h4M7 4H5a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2V8l-6-4z"
                        stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                      <path d="M15 4v4h4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
                    </svg>
                  )}
                </div>
                <p className="cp-empty-title">
                  {mode === 'quick' ? '⚡ Instant Lookup' : '📋 Professional Analysis'}
                </p>
                <p className="cp-empty-sub">
                  {mode === 'quick'
                    ? 'Get key numbers and rules in ~3 seconds — each question is answered independently'
                    : 'Complete by-law citations with section references — conversation remembers context for follow-up questions'}
                </p>
                {suggestions.length > 0 && (
                  <div className="cp-suggestions">
                    {suggestions.map((s, i) => (
                      <button key={i} className={`cp-suggestion${mode === 'quick' ? ' cp-suggestion-quick' : ' cp-suggestion-full'}`} onClick={() => send(s)}>
                        {s}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}

            {messages.map((m, i) => (
              <div key={m.id} className={`cp-msg cp-msg-${m.role}`}
                style={{ animationDelay: `${i === messages.length - 1 ? 0 : 0}ms` }}>
                {m.role === 'assistant' && (
                  <div className="cp-msg-meta">
                    <span className={`cp-msg-badge ${m.mode === 'quick' ? 'badge-quick' : 'badge-full'}`}>
                      {m.mode === 'quick' ? '⚡ Instant' : '📋 Analysis'}
                    </span>
                    {m.isError && <span className="cp-msg-badge badge-err">Error</span>}
                  </div>
                )}
                <div className={`cp-msg-bubble ${m.role === 'assistant' ? (m.mode === 'quick' ? 'bubble-quick' : 'bubble-full') : 'bubble-user'}`}>
                  <div className="cp-msg-text">
                    {m.role === 'user'
                      ? m.content
                      : <MarkdownMessage content={m.content} />}
                  </div>
                </div>
                {m.role === 'assistant' && !m.isError && (
                  <div className="cp-msg-actions">
                    <button className="cp-action-btn" onClick={() => copy(m.content, m.id)}
                      title="Copy response">
                      {copiedId === m.id ? (
                        <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
                          <path d="M2 6l3 3 5-5" stroke="var(--color-ok-text)" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"/>
                        </svg>
                      ) : (
                        <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
                          <rect x="1.5" y="3" width="7" height="8" rx="1" stroke="currentColor" strokeWidth="1.2"/>
                          <path d="M4 3V2a1 1 0 011-1h4a1 1 0 011 1v7a1 1 0 01-1 1H8" stroke="currentColor" strokeWidth="1.2"/>
                        </svg>
                      )}
                      <span>{copiedId === m.id ? 'Copied' : 'Copy'}</span>
                    </button>
                  </div>
                )}
              </div>
            ))}

            {busy && !streamingContent && (
              <div className="cp-msg cp-msg-assistant">
                <div className="cp-msg-meta">
                  <span className={`cp-msg-badge ${mode === 'quick' ? 'badge-quick' : 'badge-full'}`}>
                    {mode === 'quick' ? '⚡ Instant' : '📋 Analysis'}
                  </span>
                </div>
                <div className={`cp-msg-bubble ${mode === 'quick' ? 'bubble-quick' : 'bubble-full'} bubble-thinking`}>
                  <div className="cp-thinking">
                    <span /><span /><span />
                  </div>
                </div>
              </div>
            )}

            {streamingContent && (
              <div className="cp-msg cp-msg-assistant">
                <div className="cp-msg-meta">
                  <span className={`cp-msg-badge ${mode === 'quick' ? 'badge-quick' : 'badge-full'}`}>
                    {mode === 'quick' ? '⚡ Instant' : '📋 Analysis'}
                  </span>
                </div>
                <div className={`cp-msg-bubble ${mode === 'quick' ? 'bubble-quick' : 'bubble-full'}`}>
                  <div className="cp-msg-text">
                    <MarkdownMessage content={streamingContent} />
                  </div>
                </div>
              </div>
            )}

            <div ref={bottomRef} style={{ height: 1 }} />
          </div>

          {/* Input bar */}
          <div className="cp-input-wrap">
            <div className="cp-input-row">
              <textarea
                ref={inputRef}
                className="cp-textarea"
                placeholder={mode === 'quick'
                  ? '⚡ Quick question — e.g. "Max height?" or "How many units?"'
                  : '📋 Ask for full analysis — e.g. "All garden suite requirements?"'}
                value={input}
                onChange={e => {
                  setInput(e.target.value);
                  e.target.style.height = 'auto';
                  e.target.style.height = Math.min(e.target.scrollHeight, 120) + 'px';
                }}
                onKeyDown={e => {
                  if (e.key === 'Enter' && !e.shiftKey && !busy) {
                    e.preventDefault();
                    send();
                  }
                }}
                disabled={busy}
                rows={1}
              />
              <button
                className={`cp-send${busy || !input.trim() ? ' cp-send-disabled' : ''} ${mode === 'quick' ? 'cp-send-quick' : 'cp-send-full'}`}
                onClick={() => send()}
                disabled={busy || !input.trim()}
                title="Send (Enter)"
              >
                {busy ? (
                  <div className="cp-spinner cp-spinner-sm" />
                ) : (
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                    <path d="M14 8H2M14 8l-4.5-4.5M14 8l-4.5 4.5" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"/>
                  </svg>
                )}
              </button>
            </div>
            <div className="cp-input-hint">
              {mode === 'quick' ? (
                <>Enter to send · <strong>⚡ Instant</strong> — each question answered independently · ~3s</>
              ) : (
                <>Enter to send · <strong>📋 Analysis</strong> — {
                  messages.filter(m => m.role === 'assistant').length > 0
                    ? `${messages.filter(m => m.role === 'assistant').length} turn${messages.filter(m => m.role === 'assistant').length !== 1 ? 's' : ''} remembered`
                    : 'conversation history will be remembered'
                } · ~10s</>
              )}
            </div>
          </div>

        </main>
      </div>
    </div>
  );
}

function DataRow({ label, value, highlight, href, pill }) {
  return (
    <div className="cp-dr">
      <span className="cp-dr-label">{label}</span>
      <span className={`cp-dr-value${highlight ? ' cp-dr-highlight' : ''}`}>
        {value}
        {href && pill && (
          <a href={href} target="_blank" rel="noreferrer" className="cp-dr-pill">{pill}</a>
        )}
      </span>
    </div>
  );
}
