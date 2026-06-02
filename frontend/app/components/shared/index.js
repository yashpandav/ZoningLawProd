'use client';
import { useState, useRef, useEffect } from 'react';

// ── Design tokens ──────────────────────────────────────────────────────────────
export const T = {
  accent:    '#1A4A35',
  accentDim: 'rgba(26,74,53,0.10)',
  success:   '#1A5A35',
  warn:      '#7A5800',
  danger:    '#8A2A20',
  violet:    '#B85A2A',
  violetDim: 'rgba(184,90,42,0.07)',
  border:    'var(--color-border)',
  borderHi:  'var(--color-border-strong)',
  surface:   'var(--color-bg-wash)',
  t1:        'var(--color-text-primary)',
  t2:        'var(--color-text-muted)',
  t3:        'var(--color-text-hint)',
};

// ── Data source provenance dot ─────────────────────────────────────────────────
export const SRC_META = {
  overlay: { color: '#7A5800', label: 'City overlay map' },
  postGIS: { color: '#1A4A35', label: 'City zoning database' },
  derived: { color: '#1A5A35', label: 'Derived from lot dimensions' },
  default: { color: '#A8A098', label: 'Zone default — approximate' },
};
export function Dot({ source }) {
  const s = SRC_META[source];
  if (!s) return null;
  return (
    <span title={s.label} style={{
      display: 'inline-block', width: 6, height: 6, borderRadius: '50%',
      background: s.color, marginLeft: 5, flexShrink: 0, verticalAlign: 'middle',
    }} />
  );
}

// ── Slider track colour ────────────────────────────────────────────────────────
export function tCol(pct, ok) {
  if (!ok)         return T.danger;
  if (pct == null) return T.accent;
  if (pct >= 95)   return T.danger;
  if (pct >= 80)   return T.warn;
  return T.success;
}

// ── Round helper ───────────────────────────────────────────────────────────────
export function r(v, step) { return Math.round(v / step) * step; }

// ── Slider ─────────────────────────────────────────────────────────────────────
export function Slider({ label, value, min, max, step, onChange, result, noLimit, src, sub }) {
  const pct  = result?.pct_used;
  const ok   = result?.compliant ?? true;
  const fill = max > min ? Math.min(((value - min) / (max - min)) * 100, 100) : 0;
  return (
    <div style={{ marginBottom: 15 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 5 }}>
        <span style={{ fontSize: 11, color: T.t2, fontWeight: 500, display: 'flex', alignItems: 'center', gap: 1 }}>
          {label}{src && <Dot source={src} />}
        </span>
        <span style={{ fontSize: 12, color: ok ? T.t1 : T.danger, fontFamily: 'var(--font-mono)', fontWeight: 700 }}>
          {result?.label || `${value}`}
        </span>
      </div>
      <div style={{ position: 'relative', height: 7, marginBottom: 4 }}>
        <div style={{ position: 'absolute', inset: 0, borderRadius: 4, background: 'var(--color-border)' }} />
        <div style={{
          position: 'absolute', top: 0, left: 0, height: '100%', borderRadius: 4,
          width: `${fill}%`, background: tCol(pct, ok),
          transition: 'width 50ms ease, background 150ms ease',
        }} />
        <input type="range" min={min} max={max} step={step} value={value}
          onChange={e => onChange(parseFloat(e.target.value))}
          style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', opacity: 0, cursor: 'pointer', zIndex: 1 }}
        />
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ fontSize: 9, color: T.t3 }}>{min}</span>
        {sub && <span style={{ fontSize: 9, color: T.t2 }}>{sub}</span>}
        <span style={{ fontSize: 9, color: noLimit ? T.accent : T.t3 }}>{noLimit ? 'no limit' : max}</span>
      </div>
    </div>
  );
}

// ── Section header ─────────────────────────────────────────────────────────────
export function ColHead({ children }) {
  return (
    <div style={{
      fontSize: 9, fontWeight: 700, color: T.t3, letterSpacing: '0.12em',
      textTransform: 'uppercase', marginBottom: 11, paddingBottom: 6,
      borderBottom: `1px solid ${T.border}`,
    }}>{children}</div>
  );
}

export function SectionDiv() {
  return <div style={{ marginTop: 16, marginBottom: 16, borderTop: `1px solid ${T.border}` }} />;
}

// ── Compliance chip ────────────────────────────────────────────────────────────
// Change 6: onAsk is now a pre-bound handler (built in parent); Chip just calls it.
export function Chip({ label, compliant, onAsk }) {
  const ok = compliant;
  return (
    <div style={{
      display: 'inline-flex', alignItems: 'center', gap: 4,
      padding: '3px 9px', borderRadius: 20, flexShrink: 0,
      background: ok ? 'var(--color-ok-bg)' : 'var(--color-violation-bg)',
      border: `1px solid ${ok ? 'var(--color-ok-border)' : 'var(--color-violation-border)'}`,
      fontSize: 10, color: ok ? T.success : T.danger, fontWeight: 500, whiteSpace: 'nowrap',
    }}>
      <span style={{ fontSize: 7, lineHeight: 1 }}>{ok ? '●' : '✕'}</span>
      {label}
      {!ok && onAsk && (
        <button
          onClick={onAsk}
          style={{
            fontSize: 9, color: 'var(--color-violation-text)', textDecoration: 'underline',
            cursor: 'pointer', background: 'none', border: 'none', padding: 0, marginLeft: 2,
          }}
        >fix?</button>
      )}
    </div>
  );
}

// ── Violations banner ──────────────────────────────────────────────────────────
export function ViolationsBanner({ violations }) {
  if (!violations.length) return null;
  return (
    <div style={{
      flexShrink: 0, padding: '9px 16px',
      background: 'var(--color-violation-bg)',
      borderBottom: '1px solid var(--color-violation-border)',
    }}>
      <div style={{
        fontSize: 9.5, fontWeight: 700, color: T.danger,
        letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: 5,
      }}>
        ✕ {violations.length} violation{violations.length !== 1 ? 's' : ''} — fix before applying for a permit
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
        {violations.map((v, i) => (
          <span key={i} style={{ fontSize: 10.5, color: 'var(--color-violation-text)', lineHeight: 1.45 }}>→ {v}</span>
        ))}
      </div>
    </div>
  );
}

// ── Summary metrics bar ────────────────────────────────────────────────────────
export function SummaryMetrics({ result, onAsk, zoneSymbol }) {
  const gs = result.garden_suite;
  return (
    <div style={{
      flexShrink: 0, padding: '8px 16px',
      borderBottom: `1px solid ${T.border}`,
      display: 'flex', gap: 6, overflowX: 'auto', scrollbarWidth: 'none',
      alignItems: 'stretch',
    }}>
      {result.remaining_lot_m2 != null && (
        <Metric
          label="Remaining lot"
          value={`${Math.round(result.remaining_lot_m2)}m²`}
          color={result.remaining_lot_m2 < 50 ? T.warn : T.t1}
          title="Lot area not covered by building footprint"
        />
      )}
      {result.angular_plane.applies && (
        <Metric
          label="45° plane"
          value={result.angular_plane.compliant ? '✓ OK' : '✕ Fail'}
          color={result.angular_plane.compliant ? T.success : T.danger}
          title={result.angular_plane.label}
        />
      )}
      {gs.applies && (
        <div
          onClick={() => gs.feasible && onAsk && onAsk(
            `Can I build a garden suite on this ${zoneSymbol} lot? ` +
            `My current configuration has a ${result.remaining_lot_m2 ? Math.round(result.remaining_lot_m2) : '?'}m² remaining lot area. ` +
            `What are all the requirements under By-law 156-2023?`
          )}
          title={gs.feasible ? `${gs.reason}\n${gs.note}` : gs.reason}
          style={{
            display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
            padding: '5px 12px', borderRadius: 7, flexShrink: 0,
            cursor: gs.feasible && onAsk ? 'pointer' : 'default',
            background: gs.feasible ? 'var(--color-ok-bg)' : T.surface,
            border: `1px solid ${gs.feasible ? 'var(--color-ok-border)' : T.border}`,
            minWidth: 88, transition: 'background 0.15s',
          }}
        >
          <span style={{ fontSize: 8, color: T.t3, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 3 }}>Garden Suite</span>
          <span style={{ fontSize: 12, fontWeight: 700, color: gs.feasible ? T.success : T.t3 }}>
            {gs.feasible ? '✓ Possible' : '✗ Check'}
          </span>
          {gs.feasible && (
            <span style={{ fontSize: 8, color: 'var(--color-ok-text)', marginTop: 2, opacity: 0.7 }}>tap to ask AI →</span>
          )}
        </div>
      )}
    </div>
  );
}

export function Metric({ label, value, color, title }) {
  return (
    <div title={title} style={{
      display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
      padding: '5px 12px', borderRadius: 7, flexShrink: 0,
      background: T.surface, border: `1px solid ${T.border}`, minWidth: 68,
    }}>
      <span style={{ fontSize: 8, color: T.t3, letterSpacing: '0.09em', textTransform: 'uppercase', marginBottom: 3, whiteSpace: 'nowrap' }}>{label}</span>
      <span style={{ fontSize: 13, fontWeight: 700, fontFamily: 'var(--font-mono)', color: color || T.t1 }}>
        {value}
      </span>
    </div>
  );
}
