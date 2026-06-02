'use client';
import { useState, useMemo, useCallback, useEffect } from 'react';
import { calculateEnvelope } from '../utils/calculateEnvelope';
import { buildCopySummary } from '../utils/copySummary';
import { makeParamPresets } from '../utils/paramPresets';
import { buildChips } from '../utils/buildChips';
import usePackGen from '../hooks/usePackGen';
import useAnalysis from '../hooks/useAnalysis';

import { T, Chip, ViolationsBanner, SummaryMetrics } from './shared';
import LotPlan, { useContainerHeight } from './envelope/LotPlan';
import EnvelopeCalculator from './envelope/EnvelopeCalculator';
import DesignStudioWizard from './studio/DesignStudioWizard';
import PackGenPanel, { PACK_STEPS, AI_PACK_STEPS } from './plan/PlanPreview';
import AnalysisReport, { buildAnalyzeMessage } from './report/AnalysisReport';

export default function ParameterTweaker({ constraints, zoneSymbol, onAskClaude, parcel, apiBase }) {
  const c  = constraints || {};
  const ov = c.exception_overrides || {};
  const ds = c.data_sources || {};

  const maxCov   = ov.max_coverage_pct   ?? c.max_coverage_pct;
  const maxHt    = ov.max_height_m       ?? c.max_height_m;
  const maxFsi   = ov.max_fsi            ?? c.max_fsi;
  const maxUnits = ov.max_units          ?? c.max_units;
  const frontMin = ov.front_yard_min_m   ?? c.front_yard_min_m;
  const rearMin  = ov.rear_yard_min_m    ?? c.rear_yard_min_m;
  const sideMin  = ov.side_yard_min_m    ?? c.side_yard_min_m;
  const parkMin  = ov.parking_min_spaces ?? c.parking_min_spaces ?? 1;
  const bikMin   = c.bicycle_parking_min ?? 1;
  const lotArea  = c.lot_area_m2;
  const maxBd    = ov.max_building_depth_m ?? c.max_building_depth_m;

  const baseZone  = (zoneSymbol || '').split(/[\s(]/)[0].toUpperCase();
  const isRes     = ['R','RD','RS','RT','RM'].includes(baseZone);
  const isCR      = ['CR','CL','CRE'].some(z => baseZone.startsWith(z));
  const isHolding    = !!c.holding_zone;
  const isUnderAppeal = !!c.zone_under_appeal;

  const maxFpSl  = lotArea ? Math.ceil(lotArea * 0.65) : 200;
  const maxGfaSl = lotArea && maxFsi ? Math.ceil(lotArea * maxFsi * 1.1) : 400;
  const maxHtSl  = maxHt ?? 30;
  const maxUnSl  = maxUnits ?? 20;
  const maxBdSl  = maxBd ? Math.ceil(maxBd * 1.25) : 25;

  const { initParams, safeInit, PRESETS } = makeParamPresets({
    lotArea, maxCov, maxFsi, maxHt, maxUnits, maxBd,
    maxFpSl, maxGfaSl, frontMin, rearMin, sideMin, parkMin, bikMin, isRes,
  });

  const [params,       setParams]      = useState(safeInit);
  const [excOpen,      setExcOpen]     = useState(false);
  const [copied,       setCopied]      = useState(false);
  const [activePreset, setActivePreset] = useState('typical');
  const [diagramRef,   diagramH]       = useContainerHeight();
  const [showWizard,   setShowWizard]  = useState(false);

  // Advanced Parameter Tweaker state
  const [advancedMode,  setAdvancedMode]  = useState(false);
  const [schemaData,    setSchemaData]    = useState(null);
  const [schemaLoading, setSchemaLoading] = useState(false);
  const [advValidation, setAdvValidation] = useState({});
  const [advProposed,   setAdvProposed]   = useState({});
  const [advOpen,       setAdvOpen]       = useState({});

  const set   = useCallback(k => v => { setParams(p => ({ ...p, [k]: v })); setActivePreset(''); }, []);
  const reset = () => {
    try { setParams(initParams()); } catch { setParams(safeInit()); }
    setActivePreset('typical');
  };

  const result = useMemo(
    () => calculateEnvelope(params, constraints, zoneSymbol),
    [params, constraints, zoneSymbol]
  );

  // Pack-gen via hook
  const {
    showPack, setShowPack,
    packState,
    packSvgA, packSvgB,
    roomBrief, setRoomBrief,
    runPackGen, downloadPack,
  } = usePackGen({ parcel, params, zoneSymbol, c, ov, apiBase });

  // Analysis report via hook
  const {
    showReport, setShowReport,
    reportContent, reportStreaming,
    runAnalysis,
  } = useAnalysis({ parcel, params, zoneSymbol, result, apiBase });

  // Schema fetch when advanced mode opens
  useEffect(() => {
    if (!advancedMode || schemaData || !zoneSymbol) return;
    setSchemaLoading(true);
    const qs = new URLSearchParams({ zone_symbol: zoneSymbol });
    if (c.lot_frontage_m) qs.set('lot_frontage_m', c.lot_frontage_m);
    if (c.lot_depth_m)    qs.set('lot_depth_m',    c.lot_depth_m);
    if (c.lot_area_m2)    qs.set('lot_area_m2',    c.lot_area_m2);
    const base = apiBase || '';
    fetch(`${base}/api/packgen/params/schema?${qs}`)
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(data => { setSchemaData(data); setSchemaLoading(false); })
      .catch(() => setSchemaLoading(false));
  }, [advancedMode, zoneSymbol, schemaData, c.lot_frontage_m, c.lot_depth_m, c.lot_area_m2, apiBase]);

  // Reset schema when zone changes
  useEffect(() => { setSchemaData(null); setAdvProposed({}); setAdvValidation({}); }, [zoneSymbol]);

  // Live validation (debounced)
  useEffect(() => {
    if (!advancedMode || !zoneSymbol || Object.keys(advProposed).length === 0) return;
    const timer = setTimeout(() => {
      const base = apiBase || '';
      fetch(`${base}/api/packgen/params/validate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          zone_symbol: zoneSymbol,
          proposed: advProposed,
          lot_data: {
            lot_frontage_m: c.lot_frontage_m,
            lot_depth_m: c.lot_depth_m,
            lot_area_m2: c.lot_area_m2,
          },
        }),
      })
        .then(r => r.ok ? r.json() : null)
        .then(data => {
          if (!data) return;
          const byKey = {};
          (data.results || []).forEach(r => { byKey[r.param_key] = r; });
          setAdvValidation(byKey);
        })
        .catch(() => {});
    }, 180);
    return () => clearTimeout(timer);
  }, [advProposed, advancedMode, zoneSymbol, c.lot_frontage_m, c.lot_depth_m, c.lot_area_m2, apiBase]);

  const liveCov    = result.coverage_pct;
  const liveFsi    = result.live_fsi;
  const excEntries = Object.entries(ov).filter(([, v]) => v != null && v !== '');
  const allOk      = result.overall_compliant;
  const violations = result.violations;

  const chips = buildChips({ params, result, c, ov, maxCov, maxFsi, maxUnits, maxBd, isRes, zoneSymbol, onAskClaude });

  function handleCopySummary() {
    const text = buildCopySummary({
      params, c, result, zoneSymbol,
      maxCov, maxFsi, maxHt, maxUnits, frontMin, rearMin, sideMin,
      isRes, isHolding, isUnderAppeal,
    });
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  function handleAnalyze() {
    if (parcel) { runAnalysis(); return; }
    if (onAskClaude) onAskClaude(buildAnalyzeMessage(params, constraints, result, zoneSymbol));
  }

  const disclaimer = isRes
    ? 'R-series setbacks are contextual (street average) — values shown are approximate defaults. Angular plane now uses lot depth from city database.'
    : isCR
    ? "CR FSI varies by designation. CR residential/commercial FSI split is approximate (60/40). Verify with the designation schedule."
    : 'Indicative only. Verify with the By-law and a registered planner before any permit application.';

  const btnBase = {
    fontSize: 10.5, borderRadius: 7, padding: '5px 11px',
    cursor: 'pointer', fontWeight: 500, border: `1px solid ${T.border}`,
    background: T.surface, color: T.t2, lineHeight: 1, whiteSpace: 'nowrap',
  };

  return (
    <>
    {showWizard && (
      <DesignStudioWizard
        parcel={parcel} zoneSymbol={zoneSymbol} constraints={c}
        params={params} apiBase={apiBase}
        onClose={() => setShowWizard(false)}
        onGenerate={({ typologyId, roomBrief: rb }) => {
          setRoomBrief(rb);
          runPackGen(typologyId, rb);
        }}
      />
    )}
    {showReport && (
      <AnalysisReport
        content={reportContent} streaming={reportStreaming} zoneSymbol={zoneSymbol}
        onClose={() => { setShowReport(false); }}
        onContinueChat={onAskClaude ? () => {
          setShowReport(false);
          onAskClaude('I\'ve reviewed the compliance analysis report for this parcel. Please help me understand the next steps and answer any follow-up questions.');
        } : null}
      />
    )}
    {showPack && (
      <PackGenPanel
        state={packState} steps={roomBrief ? AI_PACK_STEPS : PACK_STEPS}
        svgA={packSvgA} svgB={packSvgB}
        downloadFn={downloadPack} onClose={() => setShowPack(false)}
        isAiLayout={!!roomBrief}
      />
    )}
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>

      {/* ── Top bar ── */}
      <div style={{ flexShrink: 0, padding: '10px 16px', borderBottom: `1px solid ${T.border}`, display: 'flex', alignItems: 'center', gap: 10 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
          <div style={{ width: 8, height: 8, borderRadius: '50%', background: allOk ? T.success : T.danger, boxShadow: `0 0 6px ${allOk ? T.success : T.danger}`, flexShrink: 0 }} />
          <span style={{ fontSize: 12, fontWeight: 700, color: allOk ? T.success : T.danger }}>
            {allOk ? 'Compliant' : `${violations.length} violation${violations.length !== 1 ? 's' : ''}`}
          </span>
        </div>
        <div style={{ width: 1, height: 16, background: T.border, flexShrink: 0 }} />
        <div style={{ display: 'flex', gap: 6, alignItems: 'center', flex: 1, minWidth: 0, flexWrap: 'wrap' }}>
          {liveCov != null && (
            <span style={{ fontSize: 10, color: T.t2, background: T.surface, border: `1px solid ${T.border}`, borderRadius: 5, padding: '2px 7px', fontFamily: 'var(--font-mono)', whiteSpace: 'nowrap' }}>
              cov&nbsp;<strong style={{ color: maxCov && liveCov > maxCov ? T.danger : T.t1, fontWeight: 700 }}>{liveCov}%</strong>
              {maxCov != null && <span style={{ color: T.t3 }}>/{maxCov}%</span>}
            </span>
          )}
          {liveFsi != null && (
            <span style={{ fontSize: 10, color: T.t2, background: T.surface, border: `1px solid ${T.border}`, borderRadius: 5, padding: '2px 7px', fontFamily: 'var(--font-mono)', whiteSpace: 'nowrap' }}>
              FSI&nbsp;<strong style={{ color: maxFsi && liveFsi > maxFsi ? T.danger : T.t1, fontWeight: 700 }}>{liveFsi}</strong>
              {maxFsi != null && <span style={{ color: T.t3 }}>/{maxFsi}</span>}
            </span>
          )}
          {c.lot_depth_m && (
            <span style={{ fontSize: 10, color: T.t3, fontFamily: 'var(--font-mono)', background: T.surface, border: `1px solid ${T.border}`, borderRadius: 5, padding: '2px 7px', whiteSpace: 'nowrap' }}>
              {c.lot_frontage_m?.toFixed(1)}×{c.lot_depth_m?.toFixed(1)} m
            </span>
          )}
        </div>
        <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
          <button onClick={reset} title="Reset to 80% of limits" style={btnBase}>Reset</button>
          <button onClick={handleCopySummary} title="Copy formatted compliance summary to clipboard"
            style={{ ...btnBase, color: copied ? T.success : T.t2, borderColor: copied ? 'rgba(52,211,153,0.3)' : T.border }}>
            {copied ? '✓ Copied' : 'Copy'}
          </button>
          {(onAskClaude || parcel) && (
            <button onClick={handleAnalyze} title="Generate a full compliance analysis report — permit checklist, risk assessment, By-law citations"
              style={{ ...btnBase, color: '#fff', fontWeight: 700, background: 'var(--color-copper)', border: '1px solid var(--color-copper-hover)', padding: '5px 13px' }}>
              ✨ Full Analysis
            </button>
          )}
          {parcel?.lot_polygon_wkt && (
            <button onClick={() => setShowWizard(true)} title="Open Design Studio — generate a floor plan pack (DXF + SVG + IFC + PDF)"
              style={{ ...btnBase, color: '#fff', fontWeight: 700, background: 'var(--color-forest-deep)', border: '1px solid var(--color-forest)', padding: '5px 13px' }}>
              📐 Design Studio
            </button>
          )}
        </div>
      </div>

      {/* ── Holding zone / appeal banner ── */}
      {(isHolding || isUnderAppeal) && (
        <div style={{ flexShrink: 0, padding: '8px 16px', background: isHolding ? 'var(--color-violation-bg)' : 'var(--color-warn-bg)', borderBottom: `1px solid ${isHolding ? 'var(--color-violation-border)' : 'var(--color-warn-border)'}`, fontSize: 11, fontWeight: 600, color: isHolding ? 'var(--color-violation-text)' : 'var(--color-warn-text)', lineHeight: 1.45 }}>
          {isHolding ? '⛔ HOLDING ZONE — no building permit can be issued without a zoning bylaw amendment. This analysis is for planning purposes only.' : '⚠️ ZONE UNDER APPEAL — provisions may change. Verify with the City before permit application.'}
        </div>
      )}

      {/* ── Scenario presets ── */}
      <div style={{ flexShrink: 0, padding: '8px 16px', borderBottom: `1px solid ${T.border}`, display: 'flex', gap: 6, overflowX: 'auto', scrollbarWidth: 'none', alignItems: 'center' }}>
        <span style={{ fontSize: 9, color: T.t3, letterSpacing: '0.11em', textTransform: 'uppercase', flexShrink: 0, marginRight: 2 }}>Scenario</span>
        {PRESETS.map(p => (
          <button key={p.id} onClick={() => { setParams(p.fn()); setActivePreset(p.id); }} title={p.desc}
            style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '5px 12px', borderRadius: 7, cursor: 'pointer', flexShrink: 0, background: activePreset === p.id ? 'var(--color-forest-wash)' : T.surface, border: `1px solid ${activePreset === p.id ? 'var(--color-forest-border)' : T.border}`, transition: 'background 0.15s, border-color 0.15s' }}>
            <span style={{ fontSize: 10, fontWeight: 700, color: activePreset === p.id ? T.accent : T.t1, whiteSpace: 'nowrap' }}>{p.label}</span>
            <span style={{ fontSize: 8, color: T.t3, marginTop: 2, whiteSpace: 'nowrap' }}>{p.desc}</span>
          </button>
        ))}
      </div>

      <ViolationsBanner violations={violations} />
      <SummaryMetrics result={result} onAsk={onAskClaude} zoneSymbol={zoneSymbol} />

      {/* ── Compliance chips ── */}
      <div style={{ flexShrink: 0, padding: '7px 16px', borderBottom: `1px solid ${T.border}`, display: 'flex', gap: 5, overflowX: 'auto', scrollbarWidth: 'none', alignItems: 'center' }}>
        {chips.map(ch => (
          <Chip key={ch.key} label={ch.label}
            compliant={'compliant' in ch ? ch.compliant : (result[ch.key]?.compliant ?? true)}
            onAsk={ch._ask}
          />
        ))}
      </div>

      {/* ── Split pane ── */}
      <div style={{ flex: 1, minHeight: 0, display: 'flex', overflow: 'hidden' }}>
        <EnvelopeCalculator
          params={params} setParams={setParams} result={result}
          maxCov={maxCov} maxFsi={maxFsi} maxHt={maxHt} maxUnits={maxUnits} maxBd={maxBd}
          maxFpSl={maxFpSl} maxGfaSl={maxGfaSl} maxHtSl={maxHtSl} maxUnSl={maxUnSl} maxBdSl={maxBdSl}
          frontMin={frontMin} rearMin={rearMin} sideMin={sideMin} parkMin={parkMin} bikMin={bikMin}
          isRes={isRes} isCR={isCR} ds={ds} disclaimer={disclaimer}
          advancedMode={advancedMode} setAdvancedMode={setAdvancedMode}
          schemaData={schemaData} schemaLoading={schemaLoading}
          advValidation={advValidation} advProposed={advProposed}
          advOpen={advOpen} setAdvOpen={setAdvOpen}
          onAdvProposedChange={(key, val) => setAdvProposed(p => ({ ...p, [key]: val }))}
          set={set} excOpen={excOpen} setExcOpen={setExcOpen}
          excEntries={excEntries} c={c} onAskClaude={onAskClaude} zoneSymbol={zoneSymbol}
        />
        <div ref={diagramRef} style={{ flex: 1, minWidth: 0, overflow: 'hidden', padding: '12px 14px', display: 'flex', flexDirection: 'column' }}>
          {diagramH > 20 && (
            <LotPlan params={params} constraints={c} baseZone={baseZone} svgH={diagramH - 24} result={result} />
          )}
        </div>
      </div>
    </div>
    </>
  );
}
