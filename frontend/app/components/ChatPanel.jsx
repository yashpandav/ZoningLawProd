'use client';
import { useRef, useEffect, useState, useCallback } from 'react';
import MarkdownMessage from './MarkdownMessage';

export default function ChatPanel({
  chatHist,
  chatBusy,
  chatMsg,
  setChatMsg,
  onSend,
  mode,
  onModeChange,
  streamingContent = '',
  isStreaming = false,
  sessionResume = null,
}) {
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [chatHist, chatBusy, streamingContent]);

  const isQuick       = mode === 'quick';
  const assistTurns   = chatHist.filter(m => m.role === 'assistant').length;

  const [feedback, setFeedback] = useState({});

  const sendFeedback = useCallback(async (msg, rating) => {
    if (!msg.message_id || !msg.session_id) return;
    setFeedback(f => ({ ...f, [msg.message_id]: 'pending' }));
    try {
      const base = process.env.NEXT_PUBLIC_API_BASE || 'http://localhost:8000';
      const userId = (typeof localStorage !== 'undefined' && localStorage.getItem('zoning_user_id')) || 'anonymous';
      await fetch(`${base}/api/feedback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message_id: msg.message_id, session_id: msg.session_id, user_id: userId, rating }),
      });
    } catch { /* best-effort */ }
    setFeedback(f => ({ ...f, [msg.message_id]: rating }));
  }, []);

  return (
    <div style={{ padding: '16px 16px 20px' }}>

      {/* ── Header ──────────────────────────────────────────────────────── */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <svg style={{ width: 13, height: 13, color: 'var(--color-text-hint)' }} fill="currentColor" viewBox="0 0 20 20">
            <path fillRule="evenodd" d="M18 10c0 3.866-3.582 7-8 7a8.841 8.841 0 01-4.083-.98L2 17l1.338-3.123C2.493 12.767 2 11.434 2 10c0-3.866 3.582-7 8-7s8 3.134 8 7zM7 9H5v2h2V9zm8 0h-2v2h2V9zM9 9h2v2H9V9z" clipRule="evenodd" />
          </svg>
          <span style={{ fontSize: 10, fontWeight: 700, color: 'var(--color-text-hint)', textTransform: 'uppercase', letterSpacing: '0.12em' }}>
            Ask about this parcel
          </span>
        </div>
        {!isQuick && assistTurns > 0 && (
          <span style={{
            fontSize: 9, fontFamily: 'var(--font-mono)',
            color: 'var(--color-copper)', background: 'var(--color-copper-wash)',
            border: '1px solid var(--color-copper-border)',
            padding: '2px 6px', borderRadius: 'var(--radius-sm)',
          }}>
            {assistTurns} turn{assistTurns !== 1 ? 's' : ''}
          </span>
        )}
      </div>

      {/* ── Mode toggle ─────────────────────────────────────────────────── */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, marginBottom: 10 }}>

        <button
          onClick={() => onModeChange('quick')}
          title="Instant lookup — plain-English answer in ~3 seconds. Each question is independent."
          style={{
            display: 'flex', flexDirection: 'column', alignItems: 'flex-start',
            padding: '8px 10px', borderRadius: 'var(--radius-md)', textAlign: 'left',
            cursor: 'pointer', transition: 'all 150ms ease',
            background: isQuick ? 'var(--color-forest-wash)' : 'transparent',
            border: `1px solid ${isQuick ? 'var(--color-forest-border)' : 'var(--color-border)'}`,
          }}
        >
          <span style={{ fontSize: 10, fontWeight: 700, color: isQuick ? 'var(--color-forest-deep)' : 'var(--color-text-hint)', letterSpacing: '0.02em' }}>
            ⚡ Quick
          </span>
          <span style={{ fontSize: 9, marginTop: 2, lineHeight: 1.3, color: isQuick ? 'var(--color-forest-mid)' : 'var(--color-text-hint)' }}>
            Instant · No history
          </span>
        </button>

        <button
          onClick={() => onModeChange('full')}
          title="Full analysis — complete by-law citations, multi-turn conversation. Best for permit research."
          style={{
            display: 'flex', flexDirection: 'column', alignItems: 'flex-start',
            padding: '8px 10px', borderRadius: 'var(--radius-md)', textAlign: 'left',
            cursor: 'pointer', transition: 'all 150ms ease',
            background: !isQuick ? 'var(--color-copper-wash)' : 'transparent',
            border: `1px solid ${!isQuick ? 'var(--color-copper-border)' : 'var(--color-border)'}`,
          }}
        >
          <span style={{ fontSize: 10, fontWeight: 700, color: !isQuick ? 'var(--color-copper)' : 'var(--color-text-hint)', letterSpacing: '0.02em' }}>
            📋 Analysis
          </span>
          <span style={{ fontSize: 9, marginTop: 2, lineHeight: 1.3, color: !isQuick ? 'var(--color-copper)' : 'var(--color-text-hint)' }}>
            Citations · History
          </span>
        </button>

      </div>

      {/* ── Mode description ────────────────────────────────────────────── */}
      <div style={{
        marginBottom: 10, fontSize: 10, lineHeight: 1.5,
        color: isQuick ? 'var(--color-forest-mid)' : 'var(--color-copper)',
      }}>
        {isQuick
          ? 'Key facts in ~3 seconds — every question is answered fresh'
          : 'Full citations + follow-up questions remember context'}
      </div>

      {/* ── Session resume banner ───────────────────────────────────────── */}
      {sessionResume && chatHist.length > 0 && (
        <div style={{
          marginBottom: 10, padding: '10px 12px', borderRadius: 'var(--radius-md)',
          background: 'var(--color-forest-wash)', border: '1px solid var(--color-forest-border)',
          fontSize: 11, lineHeight: 1.4,
        }}>
          <div style={{ color: 'var(--color-forest-deep)', fontWeight: 600, marginBottom: 3, textTransform: 'uppercase', letterSpacing: '0.08em', fontSize: 9 }}>
            Session resumed · {Math.floor(sessionResume.message_count / 2)} prior turns
          </div>
          {sessionResume.summary ? (
            <div style={{ color: 'var(--color-text-muted)', overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' }}>
              {sessionResume.summary.slice(0, 160)}
            </div>
          ) : (
            <div style={{ color: 'var(--color-text-hint)', fontStyle: 'italic' }}>Previous conversation loaded</div>
          )}
        </div>
      )}

      {/* ── Message list ────────────────────────────────────────────────── */}
      <div style={{ maxHeight: 280, minHeight: 60, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 12 }} className="chat-scroll">

        {chatHist.length === 0 && !chatBusy && (
          <div style={{ fontSize: 12, color: 'var(--color-text-hint)', fontStyle: 'italic', lineHeight: 1.6 }}>
            {isQuick
              ? 'Try: "Max height?" · "How many units?" · "Parking requirement?"'
              : 'Try: "All requirements for a garden suite?" · "Explain this exception"'}
          </div>
        )}

        {chatHist.map((m, i) => (
          <div key={i}>
            {m.role === 'user' ? (
              <div style={{
                alignSelf: 'flex-end', fontSize: 13, maxWidth: '85%', marginLeft: 'auto',
                padding: '10px 14px', borderRadius: 'var(--radius-lg)',
                borderBottomRightRadius: 'var(--radius-sm)',
                background: 'var(--color-forest-wash)',
                border: '1px solid var(--color-forest-border)',
                color: 'var(--color-forest-deep)',
              }}>
                {m.content}
              </div>
            ) : (
              <div style={{
                alignSelf: 'flex-start', fontSize: 13,
                padding: '10px 14px', borderRadius: 'var(--radius-lg)',
                borderBottomLeftRadius: 'var(--radius-sm)',
                background: 'var(--color-bg-primary)',
                border: `1px solid ${m.mode === 'quick' ? 'var(--color-forest-border)' : 'var(--color-copper-border)'}`,
                color: 'var(--color-text-secondary)',
              }}>
                <span style={{
                  display: 'block', marginBottom: 6, fontSize: 9, fontWeight: 700,
                  textTransform: 'uppercase', letterSpacing: '0.1em',
                  color: m.mode === 'quick' ? 'var(--color-forest-deep)' : 'var(--color-copper)',
                }}>
                  {m.mode === 'quick' ? '⚡ Quick' : '📋 Analysis'}
                </span>
                <MarkdownMessage content={m.content} />
                {m.mode !== 'quick' && m.message_id && (() => {
                  const fb = feedback[m.message_id];
                  if (fb && fb !== 'pending') {
                    return (
                      <div style={{ marginTop: 8, fontSize: 9, color: 'var(--color-text-hint)' }}>
                        Thanks for the feedback
                      </div>
                    );
                  }
                  return (
                    <div style={{ marginTop: 8, display: 'flex', gap: 6, alignItems: 'center' }}>
                      <button
                        disabled={fb === 'pending'}
                        onClick={() => sendFeedback(m, 1)}
                        style={{ fontSize: 13, opacity: 0.35, cursor: 'pointer', background: 'transparent', border: 0, padding: 0, lineHeight: 1 }}
                        title="Helpful"
                      >👍</button>
                      <button
                        disabled={fb === 'pending'}
                        onClick={() => sendFeedback(m, -1)}
                        style={{ fontSize: 13, opacity: 0.35, cursor: 'pointer', background: 'transparent', border: 0, padding: 0, lineHeight: 1 }}
                        title="Not helpful"
                      >👎</button>
                    </div>
                  );
                })()}
              </div>
            )}
          </div>
        ))}

        {/* Thinking indicator */}
        {chatBusy && !streamingContent && (
          <div style={{
            alignSelf: 'flex-start', display: 'flex', alignItems: 'center', gap: 8,
            padding: '10px 14px', borderRadius: 'var(--radius-lg)',
            borderBottomLeftRadius: 'var(--radius-sm)', fontSize: 13,
            background: 'var(--color-bg-primary)',
            border: `1px solid ${isQuick ? 'var(--color-forest-border)' : 'var(--color-copper-border)'}`,
            color: isQuick ? 'var(--color-forest-mid)' : 'var(--color-copper)',
          }}>
            <div className="spin" />
            {isQuick ? '⚡ Looking up…' : '📋 Analyzing…'}
          </div>
        )}

        {/* Streaming response */}
        {streamingContent && (
          <div style={{
            alignSelf: 'flex-start', fontSize: 13,
            padding: '10px 14px', borderRadius: 'var(--radius-lg)',
            borderBottomLeftRadius: 'var(--radius-sm)',
            background: 'var(--color-bg-primary)',
            border: `1px solid ${isQuick ? 'var(--color-forest-border)' : 'var(--color-copper-border)'}`,
            color: 'var(--color-text-secondary)',
          }}>
            <span style={{
              display: 'block', marginBottom: 6, fontSize: 9, fontWeight: 700,
              textTransform: 'uppercase', letterSpacing: '0.1em',
              color: isQuick ? 'var(--color-forest-deep)' : 'var(--color-copper)',
            }}>
              {isQuick ? '⚡ Quick' : '📋 Analysis'}
            </span>
            <MarkdownMessage content={streamingContent} />
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* ── Input row ───────────────────────────────────────────────────── */}
      <div style={{ display: 'flex', gap: 8 }}>
        <input
          style={{
            flex: 1, borderRadius: 'var(--radius-md)', padding: '9px 12px',
            fontSize: 13, color: 'var(--color-text-primary)',
            background: 'var(--color-bg-muted)', border: '1px solid var(--color-border)',
            outline: 'none', transition: 'border-color 150ms ease', fontFamily: 'var(--font-sans)',
            opacity: chatBusy ? 0.5 : 1,
          }}
          onFocus={e => { e.currentTarget.style.borderColor = 'var(--color-forest)'; }}
          onBlur={e => { e.currentTarget.style.borderColor = 'var(--color-border)'; }}
          type="text"
          placeholder={isQuick ? 'Quick question…' : 'Ask for full analysis…'}
          value={chatMsg}
          onChange={e => setChatMsg(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && !chatBusy && onSend()}
          disabled={chatBusy}
        />
        <button
          style={{
            padding: '9px 14px', color: '#FFFFFF', borderRadius: 'var(--radius-md)',
            fontSize: 13, fontWeight: 500, border: 'none', cursor: 'pointer',
            transition: 'background 150ms ease',
            opacity: chatBusy || !chatMsg.trim() ? 0.4 : 1,
            background: isQuick ? 'var(--color-forest-deep)' : 'var(--color-copper)',
            cursor: chatBusy || !chatMsg.trim() ? 'not-allowed' : 'pointer',
          }}
          onClick={onSend}
          disabled={chatBusy || !chatMsg.trim()}
        >
          {isQuick ? '⚡' : '→'}
        </button>
      </div>

    </div>
  );
}
