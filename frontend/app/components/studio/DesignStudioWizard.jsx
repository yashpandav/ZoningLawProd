'use client';
import { useState, useEffect, useRef } from 'react';
import RoomBriefForm from '../RoomBriefForm';
import SuggestionCard from '../SuggestionCard';
import { T } from '../shared';

// ── Design Studio Wizard ──────────────────────────────────────────────────────

const TYPOLOGY_SVG = {
  vertical:   (
    <svg viewBox="0 0 50 60" width={36} height={44}>
      <rect x={5}  y={32} width={40} height={26} fill="rgba(26,74,53,0.12)" stroke="#1A4A35" strokeWidth={1.5} rx={2}/>
      <rect x={5}  y={2}  width={40} height={26} fill="rgba(184,90,42,0.10)" stroke="#B85A2A" strokeWidth={1.5} rx={2}/>
      <line x1={25} y1={28} x2={25} y2={32} stroke="#C0BAB0" strokeWidth={1.5} strokeDasharray="3,2"/>
    </svg>
  ),
  horizontal: (
    <svg viewBox="0 0 60 40" width={44} height={32}>
      <rect x={2}  y={5} width={26} height={30} fill="rgba(26,74,53,0.12)" stroke="#1A4A35" strokeWidth={1.5} rx={2}/>
      <rect x={32} y={5} width={26} height={30} fill="rgba(184,90,42,0.10)" stroke="#B85A2A" strokeWidth={1.5} rx={2}/>
    </svg>
  ),
  mixed: (
    <svg viewBox="0 0 60 50" width={44} height={38}>
      <rect x={2}  y={2}  width={56} height={22} fill="rgba(26,90,53,0.12)" stroke="#1A5A35" strokeWidth={1.5} rx={2}/>
      <rect x={2}  y={26} width={26} height={22} fill="rgba(26,74,53,0.12)" stroke="#1A4A35" strokeWidth={1.5} rx={2}/>
      <rect x={32} y={26} width={26} height={22} fill="rgba(184,90,42,0.10)" stroke="#B85A2A" strokeWidth={1.5} rx={2}/>
    </svg>
  ),
};

export default function DesignStudioWizard({
  parcel, zoneSymbol, constraints, params, apiBase,
  onClose, onGenerate,
}) {
  const c  = constraints || {};
  const [step, setStep]             = useState(1);
  const [selectedTyp, setSelectedTyp] = useState(null);  // typology id from AI card
  const [aiSuggestions,    setAiSuggestions]    = useState([]);   // SuggestionCard data
  const [aiSugLoading,     setAiSugLoading]     = useState(false);
  const [selectedAiCard,   setSelectedAiCard]   = useState(null); // typology_id of selected card
  const [generatingCard,   setGeneratingCard]   = useState(null); // typology_id being generated
  const [briefMode,       setBriefMode]       = useState('structured');
  const [briefFreeText,   setBriefFreeText]   = useState('');
  const [briefParsing,    setBriefParsing]    = useState(false);
  const [briefParseError, setBriefParseError] = useState(null);
  const [briefParsed,     setBriefParsed]     = useState(null);
  // Number of DWELLING UNITS the architect wants in this floor plan.
  // 1 = single-family house, 2 = duplex, 3 = triplex, etc.
  // This is separate from the Parameter Tweaker's envelope slider (params.units).
  const [briefUnitsCount,  setBriefUnitsCount]  = useState(1);
  const [briefBedrooms,    setBriefBedrooms]    = useState(2);
  const [briefLiving,      setBriefLiving]      = useState(1);
  const [briefBathrooms,   setBriefBathrooms]   = useState(1);
  const [briefCustomRooms, setBriefCustomRooms] = useState([]);
  const [briefStkPref,    setBriefStkPref]    = useState('vertical');
  const [briefFormNotes,  setBriefFormNotes]  = useState('');
  const [briefHasBasement, setBriefHasBasement] = useState(false);
  // Per-room-instance floor preferences (arrays, not scalars).
  // Each element = storey_preference for that individual room: -1 basement, 0 ground, 1 upper.
  // Default: 2 bedrooms → both upper; 1 living → ground; 1 bathroom → ground.
  const [briefFloorAssign, setBriefFloorAssign] = useState({ bedrooms: [1, 1], living: [0], kitchen: 0, bathrooms: [0] });
  const briefParseAbortCtrlRef = useRef(null);

  const base = apiBase || 'http://localhost:8000';

  // ── Brief helpers ────────────────────────────────────────────────────────────
  const _KNOWN_ROLES = new Set(['bedroom', 'living', 'bathroom', 'kitchen', 'stair', 'corridor', 'entry']);

  // Resize a per-room floor array when the count changes.
  // Fills new slots by repeating the last value; clamps to 0 if no basement.
  function _resizeFloorArray(arr, newCount, hasBasement) {
    const cur = Array.isArray(arr) ? arr : [arr];
    const last = cur[cur.length - 1] ?? 1;
    return Array(Math.max(0, newCount)).fill(null).map((_, i) => {
      const v = i < cur.length ? cur[i] : last;
      return hasBasement ? v : Math.max(0, v);
    });
  }

  // Convert a per-room floors array into grouped {role, count, storey_preference} specs.
  // Example: [1, 1, 0] → [{count:2, storey:1}, {count:1, storey:0}]
  function _floorsToSpecs(role, floorsArr, count) {
    const arr = Array.isArray(floorsArr) ? floorsArr.slice(0, count) : Array(count).fill(floorsArr);
    const byFloor = {};
    arr.forEach(f => { byFloor[f] = (byFloor[f] || 0) + 1; });
    return Object.entries(byFloor).map(([f, n]) => ({
      role, count: n, min_area_m2: 0, storey_preference: parseInt(f, 10),
    }));
  }

  function briefFormToString() {
    const floorLabel = v => v === -1 ? 'basement' : v === 0 ? 'ground floor' : 'upper floor';
    const fa = briefFloorAssign;
    const parts = [];

    // Describe each bedroom individually if they're on different floors
    if (briefBedrooms > 0) {
      const floors = Array.isArray(fa.bedrooms) ? fa.bedrooms.slice(0, briefBedrooms) : Array(briefBedrooms).fill(fa.bedrooms);
      const byFloor = {};
      floors.forEach(f => { byFloor[f] = (byFloor[f] || 0) + 1; });
      parts.push(Object.entries(byFloor).map(([f, n]) => `${n} bedroom${n !== 1 ? 's' : ''} on ${floorLabel(parseInt(f, 10))}`).join(' + '));
    }
    if (briefLiving > 0) {
      const floors = Array.isArray(fa.living) ? fa.living.slice(0, briefLiving) : [fa.living];
      parts.push(`${briefLiving} living room${briefLiving !== 1 ? 's' : ''} on ${floorLabel(floors[0])}`);
    }
    if (briefBathrooms > 0) {
      const floors = Array.isArray(fa.bathrooms) ? fa.bathrooms.slice(0, briefBathrooms) : Array(briefBathrooms).fill(fa.bathrooms);
      const byFloor = {};
      floors.forEach(f => { byFloor[f] = (byFloor[f] || 0) + 1; });
      parts.push(Object.entries(byFloor).map(([f, n]) => `${n} bathroom${n !== 1 ? 's' : ''} on ${floorLabel(parseInt(f, 10))}`).join(' + '));
    }
    parts.push(`kitchen on ${floorLabel(fa.kitchen)}`);
    briefCustomRooms.forEach(cr => {
      const loc = `on ${floorLabel(cr.storeyPref ?? 0)}`;
      parts.push(cr.instruction ? `${cr.label} (${cr.instruction}) ${loc}` : `${cr.label} ${loc}`);
    });
    const lines = [`Program: ${parts.join(', ')}`];
    if (briefHasBasement) lines.push('Includes basement.');
    if (briefStkPref !== 'vertical') lines.push(`Stacking: ${briefStkPref}`);
    if (briefFormNotes) lines.push(`Notes: ${briefFormNotes}`);
    return lines.join('. ');
  }

  function applyParsedBriefToState(modelUnits) {
    const u = modelUnits?.[0] || {};
    const rooms = u.rooms || [];
    const bedroomTotal   = rooms.filter(r => r.role === 'bedroom').reduce((s, r) => s + r.count, 0) || 2;
    const livingTotal    = rooms.filter(r => r.role === 'living').reduce((s, r) => s + r.count, 0) || 1;
    const bathroomTotal  = rooms.filter(r => r.role === 'bathroom').reduce((s, r) => s + r.count, 0) || 1;
    setBriefBedrooms(bedroomTotal);
    setBriefLiving(livingTotal);
    setBriefBathrooms(bathroomTotal);
    const extras = rooms.filter(r => !_KNOWN_ROLES.has(r.role));
    setBriefCustomRooms(extras.map((r, i) => ({
      id: `parsed-${i}`,
      role: r.role,
      label: r.role.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),
      instruction: '',
      showInstruction: false,
      storeyPref: r.storey_preference ?? 0,
    })));
    // Build per-instance floor arrays from parsed storey_preference values
    const buildFloorArr = (role, total) =>
      rooms.filter(r => r.role === role)
        .flatMap(r => Array(r.count).fill(r.storey_preference ?? 0))
        .concat(Array(Math.max(0, total)).fill(1))
        .slice(0, total);
    const fa = {
      bedrooms:  buildFloorArr('bedroom', bedroomTotal),
      living:    buildFloorArr('living', livingTotal),
      bathrooms: buildFloorArr('bathroom', bathroomTotal),
      kitchen:   rooms.find(r => r.role === 'kitchen')?.storey_preference ?? 0,
    };
    setBriefFloorAssign(fa);
    if (rooms.some(r => r.storey_preference === -1)) setBriefHasBasement(true);
  }

  function buildRoomBriefForGen() {
    const fa = briefFloorAssign;
    // Build the room program for ONE unit (the architect describes a single dwelling)
    const rooms = [
      ...(briefBedrooms  > 0 ? _floorsToSpecs('bedroom',  fa.bedrooms,  briefBedrooms)  : []),
      ...(briefLiving    > 0 ? _floorsToSpecs('living',   fa.living,    briefLiving)    : []),
      ...(briefBathrooms > 0 ? _floorsToSpecs('bathroom', fa.bathrooms, briefBathrooms) : []),
      { role: 'kitchen', count: 1, min_area_m2: 0, storey_preference: fa.kitchen },
      ...briefCustomRooms.map(cr => ({ role: cr.role, count: 1, min_area_m2: 0, storey_preference: cr.storeyPref ?? 0 })),
    ];
    const customNotes = briefCustomRooms
      .filter(cr => cr.instruction)
      .map(cr => `${cr.label}: ${cr.instruction}`)
      .join('; ');
    const fullNotes = [customNotes, briefFormNotes].filter(Boolean).join('. ');
    // Create one UnitBriefModel per dwelling unit. len(units) drives the unit count
    // in the backend — it overrides the Parameter Tweaker's envelope slider.
    const units = Array(Math.max(1, briefUnitsCount)).fill(null).map((_, i) => ({
      unit_id: i + 1, rooms,
    }));
    return { units, stack_preference: briefStkPref, notes: fullNotes };
  }

  function handleHasBasementChange(val) {
    setBriefHasBasement(val);
    if (!val) {
      setBriefFloorAssign(prev => {
        const fa = { ...prev };
        Object.keys(fa).forEach(k => {
          if (Array.isArray(fa[k])) fa[k] = fa[k].map(v => v === -1 ? 0 : v);
          else if (fa[k] === -1) fa[k] = 0;
        });
        return fa;
      });
      setBriefCustomRooms(prev => prev.map(r => r.storeyPref === -1 ? { ...r, storeyPref: 0 } : r));
    }
  }

  useEffect(() => {
    if (step !== 3) return;
    if (!parcel?.lot_polygon_wkt || !zoneSymbol) return;
    let cancelled = false;
    setAiSugLoading(true);
    setAiSuggestions([]);
    const ov = c.exception_overrides || {};
    const briefStr = briefMode === 'freetext'
      ? briefFreeText
      : briefFormToString();
    fetch(`${base}/api/design-studio/suggest`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        polygon_wkt:           parcel.lot_polygon_wkt,
        zone_symbol:           zoneSymbol,
        units_target:          params?.units ?? 2,
        ward:                  parcel?.ward ?? null,
        brief:                 briefStr || null,
        exception_constraints: Object.keys(ov).length ? ov : null,
      }),
    })
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then(data => { if (!cancelled) { setAiSuggestions(data.suggestions || []); setAiSugLoading(false); } })
      .catch(() => { if (!cancelled) setAiSugLoading(false); });
    return () => { cancelled = true; };
  }, [step]); // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-select the top AI suggestion when it first arrives
  useEffect(() => {
    if (aiSuggestions.length > 0 && !selectedAiCard) {
      setSelectedAiCard(aiSuggestions[0].typology_id);
      setSelectedTyp(aiSuggestions[0].typology_id);
    }
  }, [aiSuggestions]); // eslint-disable-line react-hooks/exhaustive-deps

  const overlayStyle = {
    position: 'fixed', inset: 0, zIndex: 9000,
    background: 'rgba(0,0,0,0.55)', backdropFilter: 'blur(10px)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
  };

  const panelStyle = {
    width: '100%', maxWidth: 680, maxHeight: '92vh',
    background: 'var(--color-bg-primary)', borderRadius: 14,
    border: '1px solid var(--color-border)',
    display: 'flex', flexDirection: 'column',
    boxShadow: '0 32px 80px rgba(0,0,0,0.18)',
    overflow: 'hidden',
  };

  const stepLabels = ['Site', 'Brief', 'Configuration', 'Generate'];

  function renderStepBar() {
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 0, padding: '0 24px' }}>
        {stepLabels.map((label, idx) => {
          const n = idx + 1;
          const active = n === step;
          const done   = n < step;
          return (
            <div key={n} style={{ display: 'flex', alignItems: 'center', flex: 1 }}>
              <div style={{
                display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4,
                flex: 1,
              }}>
                <div style={{
                  width: 28, height: 28, borderRadius: '50%',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  fontSize: 11, fontWeight: 700,
                  background: done ? T.success : active ? T.accent : T.surface,
                  color: done || active ? '#fff' : T.t3,
                  border: `1px solid ${done ? T.success : active ? T.accent : T.border}`,
                }}>
                  {done ? '✓' : n}
                </div>
                <span style={{ fontSize: 8.5, color: active ? T.t1 : T.t3, whiteSpace: 'nowrap' }}>
                  {label}{label === 'Configuration' && !aiSugLoading && aiSuggestions.length > 0 && (
                    <span style={{ fontSize: 8, color: T.success, marginLeft: 4 }}>
                      ({aiSuggestions.length})
                    </span>
                  )}
                </span>
              </div>
              {idx < stepLabels.length - 1 && (
                <div style={{ height: 1, flex: 0.5, background: done ? T.success : T.border, marginBottom: 14 }} />
              )}
            </div>
          );
        })}
      </div>
    );
  }

  // ── Step 1: Site confirmation ────────────────────────────────────────
  function renderStep1() {
    return (
      <div style={{ padding: '0 24px 8px' }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: T.t1, marginBottom: 16 }}>
          Confirm site details
        </div>
        {/* Address banner — full width, shown when available */}
        {(() => {
          const addr = parcel?.address || `${parcel?.lat?.toFixed(5)}, ${parcel?.lng?.toFixed(5)}`;
          return (
            <div style={{
              marginBottom: 10, padding: '9px 14px', borderRadius: 8,
              background: 'var(--color-copper-wash)', border: '1px solid var(--color-copper-border)',
              display: 'flex', alignItems: 'center', gap: 8,
            }}>
              <span style={{ fontSize: 13, color: 'var(--color-copper)' }}>📍</span>
              <span style={{
                fontSize: 12, fontWeight: 600, color: 'var(--color-text-primary)',
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }} title={addr}>{addr}</span>
            </div>
          );
        })()}

        <div style={{
          display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10,
        }}>
          {[
            ['Zone',      zoneSymbol || '—'],
            ['Frontage',  c.lot_frontage_m ? `${c.lot_frontage_m.toFixed(1)} m` : '—'],
            ['Lot area',  c.lot_area_m2    ? `${c.lot_area_m2.toFixed(0)} m²`  : '—'],
            ['Depth',     c.lot_depth_m    ? `${c.lot_depth_m.toFixed(1)} m`   : '—'],
            ['Max height',c.max_height_m   ? `${c.max_height_m} m`             : '—'],
            ['Exception', c.exception_number ? `#${c.exception_number}`          : 'None'],
          ].map(([label, val]) => (
            <div key={label} style={{
              padding: '8px 12px', borderRadius: 8,
              background: T.surface, border: `1px solid ${T.border}`,
              display: 'flex', justifyContent: 'space-between', alignItems: 'center',
            }}>
              <span style={{ fontSize: 10.5, color: T.t3 }}>{label}</span>
              <span style={{ fontSize: 11, fontWeight: 700, color: T.t1, fontFamily: 'var(--font-mono)' }}>{val}</span>
            </div>
          ))}
        </div>
        {!parcel?.lot_polygon_wkt && (
          <div style={{
            marginTop: 14, padding: '9px 14px', borderRadius: 8,
            background: 'var(--color-warn-bg)', border: '1px solid var(--color-warn-border)',
            fontSize: 11, color: 'var(--color-warn-text)',
          }}>
            ⚠ No lot polygon found. Click a parcel on the map first to enable generation.
          </div>
        )}
      </div>
    );
  }

  // ── Step 2: Room brief ───────────────────────────────────────────────────
  function renderBriefStep() {
    const parsedBannerText = briefParsed
      ? `Parsed with ${Math.round(briefParsed.confidence * 100)}% confidence` +
        (briefParsed.terms?.length ? ` (unrecognized: ${briefParsed.terms.join(', ')})` : '')
      : null;
    // Combined handlers: update count AND resize the per-room floor array in sync
    const handleBedroomsChange = (n) => {
      setBriefBedrooms(n);
      setBriefFloorAssign(prev => ({ ...prev, bedrooms: _resizeFloorArray(prev.bedrooms, n, briefHasBasement) }));
    };
    const handleLivingChange = (n) => {
      setBriefLiving(n);
      setBriefFloorAssign(prev => ({ ...prev, living: _resizeFloorArray(prev.living, n, briefHasBasement) }));
    };
    const handleBathroomsChange = (n) => {
      setBriefBathrooms(n);
      setBriefFloorAssign(prev => ({ ...prev, bathrooms: _resizeFloorArray(prev.bathrooms, n, briefHasBasement) }));
    };
    return (
      <RoomBriefForm
        unitsCount={briefUnitsCount}
        onUnitsCountChange={setBriefUnitsCount}
        bedrooms={briefBedrooms}
        onBedroomsChange={handleBedroomsChange}
        living={briefLiving}
        onLivingChange={handleLivingChange}
        bathrooms={briefBathrooms}
        onBathroomsChange={handleBathroomsChange}
        customRooms={briefCustomRooms}
        onCustomRoomsChange={setBriefCustomRooms}
        stackPref={briefStkPref}
        onStackPrefChange={setBriefStkPref}
        notes={briefFormNotes}
        onNotesChange={setBriefFormNotes}
        mode={briefMode}
        onModeChange={setBriefMode}
        freeText={briefFreeText}
        onFreeTextChange={setBriefFreeText}
        parsing={briefParsing}
        parseError={briefParseError}
        parsedBanner={parsedBannerText}
        hasBasement={briefHasBasement}
        onHasBasementChange={handleHasBasementChange}
        floorAssignment={briefFloorAssign}
        onFloorAssignmentChange={setBriefFloorAssign}
      />
    );
  }

  // ── Step 3: AI Configuration picker ────────────────────────────────────────
  function renderStep2() {
    const fa = briefFloorAssign;
    const floorTag = v => v === -1 ? 'B' : v === 1 ? 'U' : 'G';
    // Summarise per-room floor distribution compactly, e.g. "3 bed (G/U/U)"
    const floorSummary = (floors, count) => {
      const arr = Array.isArray(floors) ? floors.slice(0, count) : Array(count).fill(floors);
      return count === 1 ? `(${floorTag(arr[0])})` : `(${arr.map(floorTag).join('/')})`;
    };
    const briefParts = [];
    if ((briefUnitsCount ?? 1) > 1) briefParts.push(`${briefUnitsCount} units`);
    if (briefBedrooms > 0)  briefParts.push(`${briefBedrooms} bed ${floorSummary(fa.bedrooms, briefBedrooms)}`);
    if (briefLiving > 0)    briefParts.push(`${briefLiving} living ${floorSummary(fa.living, briefLiving)}`);
    if (briefBathrooms > 0) briefParts.push(`${briefBathrooms} bath ${floorSummary(fa.bathrooms, briefBathrooms)}`);
    briefParts.push(`kitchen (${floorTag(fa.kitchen)})`);
    if (briefCustomRooms.length > 0) briefParts.push(`+${briefCustomRooms.length} extra`);
    const briefSummary = briefParts.join(' · ');
    const zoneBase = zoneSymbol?.split('(')[0].trim() ?? '';
    const frontage  = c.lot_frontage_m;
    const depth     = c.lot_depth_m;

    return (
      <div style={{ padding: '0 24px 8px' }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: T.t1, marginBottom: 4 }}>
          AI-Suggested Configurations
        </div>

        {/* Brief + lot context summary */}
        <div style={{
          marginBottom: 16, padding: '8px 12px', borderRadius: 7,
          background: 'var(--color-forest-wash)', border: '1px solid var(--color-forest-border)',
          display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap',
        }}>
          <span style={{ fontSize: 9, color: T.t3, letterSpacing: '0.1em', textTransform: 'uppercase', flexShrink: 0 }}>Brief</span>
          <span style={{ fontSize: 11, color: T.accent, fontWeight: 500 }}>{briefSummary}</span>
          {(frontage || depth || zoneBase) && (
            <>
              <span style={{ color: T.t3, fontSize: 10 }}>·</span>
              <span style={{ fontSize: 10, color: T.t2 }}>
                {frontage ? `${frontage.toFixed(1)}m` : '?'} × {depth ? `${depth.toFixed(1)}m` : '?'}
                {zoneBase ? <> · <strong style={{ color: T.accent }}>{zoneBase}</strong></> : null}
              </span>
            </>
          )}
          {(params?.units ?? 1) > 1 && (
            <span style={{
              marginLeft: 'auto', fontSize: 9, fontWeight: 700, padding: '2px 7px', borderRadius: 20,
              background: 'var(--color-copper-wash)', border: '1px solid var(--color-copper-border)',
              color: T.violet, flexShrink: 0,
            }}>
              {params.units} units
            </span>
          )}
        </div>

        {/* Loading */}
        {aiSugLoading && (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10, padding: '24px 0' }}>
            <div style={{
              width: 20, height: 20, borderRadius: '50%',
              border: `2px solid ${T.violet}`, borderTopColor: 'transparent',
              animation: 'cp-spin 0.8s linear infinite',
            }} />
            <span style={{ fontSize: 11, color: T.t2, textAlign: 'center' }}>
              Analysing lot dimensions, zone rules, and your brief…
            </span>
            <span style={{ fontSize: 9.5, color: T.t3, textAlign: 'center' }}>
              Ranking configurations that fit this specific parcel
            </span>
          </div>
        )}

        {/* Error / empty state */}
        {!aiSugLoading && aiSuggestions.length === 0 && (
          <div style={{
            padding: '14px 16px', borderRadius: 8,
            background: 'var(--color-warn-bg)', border: '1px solid var(--color-warn-border)',
            fontSize: 11, color: 'var(--color-warn-text)', lineHeight: 1.5,
          }}>
            {!parcel?.lot_polygon_wkt
              ? '⚠ No lot polygon — click a parcel on the map first, then re-open Design Studio.'
              : '⚠ Could not load AI suggestions. Ensure the backend is running with ENABLE_PACKGEN=true.'}
          </div>
        )}

        {/* AI suggestion cards — full-width vertical list */}
        {!aiSugLoading && aiSuggestions.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {aiSuggestions.map((card, i) => (
              <SuggestionCard
                key={card.typology_id}
                card={card}
                rank={i + 1}
                selected={selectedAiCard === card.typology_id}
                onSelect={(sc) => { setSelectedAiCard(sc.typology_id); setSelectedTyp(sc.typology_id); }}
                onGenerate={(sc) => {
                  setGeneratingCard(sc.typology_id);
                  onGenerate({ typologyId: sc.typology_id, roomBrief: buildRoomBriefForGen() });
                  onClose();
                }}
                generating={generatingCard === card.typology_id}
              />
            ))}
            <div style={{ fontSize: 9, color: T.t3, textAlign: 'center', paddingTop: 4 }}>
              Top configuration is auto-selected. Click any card to change, or hit &quot;Generate Pack&quot;.
            </div>
          </div>
        )}
      </div>
    );
  }

  const canAdvance1 = !!parcel?.lot_polygon_wkt;
  const canAdvance3 = !!selectedTyp || (!aiSugLoading && aiSuggestions.length > 0);

  async function handleBriefNext() {
    if (briefMode === 'freetext' && briefFreeText.trim()) {
      if (briefParseAbortCtrlRef.current) briefParseAbortCtrlRef.current.abort();
      const ctrl = new AbortController();
      briefParseAbortCtrlRef.current = ctrl;
      setBriefParsing(true);
      setBriefParseError(null);
      try {
        const res = await fetch(`${base}/api/parse-brief`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: briefFreeText, units_target: params?.units ?? 1 }),
          signal: ctrl.signal,
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        applyParsedBriefToState(data.room_brief.units);
        setBriefStkPref(data.room_brief.stack_preference || 'vertical');
        setBriefParsed({ confidence: data.confidence, terms: data.unrecognized_terms });
        setStep(3);
      } catch (err) {
        if (err.name !== 'AbortError') {
          setBriefParseError('Could not parse brief — using structured defaults. You can still continue.');
          setStep(3);
        }
      } finally {
        setBriefParsing(false);
      }
    } else {
      setStep(3);
    }
  }

  function handleNext() {
    if (step === 1 && canAdvance1) setStep(2);
    else if (step === 2 && !briefParsing) { void handleBriefNext(); }
    else if (step === 3 && canAdvance3) handleGenerate();
  }

  function handleGenerate() {
    // Use explicitly selected typology, or fall back to the top AI suggestion
    const typologyId = selectedTyp && selectedTyp !== 'custom'
      ? selectedTyp
      : aiSuggestions.length > 0
      ? aiSuggestions[0].typology_id
      : null;
    onGenerate({ typologyId, roomBrief: buildRoomBriefForGen() });
    onClose();
  }

  const isLastStep = step === 3;

  return (
    <div style={overlayStyle} onClick={e => e.target === e.currentTarget && onClose()}>
      <div style={panelStyle}>
        {/* Header */}
        <div style={{
          flexShrink: 0, padding: '16px 24px 12px',
          borderBottom: `1px solid ${T.border}`,
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <div>
            <span style={{ fontSize: 15, fontWeight: 800, color: T.t1 }}>📐 Design Studio</span>
            <div style={{ fontSize: 10, color: T.t3, marginTop: 2 }}>
              Generate a preliminary floor plan pack — DXF · IFC · PDF · SVG
            </div>
          </div>
          <button onClick={onClose} style={{
            background: 'none', border: 'none', color: T.t3,
            cursor: 'pointer', fontSize: 18, lineHeight: 1, padding: '2px 6px',
          }}>✕</button>
        </div>

        {/* Step bar */}
        <div style={{ flexShrink: 0, padding: '14px 24px 10px', borderBottom: `1px solid ${T.border}` }}>
          {renderStepBar()}
        </div>

        {/* Body */}
        <div style={{
          flex: 1, overflowY: 'auto', paddingTop: 20, paddingBottom: 8,
          scrollbarWidth: 'thin', scrollbarColor: 'var(--color-border) transparent',
        }}>
          {step === 1 && renderStep1()}
          {step === 2 && renderBriefStep()}
          {step === 3 && renderStep2()}
        </div>

        {/* Footer */}
        <div style={{
          flexShrink: 0, padding: '12px 24px',
          borderTop: `1px solid ${T.border}`,
          display: 'flex', gap: 10, alignItems: 'center',
        }}>
          {step > 1 && (
            <button onClick={() => {
              if (briefParseAbortCtrlRef.current) {
                briefParseAbortCtrlRef.current.abort();
                setBriefParsing(false);
              }
              setStep(s => s - 1);
            }} style={{
              fontSize: 11, padding: '8px 16px', borderRadius: 8,
              cursor: 'pointer', color: T.t2,
              background: T.surface, border: `1px solid ${T.border}`,
            }}>← Back</button>
          )}
          <div style={{ flex: 1 }} />
          <button
            onClick={handleNext}
            disabled={(step === 1 && !canAdvance1) || (step === 2 && briefParsing) || (step === 3 && !canAdvance3)}
            style={{
              fontSize: 13, fontWeight: 700, padding: '9px 24px', borderRadius: 8,
              cursor: (step === 1 && !canAdvance1) || (step === 3 && !canAdvance3) ? 'not-allowed' : 'pointer',
              color: '#fff',
              opacity: (step === 1 && !canAdvance1) || (step === 3 && !canAdvance3) ? 0.45 : 1,
              background: isLastStep ? 'var(--color-forest-deep)' : 'var(--color-copper)',
              border: `1px solid ${isLastStep ? 'var(--color-forest)' : 'var(--color-copper-hover)'}`,
            }}
          >
            {briefParsing ? 'Parsing…' : isLastStep ? '⚡ Generate Pack' : 'Next →'}
          </button>
        </div>
      </div>
    </div>
  );
}
