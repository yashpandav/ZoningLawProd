'use client';

// ---------------------------------------------------------------------------
// Advanced Parameter Tweaker — 10-category accordion
// ---------------------------------------------------------------------------

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

function StatusDot({ status }) {
  const color = STATUS_COLOR[status] || STATUS_COLOR.na;
  return (
    <span style={{
      display: 'inline-block', width: 7, height: 7,
      borderRadius: '50%', background: color, flexShrink: 0,
    }} title={STATUS_LABEL[status]} />
  );
}

function AdvParamRow({ param, validation, proposed, onProposedChange, onAskClaude, zoneSymbol }) {
  const v = validation || null;
  const status = v ? v.status : null;
  const isEditable = param.editable_basic || param.editable_advanced;
  const currentVal = proposed !== undefined ? proposed : param.value;

  const inputStyle = {
    background: 'var(--color-bg-muted)',
    border: `1px solid ${status ? STATUS_COLOR[status] : 'var(--color-border)'}`,
    borderRadius: 4, color: 'var(--color-text-primary)',
    fontSize: 10, padding: '2px 5px', width: 70, textAlign: 'right',
    fontFamily: 'var(--font-mono)',
  };

  const unit = param.unit === 'bool' ? '' : param.unit ? ` ${param.unit}` : '';

  return (
    <div style={{
      display: 'flex', alignItems: 'flex-start', gap: 6,
      padding: '5px 0', borderBottom: '1px solid var(--color-border-light)',
    }}>
      {status && <StatusDot status={status} />}
      {!status && <span style={{ width: 7 }} />}

      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
          <span style={{ fontSize: 10, color: 'var(--color-text-primary)', fontWeight: 600, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {param.label}
          </span>

          {isEditable && param.unit !== 'bool' && param.unit !== '' && param.min_val != null && param.max_val != null ? (
            <input
              type="number"
              value={currentVal ?? ''}
              min={param.min_val}
              max={param.max_val}
              step={param.unit === 'int' || param.unit === 'storeys' ? 1 : 0.1}
              style={inputStyle}
              onChange={e => {
                const n = parseFloat(e.target.value);
                if (!isNaN(n)) onProposedChange(param.key, n);
              }}
            />
          ) : param.unit === 'bool' ? (
            <input
              type="checkbox"
              checked={!!currentVal}
              style={{ accentColor: 'var(--color-forest-deep)', cursor: isEditable ? 'pointer' : 'default' }}
              disabled={!isEditable}
              onChange={e => isEditable && onProposedChange(param.key, e.target.checked)}
            />
          ) : (
            <span style={{ fontSize: 10, fontFamily: 'var(--font-mono)', color: 'var(--color-text-muted)' }}>
              {currentVal != null ? `${currentVal}${unit}` : '—'}
            </span>
          )}
        </div>

        {status && v.message && (
          <div style={{
            fontSize: 9, color: STATUS_COLOR[status], opacity: 0.85,
            marginTop: 2, lineHeight: 1.4,
          }}>
            {v.message}
          </div>
        )}

        <div style={{ fontSize: 9, color: 'var(--color-text-hint)', marginTop: 1 }}>
          {param.citation}
        </div>
      </div>

      {onAskClaude && status && status !== 'ok' && status !== 'na' && (
        <button
          onClick={() => onAskClaude(
            `Advanced parameter "${param.label}" shows status "${STATUS_LABEL[status]}" for zone ${zoneSymbol}.\n` +
            `Citation: ${param.citation}\n${v?.message || ''}\n` +
            `Please explain this regulation and what options exist to achieve compliance.`
          )}
          style={{
            fontSize: 8, padding: '2px 5px', borderRadius: 4, cursor: 'pointer',
            background: 'var(--color-forest-wash)', border: '1px solid var(--color-forest-border)',
            color: 'var(--color-forest-deep)', whiteSpace: 'nowrap', flexShrink: 0,
          }}
        >
          Ask AI
        </button>
      )}
    </div>
  );
}

export default function AdvancedAccordion({ schema, validation, proposed, openState, setOpen, onProposedChange, onAskClaude, zoneSymbol }) {
  const T_adv = {
    surface: 'var(--color-bg-wash)',
    border: 'var(--color-border)',
  };

  const catStatusSummary = (cat) => {
    const keys = cat.param_keys || [];
    let violations = 0, variances = 0;
    keys.forEach(k => {
      const s = validation[k]?.status;
      if (s === 'violation') violations++;
      else if (s === 'variance') variances++;
    });
    return { violations, variances };
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
      {(schema.categories || []).filter(cat => cat.id !== 'lot_context').map(cat => {
        const isOpen = !!openState[cat.id];
        const { violations, variances } = catStatusSummary(cat);
        const keys = cat.param_keys || [];
        const params = keys.map(k => schema.params[k]).filter(Boolean);

        return (
          <div key={cat.id} style={{
            border: `1px solid ${violations > 0 ? 'rgba(248,113,113,0.25)' : variances > 0 ? 'rgba(251,191,36,0.2)' : T_adv.border}`,
            borderRadius: 6, overflow: 'hidden',
          }}>
            <button
              onClick={() => setOpen(o => ({ ...o, [cat.id]: !isOpen }))}
              style={{
                width: '100%', display: 'flex', alignItems: 'center', gap: 6,
                background: T_adv.surface, border: 'none', cursor: 'pointer',
                padding: '6px 8px', textAlign: 'left',
              }}
            >
              <span style={{ fontSize: 10, fontWeight: 700, color: 'var(--color-text-primary)', flex: 1 }}>
                {cat.label}
              </span>
              {violations > 0 && (
                <span style={{ fontSize: 8, fontWeight: 700, color: 'var(--color-violation-text)', background: 'var(--color-violation-bg)', padding: '1px 5px', borderRadius: 3 }}>
                  {violations} violation{violations > 1 ? 's' : ''}
                </span>
              )}
              {variances > 0 && (
                <span style={{ fontSize: 8, fontWeight: 700, color: 'var(--color-warn-text)', background: 'var(--color-warn-bg)', padding: '1px 5px', borderRadius: 3 }}>
                  {variances} variance{variances > 1 ? 's' : ''}
                </span>
              )}
              <span style={{ fontSize: 8, color: 'var(--color-text-hint)', marginLeft: 2 }}>
                {params.length} param{params.length !== 1 ? 's' : ''}
              </span>
              <span style={{ fontSize: 8, color: 'var(--color-text-hint)' }}>{isOpen ? '▲' : '▼'}</span>
            </button>

            {isOpen && (
              <div style={{ padding: '4px 8px 8px', background: 'var(--color-bg-muted)' }}>
                {params.map(p => (
                  <AdvParamRow
                    key={p.key}
                    param={p}
                    validation={validation[p.key] || null}
                    proposed={proposed[p.key]}
                    onProposedChange={onProposedChange}
                    onAskClaude={onAskClaude}
                    zoneSymbol={zoneSymbol}
                  />
                ))}
              </div>
            )}
          </div>
        );
      })}

      {/* Lot Context (read-only, always collapsed by default) */}
      {(() => {
        const cat = (schema.categories || []).find(c => c.id === 'lot_context');
        if (!cat) return null;
        const isOpen = !!openState['lot_context'];
        const keys = cat.param_keys || [];
        const params = keys.map(k => schema.params[k]).filter(Boolean);
        return (
          <div style={{ border: `1px solid ${T_adv.border}`, borderRadius: 6, overflow: 'hidden' }}>
            <button
              onClick={() => setOpen(o => ({ ...o, lot_context: !isOpen }))}
              style={{
                width: '100%', display: 'flex', alignItems: 'center', gap: 6,
                background: T_adv.surface, border: 'none', cursor: 'pointer',
                padding: '6px 8px', textAlign: 'left',
              }}
            >
              <span style={{ fontSize: 10, fontWeight: 700, color: 'var(--color-text-muted)', flex: 1 }}>
                {cat.label} <span style={{ fontSize: 8, opacity: 0.5 }}>(GIS — read-only)</span>
              </span>
              <span style={{ fontSize: 8, color: 'var(--color-text-hint)' }}>{isOpen ? '▲' : '▼'}</span>
            </button>
            {isOpen && (
              <div style={{ padding: '4px 8px 8px', background: 'var(--color-bg-muted)' }}>
                {params.map(p => (
                  <AdvParamRow
                    key={p.key}
                    param={p}
                    validation={null}
                    proposed={undefined}
                    onProposedChange={() => {}}
                    onAskClaude={null}
                    zoneSymbol={zoneSymbol}
                  />
                ))}
              </div>
            )}
          </div>
        );
      })()}

      {/* Amendment flags */}
      {schema.amendment_flags && schema.amendment_flags.length > 0 && (
        <div style={{
          marginTop: 8, padding: '7px 9px',
          background: 'var(--color-copper-wash)', border: '1px solid var(--color-copper-border)',
          borderRadius: 6,
        }}>
          <div style={{ fontSize: 9, fontWeight: 700, color: 'var(--color-copper)', marginBottom: 4 }}>
            Active Amendments ({schema.amendment_flags.length})
          </div>
          {schema.amendment_flags.map((flag, i) => (
            <div key={i} style={{ fontSize: 9, color: 'var(--color-copper)', opacity: 0.75, lineHeight: 1.5 }}>• {flag}</div>
          ))}
        </div>
      )}

      {/* Warnings */}
      {schema.warnings && schema.warnings.length > 0 && (
        <div style={{
          marginTop: 4, padding: '6px 9px',
          background: 'var(--color-warn-bg)', border: '1px solid var(--color-warn-border)',
          borderRadius: 6,
        }}>
          {schema.warnings.map((w, i) => (
            <div key={i} style={{ fontSize: 9, color: 'var(--color-warn-text)' }}>⚠ {w}</div>
          ))}
        </div>
      )}
    </div>
  );
}
