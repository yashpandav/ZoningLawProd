'use client';
import { useState } from 'react';
import { T } from '../shared';

// ── Pack-gen progress panel ────────────────────────────────────────────────────
export const PACK_STEPS = [
  'Projecting lot polygon to MTM-10…',
  'Computing setback envelope…',
  'Applying angular plane…',
  'Selecting typologies…',
  'Fitting stamp…',
  'Checking OBC compliance…',
  'Building DXF…',
  'Generating SVG previews…',
  'Exporting IFC4 massing…',
  'Writing PDF report…',
  'Packaging ZIP…',
];

export const AI_PACK_STEPS = [
  'Projecting lot polygon to MTM-10…',
  'Computing setback envelope…',
  'Checking room brief feasibility…',
  'Generating AI floor plan (gpt-4.1)…',
  'Validating room placement…',
  'Checking OBC compliance…',
  'Building DXF…',
  'Generating SVG previews…',
  'Exporting IFC4 massing…',
  'Writing PDF report…',
  'Packaging ZIP…',
];

// (RoomBriefPanel removed — superseded by DesignStudioWizard Step 3)

function TutorialModal({ onDismiss }) {
  const steps = [
    { icon: '📦', title: 'Unzip the bundle', body: 'Extract the ZIP — you\'ll find a subfolder per option (option_a, option_b).' },
    { icon: '📐', title: 'Open DXF in AutoCAD', body: 'File → Open → plans.dxf. Check the paperspace tabs at the bottom: Site Plan (1:200), Ground Floor (1:50), Second Floor (1:50), Roof Plan.' },
    { icon: '🏗', title: 'Open IFC in Revit', body: 'File → Open → IFC → model_ifc4.ifc. If the layout looks wrong, retry with model_ifc2x3.ifc. Select IfcBuilding to see Pset_ZoningData_Toronto_569_2013.' },
    { icon: '📋', title: 'Read the compliance report', body: 'Open report.pdf — Section 7 is a compliance audit with §-level citations from By-law 569-2013.' },
  ];
  return (
    <div style={{
      position: 'absolute', inset: 0, zIndex: 100,
      background: 'rgba(248,246,243,0.97)', backdropFilter: 'blur(6px)',
      display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
      padding: '24px 32px',
    }}>
      <div style={{ maxWidth: 480, width: '100%' }}>
        <div style={{ fontSize: 15, fontWeight: 700, color: T.t1, marginBottom: 4, textAlign: 'center' }}>
          How to open these files
        </div>
        <div style={{ fontSize: 10, color: T.t3, marginBottom: 20, textAlign: 'center' }}>
          First-time guide — shown once
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {steps.map((s, i) => (
            <div key={i} style={{
              display: 'flex', gap: 14, padding: '12px 14px', borderRadius: 8,
              background: T.surface, border: `1px solid ${T.border}`,
            }}>
              <span style={{ fontSize: 20, flexShrink: 0 }}>{s.icon}</span>
              <div>
                <div style={{ fontSize: 11, fontWeight: 700, color: T.t1, marginBottom: 3 }}>
                  {i + 1}. {s.title}
                </div>
                <div style={{ fontSize: 10, color: T.t2, lineHeight: 1.5 }}>{s.body}</div>
              </div>
            </div>
          ))}
        </div>
        <div style={{ marginTop: 16, textAlign: 'center' }}>
          <div id="tutorial-video-slot" style={{ display: 'none' }} />
          <button
            onClick={onDismiss}
            style={{
              padding: '9px 28px', borderRadius: 8, fontSize: 12, fontWeight: 700,
              background: 'var(--color-forest-wash)', border: '1px solid var(--color-forest-border)',
              color: T.accent, cursor: 'pointer',
            }}
          >
            Got it — show me the download
          </button>
        </div>
        <div style={{ fontSize: 9, color: T.t3, marginTop: 12, textAlign: 'center', lineHeight: 1.6 }}>
          Preliminary concept only — not for permit submission.
          A licensed Ontario architect or BCIN designer must review before any Building application.
        </div>
      </div>
    </div>
  );
}

export default function PackGenPanel({ state, steps, svgA, svgB, downloadFn, onClose, isAiLayout }) {
  const { status, error, stepsLog } = state;
  const [showTutorial, setShowTutorial] = useState(() => {
    try { return !localStorage.getItem('packgen_tutorial_seen'); } catch { return false; }
  });

  function dismissTutorial() {
    try { localStorage.setItem('packgen_tutorial_seen', '1'); } catch { /* ignore */ }
    setShowTutorial(false);
  }

  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 8000,
      background: 'rgba(0,0,0,0.55)', backdropFilter: 'blur(8px)',
      display: 'flex', alignItems: 'stretch', justifyContent: 'flex-end',
    }}>
      <div style={{
        width: '100%', maxWidth: 760, background: 'var(--color-bg-primary)',
        borderLeft: `1px solid ${T.borderHi}`,
        display: 'flex', flexDirection: 'column',
        boxShadow: '-20px 0 80px rgba(0,0,0,0.15)',
        position: 'relative',
      }}>
        {status === 'done' && showTutorial && (
          <TutorialModal onDismiss={dismissTutorial} />
        )}
        {/* Header */}
        <div style={{
          flexShrink: 0, padding: '13px 20px',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          borderBottom: `1px solid ${T.border}`,
          background: status === 'done'
            ? 'var(--color-ok-bg)'
            : status === 'error'
            ? 'var(--color-violation-bg)'
            : 'var(--color-warn-bg)',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <div style={{
              width: 7, height: 7, borderRadius: '50%', flexShrink: 0,
              background: status === 'done' ? T.success : status === 'error' ? T.danger : T.warn,
              boxShadow: `0 0 8px ${status === 'done' ? T.success : status === 'error' ? T.danger : T.warn}`,
            }} />
            <span style={{ fontSize: 14, fontWeight: 700, color: T.t1 }}>
              {status === 'done' ? 'Floor Plan Pack Ready' : status === 'error' ? 'Generation Failed' : 'Generating Pack…'}
            </span>
            {isAiLayout && (
              <span style={{
                fontSize: 10, fontWeight: 700, padding: '2px 8px', borderRadius: 99,
                background: 'var(--color-copper-wash)', border: '1px solid var(--color-copper-border)',
                color: 'var(--color-copper)', letterSpacing: '0.05em',
              }}>✨ AI Layout</span>
            )}
          </div>
          <button onClick={onClose} style={{
            background: 'none', border: 'none', color: T.t3,
            cursor: 'pointer', fontSize: 18, lineHeight: 1, padding: '2px 6px',
          }}>✕</button>
        </div>

        <div style={{
          flex: 1, overflowY: 'auto', padding: '18px 24px',
          scrollbarWidth: 'thin', scrollbarColor: 'var(--color-border) transparent',
        }}>

          {/* Progress steps */}
          {status !== 'done' && (
            <div style={{ marginBottom: 20 }}>
              {steps.map((step, i) => {
                const done = i < stepsLog.length;
                const active = i === stepsLog.length && status === 'loading';
                return (
                  <div key={i} style={{
                    display: 'flex', alignItems: 'center', gap: 10,
                    marginBottom: 9, opacity: done ? 1 : active ? 1 : 0.35,
                  }}>
                    <div style={{
                      width: 18, height: 18, borderRadius: '50%', flexShrink: 0,
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      background: done ? 'var(--color-ok-bg)' : active ? 'var(--color-warn-bg)' : T.surface,
                      border: `1px solid ${done ? 'var(--color-ok-border)' : active ? 'var(--color-warn-border)' : T.border}`,
                      fontSize: 10,
                    }}>
                      {done ? '✓' : active ? '…' : String(i + 1)}
                    </div>
                    <span style={{ fontSize: 12, color: done ? T.success : active ? T.warn : T.t3 }}>
                      {step}
                    </span>
                  </div>
                );
              })}
            </div>
          )}

          {/* Error */}
          {status === 'error' && (
            <div style={{
              padding: '12px 16px', borderRadius: 8,
              background: 'var(--color-violation-bg)', border: '1px solid var(--color-violation-border)',
              color: T.danger, fontSize: 13, lineHeight: 1.5,
            }}>
              {error || 'An error occurred during pack generation.'}
            </div>
          )}

          {/* SVG previews */}
          {status === 'done' && (svgA || svgB) && (
            <>
              <div style={{ marginBottom: 16 }}>
                <div style={{ fontSize: 10, color: T.t3, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 10 }}>
                  Floor Plan Preview
                </div>
                {svgA && (
                  <div style={{ marginBottom: 12 }}>
                    <div style={{ fontSize: 10, fontWeight: 700, color: T.accent, marginBottom: 6 }}>Option A — Vertical Stack</div>
                    <div
                      style={{ borderRadius: 8, overflow: 'hidden', border: `1px solid ${T.border}`, background: 'var(--color-bg-surface)' }}
                      dangerouslySetInnerHTML={{ __html: svgA }}
                    />
                  </div>
                )}
                {svgB && (
                  <div>
                    <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--color-copper)', marginBottom: 6 }}>Option B — Side-by-Side</div>
                    <div
                      style={{ borderRadius: 8, overflow: 'hidden', border: `1px solid ${T.border}`, background: 'var(--color-bg-surface)' }}
                      dangerouslySetInnerHTML={{ __html: svgB }}
                    />
                  </div>
                )}
              </div>
            </>
          )}
        </div>

        {/* Footer */}
        {status === 'done' && (
          <div style={{
            flexShrink: 0, padding: '12px 20px',
            borderTop: `1px solid ${T.border}`,
            display: 'flex', gap: 10, alignItems: 'center',
          }}>
            <button
              onClick={downloadFn}
              style={{
                fontSize: 13, fontWeight: 700, padding: '9px 20px', borderRadius: 8,
                cursor: 'pointer', color: '#fff',
                background: 'var(--color-forest-deep)',
                border: '1px solid var(--color-forest)',
              }}
            >
              ↓ Download ZIP
            </button>
            <span style={{ fontSize: 10, color: T.t3, flex: 1 }}>
              DXF (AIA NCS layers, multi-layout) · IFC4 + IFC2x3 (Revit/ArchiCAD) · PDF compliance report · README · validation.json
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
