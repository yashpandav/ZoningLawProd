'use client';
import { useRef, useEffect, useState } from 'react';
import { T } from '../shared';

// ── Lot plan SVG ───────────────────────────────────────────────────────────────
function LotPlan({ params, constraints, baseZone, svgH, result }) {
  const H  = svgH > 40 ? svgH : 186;
  const W  = 560;
  const ML = 30, MR = 48, MT = 20, MB = 22;

  const c  = constraints || {};
  const ov = c.exception_overrides || {};

  const frontage = c.lot_frontage_m || 10;
  const depth    = c.lot_area_m2 ? c.lot_area_m2 / frontage : 18;

  const iW = W - ML - MR;
  const iH = H - MT - MB;

  const scale = Math.min(iW / depth, iH / frontage);

  const lotW = depth    * scale;
  const lotH = frontage * scale;
  const lotX = ML;
  const lotY = MT + (iH - lotH) / 2;

  const sFr = Math.min((params.front_yard_m || 0) * scale, lotW * 0.33);
  const sRr = Math.min((params.rear_yard_m  || 0) * scale, lotW * 0.33);
  const sSd = Math.min((params.side_yard_m  || 0) * scale, lotH * 0.30);

  const bzX = lotX + sRr;
  const bzY = lotY + sSd;
  const bzW = Math.max(3, lotW - sFr - sRr);
  const bzH = Math.max(3, lotH - 2 * sSd);

  const bzArea = (bzW / scale) * (bzH / scale);
  const fpArea = Math.max(0, Math.min(params.footprint_m2 || 0, bzArea * 1.05));
  let fpW = 0, fpH = 0, fpX = bzX, fpY = bzY;
  if (bzArea > 0 && fpArea > 0) {
    const ratio = Math.sqrt(fpArea / bzArea);
    fpW = Math.min(bzW * ratio, bzW);
    fpH = Math.min(bzH * ratio, bzH);
    fpX = bzX + (bzW - fpW) / 2;
    fpY = bzY + (bzH - fpH) / 2;
  }

  const maxCov = ov.max_coverage_pct ?? c.max_coverage_pct;
  const fpOver = c.lot_area_m2 != null && maxCov != null && params.footprint_m2 > c.lot_area_m2 * maxCov / 100;
  const isRes  = ['R','RD','RS','RT','RM'].includes(baseZone);

  const showAP = isRes && sFr > 5;
  const apX    = lotX + lotW - sFr;
  const apExt  = Math.min(bzW * 0.6, bzH * 0.7);
  const apOk   = result?.angular_plane?.compliant ?? true;

  const maxBd  = ov.max_building_depth_m ?? c.max_building_depth_m;
  const bdLimX = maxBd && fpW > 4 ? fpX + fpW - Math.min(maxBd * scale, fpW) : null;
  const bdOver = maxBd && (params.building_depth_m || 0) > maxBd;

  const gsOk    = result?.garden_suite?.feasible;
  const gsDepth = isRes && gsOk ? Math.min(7.5 * scale * 0.7, sRr * 0.7) : 0;

  const rotLabel = (txt, x, y, size) => (
    <text x={x} y={y} textAnchor="middle" fontSize={size || 7}
      fill="rgba(26,74,53,0.20)" fontWeight="700" letterSpacing="0.7"
      transform={`rotate(-90, ${x}, ${y})`}>{txt}</text>
  );
  const hzLabel = (txt, x, y, size) => (
    <text x={x} y={y} textAnchor="middle" fontSize={size || 7}
      fill="rgba(26,74,53,0.20)" fontWeight="700" letterSpacing="0.7">{txt}</text>
  );

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H}
      style={{ display: 'block', borderRadius: 8, background: 'var(--color-bg-surface)' }}>
      <defs>
        <pattern id="dotg" width="14" height="14" patternUnits="userSpaceOnUse">
          <circle cx="7" cy="7" r="0.65" fill="rgba(0,0,0,0.06)" />
        </pattern>
        <pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="6" stroke="rgba(26,90,53,0.25)" strokeWidth="1.5"/>
        </pattern>
      </defs>
      <rect width={W} height={H} fill="url(#dotg)" />

      {/* Street band */}
      <rect x={W - MR} y={0} width={MR} height={H} fill="rgba(0,0,0,0.03)" />
      <text x={W - MR / 2} y={H / 2} textAnchor="middle" fontSize={7.5}
        fill="#C0BAB0" letterSpacing="2.5" fontWeight="700"
        transform={`rotate(90, ${W - MR / 2}, ${H / 2})`}>STREET</text>

      {/* Lot boundary */}
      <rect x={lotX} y={lotY} width={lotW} height={lotH}
        fill="rgba(0,0,0,0.01)" stroke="#C0BAB0" strokeWidth={1.5} />

      {/* Rear yard */}
      {sRr > 0.5 && <rect x={lotX} y={lotY} width={sRr} height={lotH} fill="rgba(26,74,53,0.07)" />}
      {sRr > 26 && rotLabel('REAR YARD', lotX + sRr / 2, lotY + lotH / 2)}
      {sRr > 16 && (
        <text x={lotX + sRr / 2} y={lotY + lotH * 0.72}
          textAnchor="middle" fontSize={9} fill="rgba(26,74,53,0.60)" fontWeight="600"
          transform={`rotate(-90, ${lotX + sRr / 2}, ${lotY + lotH * 0.72})`}>
          {(params.rear_yard_m || 0).toFixed(1)} m
        </text>
      )}

      {/* Garden suite hint */}
      {gsDepth > 8 && sRr > gsDepth && (
        <>
          <rect x={lotX} y={lotY + lotH / 2 - gsDepth * 0.8} width={gsDepth} height={gsDepth * 1.6}
            fill="url(#hatch)" stroke="rgba(26,90,53,0.35)" strokeWidth={0.8} strokeDasharray="3 2" rx={2} />
          {gsDepth > 20 && (
            <text x={lotX + gsDepth / 2} y={lotY + lotH / 2 + 3.5}
              textAnchor="middle" fontSize={6.5} fill="rgba(26,90,53,0.55)" fontWeight="700"
              transform={`rotate(-90, ${lotX + gsDepth / 2}, ${lotY + lotH / 2 + 3.5})`}>SUITE</text>
          )}
        </>
      )}

      {/* Front yard */}
      {sFr > 0.5 && <rect x={lotX + lotW - sFr} y={lotY} width={sFr} height={lotH} fill="rgba(26,74,53,0.07)" />}
      {sFr > 26 && rotLabel('FRONT YARD', lotX + lotW - sFr / 2, lotY + lotH / 2)}
      {sFr > 16 && (
        <text x={lotX + lotW - sFr / 2} y={lotY + lotH * 0.72}
          textAnchor="middle" fontSize={9} fill="rgba(26,74,53,0.60)" fontWeight="600"
          transform={`rotate(-90, ${lotX + lotW - sFr / 2}, ${lotY + lotH * 0.72})`}>
          {(params.front_yard_m || 0).toFixed(1)} m
        </text>
      )}

      {/* Side yards */}
      {sSd > 0.5 && (
        <>
          <rect x={lotX} y={lotY} width={lotW} height={sSd} fill="rgba(26,74,53,0.07)" />
          <rect x={lotX} y={lotY + lotH - sSd} width={lotW} height={sSd} fill="rgba(26,74,53,0.07)" />
        </>
      )}
      {sSd > 12 && (
        <>
          {hzLabel('SIDE', bzX + bzW / 2, lotY + sSd / 2 + 3, 6.5)}
          {hzLabel('SIDE', bzX + bzW / 2, lotY + lotH - sSd / 2 + 3, 6.5)}
        </>
      )}

      {/* Buildable zone */}
      <rect x={bzX} y={bzY} width={bzW} height={bzH}
        fill={T.accentDim} stroke={T.accent} strokeWidth={1} strokeDasharray="5 3" />
      {fpArea === 0 && bzW > 46 && bzH > 11 && hzLabel('BUILDABLE ZONE', bzX + bzW / 2, bzY + bzH / 2 + 3, 7.5)}

      {/* Angular plane lines */}
      {showAP && (
        <>
          <line x1={apX} y1={bzY} x2={apX - apExt} y2={bzY + apExt}
            stroke={apOk ? T.violet : T.danger} strokeWidth={1.2} strokeDasharray="4 3" opacity={0.5} />
          <line x1={apX} y1={bzY + bzH} x2={apX - apExt} y2={bzY + bzH - apExt}
            stroke={apOk ? T.violet : T.danger} strokeWidth={1.2} strokeDasharray="4 3" opacity={0.5} />
          <text x={apX - apExt * 0.35} y={bzY + bzH / 2 + 3}
            textAnchor="middle" fontSize={7.5} fill={apOk ? T.violet : T.danger} opacity={0.5}>45°</text>
        </>
      )}

      {/* Footprint */}
      {fpArea > 0 && (
        <rect x={fpX} y={fpY} width={fpW} height={fpH} rx={3}
          fill={fpOver ? 'rgba(138,42,32,0.12)' : 'rgba(26,74,53,0.18)'}
          stroke={fpOver ? T.danger : T.accent} strokeWidth={1.5} />
      )}

      {/* Floor count label */}
      {fpW > 32 && fpH > 18 && result && (
        <>
          <text x={fpX + fpW / 2} y={fpY + fpH / 2 - 4}
            textAnchor="middle" fontSize={Math.min(13, fpH * 0.36)} fontWeight="700"
            fill={fpOver ? '#8A2A20' : '#1A4A35'}>
            {(params.footprint_m2 || 0).toFixed(0)} m²
          </text>
          {result.floor_count > 1 && fpH > 28 && (
            <text x={fpX + fpW / 2} y={fpY + fpH / 2 + 10}
              textAnchor="middle" fontSize={9} fill={fpOver ? '#8A2A20' : 'rgba(26,74,53,0.55)'}>
              {result.floor_count} floors
            </text>
          )}
        </>
      )}

      {/* Building depth limit line */}
      {bdLimX && (
        <line x1={bdLimX} y1={fpY - 3} x2={bdLimX} y2={fpY + fpH + 3}
          stroke={bdOver ? T.danger : T.warn} strokeWidth={1.5} strokeDasharray="3 2" opacity={0.75} />
      )}

      {/* Dimension labels */}
      <text x={lotX + lotW / 2} y={lotY - 6} textAnchor="middle"
        fontSize={9} fill="#7A7068" fontWeight="500">{depth.toFixed(1)} m depth</text>
      <text x={lotX - 8} y={lotY + lotH / 2} textAnchor="middle"
        fontSize={9} fill="#7A7068" fontWeight="500"
        transform={`rotate(-90, ${lotX - 8}, ${lotY + lotH / 2})`}>{frontage.toFixed(1)} m</text>

      {/* Compass */}
      <circle cx={ML - 14} cy={MT + 9} r={7} fill="none" stroke="#D8D2C8" strokeWidth={1} />
      <line x1={ML - 14} y1={MT + 3} x2={ML - 14} y2={MT + 9} stroke="#C0BAB0" strokeWidth={1.5} />
      <text x={ML - 14} y={MT + 1} textAnchor="middle" fontSize={6.5} fill="#7A7068" fontWeight="700">N</text>

      {/* Legend */}
      <g transform={`translate(${ML}, ${H - 11})`}>
        <rect width={6} height={6} rx={1.5} fill="rgba(79,142,247,0.28)" stroke={T.accent} strokeWidth={0.8} />
        <text x={9} y={5} fontSize={7.5} fill="#7A7068">footprint</text>
        <rect x={56} width={6} height={6} rx={1.5} fill="none" stroke={T.accent} strokeWidth={0.8} strokeDasharray="3 2" />
        <text x={65} y={5} fontSize={7.5} fill="#7A7068">buildable zone</text>
        {isRes && (
          <>
            <line x1={134} y1={3} x2={146} y2={3} stroke={T.violet} strokeWidth={1.2} strokeDasharray="3 2" opacity={0.6} />
            <text x={149} y={5} fontSize={7.5} fill="#7A7068">45° plane</text>
          </>
        )}
        {gsDepth > 8 && sRr > gsDepth && (
          <>
            <rect x={186} width={6} height={6} rx={1.5} fill="url(#hatch)" stroke="rgba(26,90,53,0.4)" strokeWidth={0.8} />
            <text x={195} y={5} fontSize={7.5} fill="#7A7068">garden suite</text>
          </>
        )}
      </g>
    </svg>
  );
}

// ── ResizeObserver hook ────────────────────────────────────────────────────────
export function useContainerHeight() {
  const ref = useRef(null);
  const [h, setH] = useState(0);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(entries => {
      const height = entries[0]?.contentRect.height;
      if (height > 0) setH(height);
    });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, h];
}

export default LotPlan;
