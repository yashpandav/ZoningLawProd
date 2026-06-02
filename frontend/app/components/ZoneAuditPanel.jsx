'use client';
import { useState, useEffect, useCallback } from 'react';

const T = {
  bg:      'var(--color-bg-primary)',
  surface: 'var(--color-bg-wash)',
  border:  'var(--color-border)',
  t1: 'var(--color-text-primary)', t2: 'var(--color-text-muted)', t3: 'var(--color-text-hint)',
  green:   '#1A5A35',
  amber:   '#7A5800',
  red:     '#8A2A20',
  violet:  '#B85A2A',
  blue:    '#1A4A35',
};

const STATUS_COLOR = {
  ok:        '#1A5A35',
  variance:  '#7A5800',
  violation: '#8A2A20',
  exempt:    '#B85A2A',
  na:        '#A8A098',
};

const STATUS_LABEL = {
  ok:        'OK',
  variance:  'Variance',
  violation: 'Violation',
  exempt:    'Exempt',
  na:        'N/A',
};

function CountBadge({ count, color, label }) {
  if (!count) return null;
  return (
    <div style={{
      display: 'flex', flexDirection: 'column', alignItems: 'center',
      padding: '8px 14px', borderRadius: 8,
      background: `${color}14`, border: `1px solid ${color}30`,
      minWidth: 60,
    }}>
      <span style={{ fontSize: 20, fontWeight: 800, color, fontVariantNumeric: 'tabular-nums' }}>
        {count}
      </span>
      <span style={{ fontSize: 9, color: T.t3, marginTop: 1 }}>{label}</span>
    </div>
  );
}

function ResultRow({ result, onAsk, zoneSymbol }) {
  const [open, setOpen] = useState(false);
  const color = STATUS_COLOR[result.status] || STATUS_COLOR.na;

  return (
    <div style={{
      borderBottom: `1px solid ${T.border}`,
      padding: '6px 0',
    }}>
      <div
        onClick={() => setOpen(o => !o)}
        style={{
          display: 'flex', alignItems: 'center', gap: 8,
          cursor: result.message ? 'pointer' : 'default',
        }}
      >
        <span style={{
          width: 52, fontSize: 9, fontWeight: 700, color,
          background: `${color}14`, padding: '2px 5px', borderRadius: 3,
          textAlign: 'center', flexShrink: 0,
        }}>
          {STATUS_LABEL[result.status]}
        </span>
        <span style={{ fontSize: 10, color: T.t1, flex: 1 }}>
          {result.param_key.replace(/_/g, ' ')}
        </span>
        {result.proposed != null && (
          <span style={{ fontSize: 9, color: T.t2, fontFamily: 'monospace' }}>
            {typeof result.proposed === 'number' ? result.proposed.toFixed(2) : String(result.proposed)}
            {result.limit != null && (
              <span style={{ color: T.t3 }}> / {typeof result.limit === 'number' ? result.limit.toFixed(2) : String(result.limit)}</span>
            )}
          </span>
        )}
        {result.message && (
          <span style={{ fontSize: 8, color: T.t3 }}>{open ? '▲' : '▼'}</span>
        )}
      </div>

      {open && result.message && (
        <div style={{ marginTop: 5, padding: '6px 8px', background: 'var(--color-bg-muted)', borderRadius: 5 }}>
          <div style={{ fontSize: 9.5, color, lineHeight: 1.5 }}>{result.message}</div>
          {result.citation && (
            <div style={{ fontSize: 9, color: T.t3, marginTop: 3 }}>{result.citation}</div>
          )}
          {onAsk && result.status !== 'ok' && result.status !== 'na' && (
            <button
              onClick={e => {
                e.stopPropagation();
                onAsk(
                  `Zoning audit for ${zoneSymbol}: parameter "${result.param_key.replace(/_/g, ' ')}" ` +
                  `has status "${STATUS_LABEL[result.status]}".\n${result.message}\nCitation: ${result.citation}\n` +
                  `Please explain how to achieve compliance.`
                );
              }}
              style={{
                marginTop: 6, fontSize: 9, padding: '3px 8px', borderRadius: 4, cursor: 'pointer',
                background: 'var(--color-forest-wash)', border: '1px solid var(--color-forest-border)',
                color: 'var(--color-forest-deep)',
              }}
            >
              Ask AI for guidance
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export default function ZoneAuditPanel({
  zoneSymbol,
  proposedParams,
  lotData,
  exceptionConstraints,
  overlayData,
  onAskClaude,
  apiBase,
}) {
  const [auditData,    setAuditData]    = useState(null);
  const [loading,      setLoading]      = useState(false);
  const [error,        setError]        = useState(null);
  const [filterStatus, setFilterStatus] = useState('all');
  const [auditTick,    setAuditTick]    = useState(0);

  const triggerAudit = useCallback(() => setAuditTick(n => n + 1), []);

  useEffect(() => {
    if (!zoneSymbol || Object.keys(proposedParams || {}).length === 0) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    const base = apiBase || '';
    fetch(`${base}/api/packgen/params/validate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        zone_symbol: zoneSymbol,
        proposed: proposedParams,
        lot_data: lotData || {},
        exception_constraints: exceptionConstraints || null,
        overlay_data: overlayData || null,
      }),
    })
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(data => { if (!cancelled) { setAuditData(data); setLoading(false); } })
      .catch(e => { if (!cancelled) { setError(String(e)); setLoading(false); } });
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auditTick, zoneSymbol]);

  if (!zoneSymbol) return null;

  const summary = auditData?.summary || {};
  const results = (auditData?.results || []).filter(r =>
    filterStatus === 'all' || r.status === filterStatus
  );
  const totalChecked = Object.values(summary).reduce((s, n) => s + n, 0);

  const TAB_ORDER = ['all', 'violation', 'variance', 'ok', 'exempt', 'na'];
  const TAB_COLOR = { all: T.t2, violation: T.red, variance: T.amber, ok: T.green, exempt: T.violet, na: T.t3 };
  const TAB_COUNT = {
    all: totalChecked,
    violation: summary.violation || 0,
    variance: summary.variance || 0,
    ok: summary.ok || 0,
    exempt: summary.exempt || 0,
    na: summary.na || 0,
  };

  return (
    <div style={{
      background: T.bg,
      border: `1px solid ${T.border}`,
      borderRadius: 10, overflow: 'hidden',
      display: 'flex', flexDirection: 'column',
      fontFamily: 'var(--font-sans, sans-serif)',
    }}>
      {/* Header */}
      <div style={{
        padding: '10px 14px 8px',
        borderBottom: `1px solid ${T.border}`,
        background: 'var(--color-bg-wash)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <span style={{ fontSize: 11, fontWeight: 700, color: T.t1 }}>Zone Audit</span>
          <span style={{ fontSize: 9, color: T.t3 }}>{zoneSymbol}</span>
        </div>
        <div style={{ fontSize: 9, color: T.t3, marginTop: 1 }}>
          By-law 569-2013 parameter compliance check
        </div>
      </div>

      {/* Summary badges */}
      {auditData && (
        <div style={{
          display: 'flex', gap: 8, padding: '10px 14px',
          borderBottom: `1px solid ${T.border}`,
          overflowX: 'auto', scrollbarWidth: 'none',
        }}>
          <CountBadge count={summary.violation || 0} color={T.red}    label="Violations" />
          <CountBadge count={summary.variance  || 0} color={T.amber}  label="Variances" />
          <CountBadge count={summary.ok        || 0} color={T.green}  label="Compliant" />
          <CountBadge count={summary.exempt    || 0} color={T.violet} label="Exempt" />
        </div>
      )}

      {/* Filter tabs */}
      {auditData && (
        <div style={{
          display: 'flex', gap: 1, padding: '6px 14px',
          borderBottom: `1px solid ${T.border}`,
          overflowX: 'auto', scrollbarWidth: 'none',
        }}>
          {TAB_ORDER.filter(t => TAB_COUNT[t] > 0 || t === 'all').map(tab => (
            <button
              key={tab}
              onClick={() => setFilterStatus(tab)}
              style={{
                fontSize: 9, padding: '3px 8px', borderRadius: 4, cursor: 'pointer',
                background: filterStatus === tab ? `${TAB_COLOR[tab]}18` : 'transparent',
                border: `1px solid ${filterStatus === tab ? TAB_COLOR[tab] + '40' : 'transparent'}`,
                color: filterStatus === tab ? TAB_COLOR[tab] : T.t3,
                fontWeight: filterStatus === tab ? 700 : 400,
                whiteSpace: 'nowrap',
              }}
            >
              {tab === 'all' ? 'All' : STATUS_LABEL[tab]}
              {TAB_COUNT[tab] > 0 && <span style={{ marginLeft: 4, opacity: 0.7 }}>({TAB_COUNT[tab]})</span>}
            </button>
          ))}
        </div>
      )}

      {/* Results list */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '0 14px', maxHeight: 360 }}>
        {loading && (
          <div style={{ fontSize: 10, color: T.t3, textAlign: 'center', padding: '20px 0' }}>
            Running audit…
          </div>
        )}
        {error && (
          <div style={{ fontSize: 10, color: T.red, padding: '12px 0' }}>
            Audit failed: {error}
          </div>
        )}
        {!loading && results.map((r, i) => (
          <ResultRow key={`${r.param_key}-${i}`} result={r} onAsk={onAskClaude} zoneSymbol={zoneSymbol} />
        ))}
        {!loading && auditData && results.length === 0 && (
          <div style={{ fontSize: 10, color: T.t3, textAlign: 'center', padding: '20px 0' }}>
            No results for this filter.
          </div>
        )}
        {!loading && !auditData && !error && (
          <div style={{ fontSize: 10, color: T.t3, textAlign: 'center', padding: '20px 0' }}>
            Set proposed parameter values to run compliance audit.
          </div>
        )}
      </div>

      {/* Refresh button */}
      <div style={{
        padding: '8px 14px', borderTop: `1px solid ${T.border}`,
        background: 'var(--color-bg-wash)',
      }}>
        <button
          onClick={triggerAudit}
          disabled={loading}
          style={{
            width: '100%', fontSize: 10, fontWeight: 600,
            padding: '6px 0', borderRadius: 6, cursor: loading ? 'default' : 'pointer',
            background: 'var(--color-forest-wash)', border: '1px solid var(--color-forest-border)',
            color: loading ? T.t3 : 'var(--color-forest-deep)',
          }}
        >
          {loading ? 'Running…' : 'Re-run Audit'}
        </button>
      </div>
    </div>
  );
}
