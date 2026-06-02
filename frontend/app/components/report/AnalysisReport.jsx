'use client';
import { useRef, useEffect } from 'react';
import { T } from '../shared';

// ── Build the "Analyze this" message for the AI ────────────────────────────────
export function buildAnalyzeMessage(params, constraints, result, zoneSymbol) {
  const c  = constraints || {};
  const ov = c.exception_overrides || {};

  const maxCov   = ov.max_coverage_pct ?? c.max_coverage_pct;
  const maxHt    = ov.max_height_m     ?? c.max_height_m;
  const maxFsi   = ov.max_fsi          ?? c.max_fsi;
  const maxUnits = ov.max_units        ?? c.max_units;
  const frontMin = ov.front_yard_min_m ?? c.front_yard_min_m;
  const rearMin  = ov.rear_yard_min_m  ?? c.rear_yard_min_m;
  const sideMin  = ov.side_yard_min_m  ?? c.side_yard_min_m;

  const fmt = v => v != null ? v : '—';
  const chk = ok => ok ? '✓' : '✗';

  const lines = [
    `Compliance review — proposed development on this parcel:`,
    ``,
    `Zone: ${zoneSymbol || '—'} | Lot area: ${fmt(c.lot_area_m2)}m² | Frontage: ${fmt(c.lot_frontage_m)}m | Depth: ${fmt(c.lot_depth_m)}m`,
    c.exception_number ? `Exception #${c.exception_number} applies` : null,
    c.holding_zone ? `⛔ HOLDING ZONE — permit requires bylaw amendment` : null,
    c.zone_under_appeal ? `⚠️ Zone under appeal — provisions may change` : null,
    ``,
    `Proposed configuration:`,
    `• Footprint: ${params.footprint_m2}m² (${result.coverage_pct ?? '?'}% coverage)${maxCov != null ? ` — limit is ${maxCov}% (${result.footprint.max?.toFixed(1) ?? '?'}m²)` : ' — no fixed coverage limit'} ${chk(result.footprint.compliant)}`,
    `• GFA: ${params.gfa_m2}m² | FSI: ${result.live_fsi ?? '?'}${maxFsi != null ? ` — limit is FSI ${maxFsi} (${result.gfa.max?.toFixed(1) ?? '?'}m²)` : ' — no FSI limit'} ${chk(result.gfa.compliant)}`,
    `• Height: ${params.height_m}m (${result.floor_count} floor${result.floor_count !== 1 ? 's' : ''}, ${result.gfa_per_floor}m² each)${maxHt != null ? ` — limit is ${maxHt}m` : ' — no height limit'} ${chk(result.height.compliant)}`,
    maxUnits != null ? `• Units: ${params.units} — limit is ${maxUnits} ${chk(result.units.compliant)}` : null,
    `• Front yard: ${params.front_yard_m}m${frontMin != null ? ` — min is ${frontMin}m` : ' (contextual)'} ${chk(result.front_yard.compliant)}`,
    `• Rear yard: ${params.rear_yard_m}m${rearMin != null ? ` — min is ${rearMin}m` : ''} ${chk(result.rear_yard.compliant)}`,
    `• Side yard: ${params.side_yard_m}m${sideMin != null ? ` — min is ${sideMin}m` : ''} ${chk(result.side_yard.compliant)}`,
    result.angular_plane.applies ? `• Angular plane (45°): ${result.angular_plane.label} ${chk(result.angular_plane.compliant)}` : null,
    `• Parking: ${params.parking_spaces} space(s) ${chk(result.parking.compliant)}`,
    `• Bicycle: ${params.bicycle_spaces} space(s) ${chk(result.bicycle.compliant)}`,
    ``,
    result.violations.length === 0
      ? `Status: All parameters within limits.`
      : `Violations (${result.violations.length}):\n${result.violations.map(v => `  × ${v}`).join('\n')}`,
    ``,
    `Please: (1) confirm whether this configuration is fully compliant with the by-law and any exception, (2) identify any rules I may have missed (angular plane, landscaping, lot grading, accessory structures), (3) advise what to prepare for a building permit application.`,
  ].filter(l => l !== null);

  return lines.join('\n');
}

// ── Analysis report slide-over ─────────────────────────────────────────────────
export default function AnalysisReport({ content, streaming, zoneSymbol, onClose, onContinueChat }) {
  const endRef = useRef(null);

  useEffect(() => {
    if (streaming && endRef.current) endRef.current.scrollIntoView({ behavior: 'smooth' });
  }, [content, streaming]);

  function handleDownload() {
    const today = new Date().toLocaleDateString('en-CA');
    const zone  = (zoneSymbol || 'Zone').replace(/[^A-Za-z0-9]/g, '_');
    const blob  = new Blob([content], { type: 'text/plain' });
    const url   = URL.createObjectURL(blob);
    const a     = document.createElement('a');
    a.href = url; a.download = `ZoningReport_${zone}_${today}.txt`; a.click();
    URL.revokeObjectURL(url);
  }

  // Line-by-line markdown renderer (headings, bullets, numbered lists, bold, ✓/✗)
  function renderLine(line, i) {
    if (line.startsWith('## ')) return (
      <div key={i} style={{
        fontSize: 13.5, fontWeight: 700, color: T.accent, marginTop: 22, marginBottom: 7,
        paddingBottom: 6, borderBottom: `1px solid ${T.accentDim}`,
      }}>{line.slice(3)}</div>
    );
    if (line.startsWith('# ')) return (
      <div key={i} style={{ fontSize: 16, fontWeight: 800, color: T.t1, marginBottom: 14 }}>{line.slice(2)}</div>
    );
    if (/^[-•*] /.test(line)) return (
      <div key={i} style={{ display: 'flex', gap: 8, marginBottom: 5, paddingLeft: 4, alignItems: 'flex-start' }}>
        <span style={{ color: T.accent, flexShrink: 0, marginTop: 3, fontSize: 10 }}>●</span>
        <span style={{ fontSize: 13, color: T.t1, lineHeight: 1.6 }}>{renderInline(line.slice(2))}</span>
      </div>
    );
    if (/^\d+\. /.test(line)) {
      const num = line.match(/^\d+/)[0];
      return (
        <div key={i} style={{ display: 'flex', gap: 8, marginBottom: 5, paddingLeft: 4, alignItems: 'flex-start' }}>
          <span style={{ color: T.accent, flexShrink: 0, fontSize: 11, minWidth: 18, marginTop: 2, fontWeight: 700 }}>{num}.</span>
          <span style={{ fontSize: 13, color: T.t1, lineHeight: 1.6 }}>{renderInline(line.replace(/^\d+\.\s*/, ''))}</span>
        </div>
      );
    }
    if (/^[✓✗●]/.test(line)) return (
      <div key={i} style={{
        fontSize: 13, marginBottom: 4, lineHeight: 1.55, paddingLeft: 4,
        color: line.startsWith('✓') ? T.success : line.startsWith('✗') ? T.danger : T.t2,
      }}>{renderInline(line)}</div>
    );
    if (line.startsWith('**') && line.endsWith('**') && line.length > 4) return (
      <div key={i} style={{ fontSize: 13, fontWeight: 700, color: T.t1, marginBottom: 4, paddingLeft: 4 }}>
        {line.slice(2, -2)}
      </div>
    );
    if (line.trim() === '') return <div key={i} style={{ height: 8 }} />;
    return (
      <div key={i} style={{ fontSize: 13, color: T.t1, lineHeight: 1.65, marginBottom: 2, paddingLeft: 4 }}>
        {renderInline(line)}
      </div>
    );
  }

  function renderInline(text) {
    if (!text.includes('**') && !text.includes('`')) return text;
    const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/);
    return parts.map((p, j) => {
      if (p.startsWith('**') && p.endsWith('**')) return <strong key={j} style={{ fontWeight: 700, color: T.t1 }}>{p.slice(2, -2)}</strong>;
      if (p.startsWith('`')  && p.endsWith('`'))  return <code key={j} style={{ fontFamily: 'var(--font-mono)', fontSize: 11, background: 'var(--color-forest-wash)', padding: '1px 5px', borderRadius: 4, color: T.accent }}>{p.slice(1, -1)}</code>;
      return p;
    });
  }

  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 8000,
      background: 'rgba(0,0,0,0.55)', backdropFilter: 'blur(8px)',
      display: 'flex', alignItems: 'stretch', justifyContent: 'flex-end',
    }}>
      <div style={{
        width: '100%', maxWidth: 720, background: 'var(--color-bg-primary)',
        borderLeft: `1px solid ${T.borderHi}`,
        display: 'flex', flexDirection: 'column',
        boxShadow: '-20px 0 80px rgba(0,0,0,0.15)',
      }}>
        {/* ── Header ── */}
        <div style={{
          flexShrink: 0, padding: '13px 20px',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          borderBottom: `1px solid ${T.border}`,
          background: streaming ? 'var(--color-warn-bg)' : 'var(--color-ok-bg)',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <div style={{
              width: 7, height: 7, borderRadius: '50%', flexShrink: 0,
              background: streaming ? T.warn : T.success,
              boxShadow: `0 0 8px ${streaming ? T.warn : T.success}`,
            }} />
            <span style={{ fontSize: 14, fontWeight: 700, color: T.t1 }}>
              {streaming ? 'Generating Analysis…' : 'Compliance Analysis Report'}
            </span>
            {zoneSymbol && (
              <span style={{
                fontSize: 10, color: T.accent, background: T.accentDim,
                border: '1px solid var(--color-forest-border)', borderRadius: 5,
                padding: '2px 8px', fontFamily: 'var(--font-mono)', fontWeight: 700,
              }}>{zoneSymbol}</span>
            )}
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            {!streaming && content && (
              <>
                <button onClick={handleDownload} style={{
                  fontSize: 11, padding: '5px 11px', borderRadius: 6,
                  background: T.surface, border: `1px solid ${T.border}`,
                  color: T.t2, cursor: 'pointer', fontWeight: 500,
                }}>↓ Download</button>
                {onContinueChat && (
                  <button onClick={onContinueChat} style={{
                    fontSize: 11, padding: '5px 11px', borderRadius: 6,
                    background: 'var(--color-forest-wash)', border: '1px solid var(--color-forest-border)',
                    color: T.accent, cursor: 'pointer', fontWeight: 600,
                  }}>Continue in Chat →</button>
                )}
              </>
            )}
            <button onClick={onClose} style={{
              background: 'none', border: 'none', color: T.t3,
              cursor: 'pointer', fontSize: 18, lineHeight: 1, padding: '2px 6px',
            }}>✕</button>
          </div>
        </div>

        {/* ── Content ── */}
        <div style={{
          flex: 1, overflowY: 'auto', padding: '18px 24px',
          scrollbarWidth: 'thin', scrollbarColor: 'var(--color-border) transparent',
        }}>
          {!content && streaming && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16, paddingTop: 12 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 14, color: T.t2, fontSize: 13 }}>
                <div style={{
                  width: 18, height: 18, borderRadius: '50%', flexShrink: 0,
                  border: `2px solid ${T.accent}`, borderTopColor: 'transparent',
                  animation: 'cp-spin 0.8s linear infinite',
                }} />
                Running full RAG retrieval + Voyage reranking + gpt-4.1 synthesis…
              </div>
              <div style={{ fontSize: 11, color: T.t3, lineHeight: 1.6, paddingLeft: 32 }}>
                Fetching relevant By-law sections · Applying exception overrides · Building complete analysis<br />
                This typically takes 10–20 seconds.
              </div>
            </div>
          )}
          {content && content.split('\n').map(renderLine)}
          {streaming && content && (
            <span style={{
              display: 'inline-block', width: 2, height: 14,
              background: T.accent, marginLeft: 2, verticalAlign: 'middle', opacity: 0.8,
            }} />
          )}
          <div ref={endRef} />
        </div>

        {/* ── Footer ── */}
        {!streaming && content && (
          <div style={{
            flexShrink: 0, padding: '9px 20px', borderTop: `1px solid ${T.border}`,
            fontSize: 10, color: T.t3, lineHeight: 1.55,
            display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          }}>
            <span>
              Generated from City of Toronto GIS data and By-law 569-2013. Not legal advice — verify with a registered planner.
            </span>
            <span style={{ flexShrink: 0, marginLeft: 12, color: T.t3, fontFamily: 'var(--font-mono)' }}>
              {new Date().toLocaleDateString('en-CA')}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
