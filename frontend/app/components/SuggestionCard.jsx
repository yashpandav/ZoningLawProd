'use client';

const T = {
  bg:      'var(--color-bg-primary)',
  surface: 'var(--color-bg-wash)',
  border:  'var(--color-border)',
  t1: 'var(--color-text-primary)', t2: 'var(--color-text-muted)', t3: 'var(--color-text-hint)',
  green:   'var(--color-ok-text)',
  violet:  'var(--color-copper)',
};

// Strip fixed width/height from the <svg> root element and replace with 100%/100%
// so the SVG scales to fill whatever container it's placed in.
// preserveAspectRatio="xMidYMid meet" keeps the floor plan proportional.
function fitSvg(raw) {
  if (!raw) return raw;
  return raw
    .replace(/(<svg\b[^>]*?)\s+width="[^"]*"/i, '$1')
    .replace(/(<svg\b[^>]*?)\s+height="[^"]*"/i, '$1')
    .replace(
      /<svg\b/i,
      '<svg width="100%" height="100%" preserveAspectRatio="xMidYMid meet"',
    );
}

function ScorePip({ score }) {
  const color = score >= 0.7 ? 'var(--color-ok-text)' : score >= 0.4 ? 'var(--color-warn-text)' : 'var(--color-text-hint)';
  const pct = Math.round(score * 100);
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
      <div style={{ flex: 1, height: 3, background: 'var(--color-border)', borderRadius: 2, overflow: 'hidden' }}>
        <div style={{ width: `${pct}%`, height: '100%', background: color, borderRadius: 2 }} />
      </div>
      <span style={{ fontSize: 9, color, minWidth: 26, textAlign: 'right' }}>{pct}%</span>
    </div>
  );
}

/**
 * SuggestionCard — horizontal layout: SVG preview left (fixed 150×160), content right.
 *
 * Props:
 *   card       {typology_id, label, rationale, est_gfa_m2, est_units,
 *               est_height_m, est_storeys, preview_svg, suitability_score}
 *   rank       int (1-based)
 *   selected   bool
 *   onSelect   (card) => void
 *   onGenerate (card) => void
 *   generating bool
 */
export default function SuggestionCard({ card, rank, selected, onSelect, onGenerate, generating }) {
  if (!card) return null;

  const rankBg    = rank === 1 ? 'var(--color-ok-bg)'     : rank === 2 ? 'var(--color-forest-wash)' : 'var(--color-bg-muted)';
  const rankBdr   = rank === 1 ? 'var(--color-ok-border)' : rank === 2 ? 'var(--color-forest-border)' : T.border;
  const rankColor = rank === 1 ? 'var(--color-ok-text)'   : rank === 2 ? 'var(--color-forest-deep)'  : 'var(--color-text-muted)';

  return (
    <div
      onClick={() => onSelect?.(card)}
      style={{
        display:       'flex',
        flexDirection: 'row',
        background:    selected ? 'var(--color-forest-wash)' : T.surface,
        border:        `1px solid ${selected ? 'var(--color-forest-border)' : T.border}`,
        borderRadius:  10,
        overflow:      'hidden',
        cursor:        'pointer',
        transition:    'border-color 0.15s, background 0.15s',
        fontFamily:    'var(--font-sans, sans-serif)',
        // Fixed card height so cards stay uniform and the SVG never stretches them
        height:        160,
      }}
    >
      {/* ── Left: SVG preview (fixed 150 × 160) ───────────────────────────── */}
      <div style={{
        width:       150,
        height:      160,
        flexShrink:  0,
        position:    'relative',
        background:  'var(--color-bg-surface)',
        borderRight: `1px solid ${T.border}`,
        overflow:    'hidden',
      }}>
        {card.preview_svg ? (
          <div
            style={{
              position: 'absolute',
              inset:    0,
              display:  'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
            dangerouslySetInnerHTML={{ __html: fitSvg(card.preview_svg) }}
          />
        ) : (
          <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <span style={{ fontSize: 9, color: T.t3 }}>No preview</span>
          </div>
        )}
      </div>

      {/* ── Right: card content ────────────────────────────────────────────── */}
      <div style={{
        flex:          1,
        minWidth:      0,
        padding:       '10px 14px',
        display:       'flex',
        flexDirection: 'column',
        gap:           5,
        overflow:      'hidden',
      }}>
        {/* Header: rank badge + label + radio */}
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 7, flexShrink: 0 }}>
          <span style={{
            fontSize: 9, fontWeight: 700, color: rankColor,
            background: rankBg, border: `1px solid ${rankBdr}`,
            padding: '1px 6px', borderRadius: 4, flexShrink: 0, marginTop: 1,
          }}>
            {rank === 1 ? '✨ #1' : `#${rank}`}
          </span>
          <span style={{ fontSize: 12, fontWeight: 700, color: T.t1, lineHeight: 1.3, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {card.label}
          </span>
          <div style={{
            width: 16, height: 16, borderRadius: '50%', flexShrink: 0,
            border: `2px solid ${selected ? 'var(--color-forest-deep)' : T.border}`,
            background: selected ? 'var(--color-forest-deep)' : 'transparent',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}>
            {selected && <div style={{ width: 5, height: 5, borderRadius: '50%', background: '#fff' }} />}
          </div>
        </div>

        {/* Rationale — 2-line clamp so it doesn't overflow */}
        <p style={{
          fontSize: 10.5, color: T.t2, margin: 0, lineHeight: 1.5,
          flex: 1,
          overflow: 'hidden',
          display: '-webkit-box',
          WebkitLineClamp: 3,
          WebkitBoxOrient: 'vertical',
        }}>
          {card.rationale}
        </p>

        {/* Stats chips + suitability bar */}
        <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap', alignItems: 'center', flexShrink: 0 }}>
          {[
            [`${card.est_units} unit${card.est_units !== 1 ? 's' : ''}`, 'var(--color-copper)',      'var(--color-copper-wash)',  'var(--color-copper-border)'],
            [`${card.est_gfa_m2} m²`,                                     'var(--color-forest-deep)', 'var(--color-forest-wash)',  'var(--color-forest-border)'],
            [`${card.est_storeys} fl`,                                     T.t2,                       T.surface,                   T.border],
            [`~${card.est_height_m}m`,                                     T.t3,                       T.surface,                   T.border],
          ].map(([label, color, bg, bdr]) => (
            <span key={label} style={{ fontSize: 8.5, color, background: bg, border: `1px solid ${bdr}`, borderRadius: 3, padding: '1px 5px', flexShrink: 0 }}>
              {label}
            </span>
          ))}
          <div style={{ flex: 1, minWidth: 40 }}>
            <ScorePip score={card.suitability_score} />
          </div>
        </div>

        {/* Generate button */}
        <button
          onClick={e => { e.stopPropagation(); onGenerate?.(card); }}
          disabled={generating}
          style={{
            width: '100%', flexShrink: 0,
            fontSize: 10, fontWeight: 600, padding: '5px 0', borderRadius: 6,
            cursor: generating ? 'default' : 'pointer',
            background: generating ? 'var(--color-bg-muted)' : selected ? 'var(--color-forest-deep)' : 'var(--color-forest-wash)',
            border: `1px solid ${generating ? T.border : selected ? 'var(--color-forest)' : 'var(--color-forest-border)'}`,
            color: generating ? T.t3 : selected ? '#fff' : 'var(--color-forest-deep)',
            transition: 'background 0.15s, color 0.15s',
          }}
        >
          {generating ? 'Generating…' : selected ? '⚡ Generate this plan →' : 'Generate this →'}
        </button>
      </div>
    </div>
  );
}
