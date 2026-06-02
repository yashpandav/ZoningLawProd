'use client';
import { T, Dot, Slider, ColHead, SectionDiv } from '../shared';
import AdvancedAccordion from './AdvancedAccordion';

export default function EnvelopeCalculator({
  params, setParams, result, maxCov, maxFsi, maxHt, maxUnits, maxBd,
  maxFpSl, maxGfaSl, maxHtSl, maxUnSl, maxBdSl, frontMin, rearMin,
  sideMin, parkMin, bikMin, isRes, isCR, ds, disclaimer,
  advancedMode, setAdvancedMode, schemaData, schemaLoading,
  advValidation, advProposed, advOpen, setAdvOpen, onAdvProposedChange,
  set, excOpen, setExcOpen, excEntries, c, onAskClaude, zoneSymbol,
}) {
  const liveCov = result.coverage_pct;
  const liveFsi = result.live_fsi;

  return (
    <div style={{
      width: 284, flexShrink: 0, overflowY: 'auto', overflowX: 'hidden',
      padding: '14px 16px 20px',
      borderRight: `1px solid ${T.border}`,
      scrollbarWidth: 'thin', scrollbarColor: 'var(--color-border) transparent',
    }}>

      <ColHead>Envelope</ColHead>
      <Slider
        label={maxCov == null ? 'Footprint (ctx)' : 'Footprint m²'}
        value={params.footprint_m2} min={0} max={maxFpSl} step={1}
        onChange={set('footprint_m2')} result={result.footprint}
        noLimit={maxCov == null} src={ds.max_coverage_pct}
        sub={liveCov ? `${liveCov}% coverage` : null}
      />
      <Slider
        label={maxFsi == null ? 'GFA (no FSI cap)' : 'GFA m²'}
        value={params.gfa_m2} min={0} max={maxGfaSl} step={1}
        onChange={set('gfa_m2')} result={result.gfa}
        noLimit={maxFsi == null} src={ds.max_fsi}
        sub={liveFsi ? `FSI ${liveFsi}` : null}
      />
      <Slider
        label="Height m"
        value={params.height_m} min={3} max={maxHtSl} step={0.5}
        onChange={set('height_m')}
        result={result.height}
        noLimit={!maxHt} src={ds.max_height_m}
      />
      {maxUnits != null && (
        <Slider label="Units" value={params.units} min={1} max={maxUnSl} step={1}
          onChange={set('units')} result={result.units} />
      )}
      {isRes && maxBd != null && (
        <Slider
          label="Bldg depth m"
          value={params.building_depth_m ?? Math.round(maxBd * 0.8)}
          min={3} max={maxBdSl} step={0.5}
          onChange={set('building_depth_m')} result={result.building_depth}
          src={ds.max_building_depth_m}
        />
      )}

      <SectionDiv />

      <ColHead>Setbacks</ColHead>
      <Slider
        label={frontMin == null ? 'Front yard (ctx)' : 'Front yard m'}
        value={params.front_yard_m} min={0} max={10} step={0.1}
        onChange={set('front_yard_m')} result={result.front_yard}
        noLimit={frontMin == null} src={ds.front_yard_min_m}
        sub={
          isRes && result.angular_plane.applies && !result.angular_plane.compliant
            ? '⚠ 45° plane exceeded'
            : null
        }
      />
      <Slider label="Rear yard m" value={params.rear_yard_m} min={0} max={15} step={0.1}
        onChange={set('rear_yard_m')} result={result.rear_yard} src={ds.rear_yard_min_m} />
      <Slider label="Side yard m" value={params.side_yard_m} min={0} max={8} step={0.1}
        onChange={set('side_yard_m')} result={result.side_yard} src={ds.side_yard_min_m} />

      <SectionDiv />

      <ColHead>Parking</ColHead>
      <Slider label="Parking spaces" value={params.parking_spaces} min={0} max={10} step={1}
        onChange={set('parking_spaces')} result={result.parking} src={ds.parking_min_spaces} />
      <Slider label="Bicycle spaces" value={params.bicycle_spaces} min={0} max={20} step={1}
        onChange={set('bicycle_spaces')} result={result.bicycle} />

      {/* Advanced Parameter Tweaker toggle */}
      <SectionDiv />
      <button
        onClick={() => setAdvancedMode(m => !m)}
        style={{
          width: '100%', display: 'flex', justifyContent: 'space-between',
          alignItems: 'center', background: advancedMode ? 'var(--color-forest-wash)' : T.surface,
          border: `1px solid ${advancedMode ? 'var(--color-forest-border)' : T.border}`,
          borderRadius: 7, padding: '7px 10px', cursor: 'pointer',
          fontSize: 10, fontWeight: 700, color: advancedMode ? 'var(--color-forest-deep)' : T.t2,
          transition: 'all 0.15s',
        }}
      >
        <span>Advanced ({schemaData ? schemaData.param_count : '80+'} parameters)</span>
        <span style={{ fontSize: 8, opacity: 0.7 }}>{advancedMode ? '▲ Hide' : '▼ Show'}</span>
      </button>

      {advancedMode && (
        <div style={{ marginTop: 8 }}>
          {schemaLoading && (
            <div style={{ fontSize: 10, color: T.t3, textAlign: 'center', padding: '12px 0' }}>
              Loading by-law parameters…
            </div>
          )}
          {schemaData && !schemaLoading && (
            <AdvancedAccordion
              schema={schemaData}
              validation={advValidation}
              proposed={advProposed}
              openState={advOpen}
              setOpen={setAdvOpen}
              onProposedChange={onAdvProposedChange}
              onAskClaude={onAskClaude}
              zoneSymbol={zoneSymbol}
            />
          )}
          {!schemaData && !schemaLoading && (
            <div style={{ fontSize: 10, color: 'var(--color-violation-text)', padding: '8px 0' }}>
              Could not load advanced parameters. Is the backend running with ENABLE_PACKGEN=true?
            </div>
          )}
        </div>
      )}

      {/* Exception overrides */}
      {excEntries.length > 0 && (
        <>
          <SectionDiv />
          <button
            onClick={() => setExcOpen(o => !o)}
            style={{
              width: '100%', display: 'flex', justifyContent: 'space-between',
              alignItems: 'center', background: T.violetDim,
              border: `1px solid rgba(167,139,250,0.18)`, borderRadius: 7,
              padding: '6px 10px', cursor: 'pointer', fontSize: 10, fontWeight: 700, color: T.violet,
            }}
          >
            <span>Exception #{c.exception_number} overrides</span>
            <span style={{ fontSize: 8, color: T.t3, marginLeft: 6 }}>{excOpen ? '▲' : '▼'}</span>
          </button>
          {excOpen && (
            <div style={{ marginTop: 4, padding: '7px 9px', background: T.surface, borderRadius: 6 }}>
              {excEntries.map(([k, v]) => (
                <div key={k} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 5 }}>
                  <span style={{ fontSize: 9.5, color: T.violet, opacity: 0.7 }}>{k.replace(/_/g, ' ')}</span>
                  <span style={{ fontSize: 10, fontFamily: 'var(--font-mono)', color: T.t1, fontWeight: 600 }}>{String(v)}</span>
                </div>
              ))}
            </div>
          )}
        </>
      )}

      {/* Data source legend */}
      <div style={{ marginTop: 18, display: 'flex', flexWrap: 'wrap', gap: '5px 12px' }}>
        {[['overlay','Overlay map'],['postGIS','City DB'],['derived','Derived'],['default','Zone default']].map(([s, l]) => (
          <span key={s} style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 9, color: T.t3 }}>
            <Dot source={s} />{l}
          </span>
        ))}
      </div>

      {/* Disclaimer */}
      <div style={{
        marginTop: 14, fontSize: 9.5, color: T.t3, lineHeight: 1.55,
        paddingTop: 10, borderTop: `1px solid ${T.border}`,
      }}>
        {disclaimer}
      </div>
    </div>
  );
}
