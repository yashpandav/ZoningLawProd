/**
 * calculateEnvelope(params, constraints, zoneSymbol) → compliance + metrics object
 *
 * params: current slider values
 *   { footprint_m2, gfa_m2, height_m, units, front_yard_m, rear_yard_m,
 *     side_yard_m, parking_spaces, bicycle_spaces, building_depth_m,
 *     floors (optional — explicit override for floor count) }
 *
 * constraints: the constraints object from /api/parcel
 * zoneSymbol:  e.g. "RD (u1)" — used for zone-specific checks
 *
 * Returns:
 *   {
 *     footprint, gfa, height, units,
 *     front_yard, rear_yard, side_yard, building_depth,
 *     parking, bicycle,
 *     angular_plane,       45° angular plane compliance (residential, uses lot_depth)
 *     floor_count,         floors (locked by params.floors, else derived: ⌊height/3.0⌋)
 *     floor_height_m,      height ÷ floor_count (OBC §9.10.2.1 requires ≥ 2.4m)
 *     gfa_per_floor,       GFA ÷ floors
 *     remaining_lot_m2,    lot_area − footprint
 *     coverage_pct,        live coverage %
 *     live_fsi,            live FSI
 *     garden_suite,        By-law 569-2013 §150.7 garden suite feasibility
 *     overall_compliant:   boolean,
 *     violations:          string[]
 *   }
 */
export function calculateEnvelope(params, constraints, zoneSymbol = '') {
  const c = constraints || {};
  const {
    footprint_m2, gfa_m2, height_m, units,
    front_yard_m, rear_yard_m, side_yard_m,
    parking_spaces, bicycle_spaces,
    building_depth_m,
  } = params;

  const ov = c.exception_overrides || {};

  // Effective limits: exception_overrides win over base constraints
  const maxCovPct        = ov.max_coverage_pct    ?? c.max_coverage_pct;
  const maxHeightM       = ov.max_height_m        ?? c.max_height_m;
  const maxFsi           = ov.max_fsi             ?? c.max_fsi;
  const maxUnits         = ov.max_units           ?? c.max_units;
  const frontYardMin     = ov.front_yard_min_m    ?? c.front_yard_min_m;
  const rearYardMin      = ov.rear_yard_min_m     ?? c.rear_yard_min_m;
  const sideYardMin      = ov.side_yard_min_m     ?? c.side_yard_min_m;
  const parkingMin       = ov.parking_min_spaces  ?? c.parking_min_spaces;
  const bicycleMin       = c.bicycle_parking_min;
  const lotArea          = c.lot_area_m2;
  const lotFrontage      = c.lot_frontage_m;
  const lotDepth         = c.lot_depth_m;
  const maxBuildingDepth = ov.max_building_depth_m ?? c.max_building_depth_m;

  const baseZone = (zoneSymbol || '').split(/[\s(]/)[0].toUpperCase();
  const isRes    = ['R', 'RD', 'RS', 'RT', 'RM'].includes(baseZone);
  const isCRZone = baseZone.startsWith('CR') || baseZone === 'CL' || baseZone === 'CRE';

  const violations = [];

  // ── Footprint ──────────────────────────────────────────────────────────────
  const maxFootprint     = lotArea != null && maxCovPct != null ? lotArea * maxCovPct / 100 : null;
  const footprintPct     = maxFootprint != null ? clamp(footprint_m2 / maxFootprint * 100, 0, 100) : null;
  const footprintCompliant = maxFootprint == null || footprint_m2 <= maxFootprint;
  if (!footprintCompliant)
    violations.push(
      `Footprint ${footprint_m2}m² exceeds max ${maxFootprint.toFixed(1)}m² ` +
      `(${maxCovPct}% of ${lotArea}m²) — reduce by ${r1(footprint_m2 - maxFootprint)}m²`
    );

  // ── GFA (overall FSI cap) ─────────────────────────────────────────────────
  const maxGfa      = lotArea != null && maxFsi != null ? lotArea * maxFsi : null;
  const gfaPct      = maxGfa != null ? clamp(gfa_m2 / maxGfa * 100, 0, 100) : null;
  const gfaCompliant = maxGfa == null || gfa_m2 <= maxGfa;
  if (!gfaCompliant)
    violations.push(
      `GFA ${gfa_m2}m² exceeds max ${maxGfa.toFixed(1)}m² (FSI ${maxFsi}) ` +
      `— reduce by ${r1(gfa_m2 - maxGfa)}m²`
    );

  // ── CR zone: separate residential + commercial FSI caps ───────────────────
  // Approximate the GFA split as 60% residential / 40% commercial when not
  // specified. Violations are advisory — the actual split depends on the CR schedule.
  const maxFsiRes  = c.fsi_residential;
  const maxFsiComm = c.fsi_commercial;
  const fsiResCompliant  = !isCRZone || !maxFsiRes  || !lotArea || gfa_m2 * 0.6 <= lotArea * maxFsiRes;
  const fsiCommCompliant = !isCRZone || !maxFsiComm || !lotArea || gfa_m2 * 0.4 <= lotArea * maxFsiComm;
  if (isCRZone && !fsiResCompliant)
    violations.push(`Residential GFA likely exceeds FSI ${maxFsiRes} — check CR designation schedule`);
  if (isCRZone && !fsiCommCompliant)
    violations.push(`Commercial GFA likely exceeds FSI ${maxFsiComm} — check CR designation schedule`);

  // ── Height ────────────────────────────────────────────────────────────────
  const heightPct      = maxHeightM != null ? clamp(height_m / maxHeightM * 100, 0, 100) : null;
  const heightCompliant = maxHeightM == null || height_m <= maxHeightM;
  if (!heightCompliant)
    violations.push(`Height ${height_m}m exceeds max ${maxHeightM}m — reduce by ${r1(height_m - maxHeightM)}m`);

  // ── Units ─────────────────────────────────────────────────────────────────
  const unitsCompliant = maxUnits == null || units <= maxUnits;
  if (!unitsCompliant)
    violations.push(`${units} units exceeds max ${maxUnits} — remove ${units - maxUnits} unit(s)`);

  // ── Setbacks ──────────────────────────────────────────────────────────────
  const frontCompliant = frontYardMin == null || front_yard_m >= frontYardMin;
  if (!frontCompliant)
    violations.push(`Front yard ${front_yard_m}m below min ${frontYardMin}m — increase by ${r1(frontYardMin - front_yard_m)}m`);

  const rearCompliant = rearYardMin == null || rear_yard_m >= rearYardMin;
  if (!rearCompliant)
    violations.push(`Rear yard ${rear_yard_m}m below min ${rearYardMin}m — increase by ${r1(rearYardMin - rear_yard_m)}m`);

  const sideCompliant = sideYardMin == null || side_yard_m >= sideYardMin;
  if (!sideCompliant)
    violations.push(`Side yard ${side_yard_m}m below min ${sideYardMin}m — increase by ${r1(sideYardMin - side_yard_m)}m`);

  // ── Building depth ────────────────────────────────────────────────────────
  const depthValue    = building_depth_m ?? null;
  const depthCompliant = maxBuildingDepth == null || depthValue == null || depthValue <= maxBuildingDepth;
  if (!depthCompliant)
    violations.push(`Building depth ${depthValue}m exceeds max ${maxBuildingDepth}m — reduce by ${r1(depthValue - maxBuildingDepth)}m`);

  // ── Parking / bicycle ─────────────────────────────────────────────────────
  const parkingCompliant = parkingMin == null || parking_spaces >= parkingMin;
  if (!parkingCompliant)
    violations.push(`${parking_spaces} parking space(s) below min ${parkingMin} — add ${parkingMin - parking_spaces} more`);

  const bicycleCompliant = bicycleMin == null || bicycle_spaces >= bicycleMin;
  if (!bicycleCompliant)
    violations.push(`${bicycle_spaces} bicycle space(s) below min ${bicycleMin} — add ${bicycleMin - bicycle_spaces} more`);

  // ── Angular plane (§10.X.40.50) ───────────────────────────────────────────
  // The 45° angular plane rises from 7.5m above the REAR LOT LINE.
  // At horizontal distance D from the rear lot line, max height = 7.5 + D.
  // The critical face is the building's front:
  //   D = lot_depth − front_yard − building_depth
  // Effective building depth: use actual value if set; otherwise estimate as
  // 55% of lot depth (Toronto residential typical) capped at 17m, min 10m.
  // Requires lot_depth from PostGIS; skipped if unavailable.
  const angularApplies = isRes && lotDepth != null;
  let angularCompliant = true;
  let angularMinFront  = null;
  let angularLabel     = 'N/A';

  if (angularApplies) {
    const effectiveDepth = building_depth_m
      || Math.min(17, Math.max(10, lotDepth * 0.55));
    const buildingFaceFromRear = lotDepth - (front_yard_m || 0) - effectiveDepth;
    const maxHeightAtFace      = 7.5 + Math.max(0, buildingFaceFromRear);
    angularCompliant = height_m <= maxHeightAtFace;
    angularMinFront  = r1(buildingFaceFromRear);
    angularLabel = angularCompliant
      ? `OK — building face ${r1(buildingFaceFromRear)}m from rear; 45° limit is ${r1(maxHeightAtFace)}m`
      : `FAIL — at ${r1(buildingFaceFromRear)}m from rear, 45° limit is ${r1(maxHeightAtFace)}m; building is ${height_m}m`;
    if (!angularCompliant)
      violations.push(
        `Angular plane: ${height_m}m height exceeds 45° limit of ${r1(maxHeightAtFace)}m ` +
        `at this lot depth — reduce height or increase front yard`
      );
  }

  // ── Floor count + OBC floor height check ─────────────────────────────────
  // params.floors: explicit user override (set by the Floors counter).
  // Otherwise derive: Math.floor gives full floors that fit within height.
  //   11m ÷ 3.0m = 3 full floors (not 4 — a partial storey doesn't count).
  // OBC §9.10.2.1: minimum floor-to-ceiling height 2.4m.
  // If the user has locked floors, validate that height / floors ≥ 2.4m.
  const floorCount    = params.floors ?? Math.max(1, Math.floor(height_m / 3.0));
  const floorHeightM  = r1(height_m / floorCount);
  const OBC_MIN_FLOOR = 2.4;
  const floorHeightCompliant = floorHeightM >= OBC_MIN_FLOOR;
  if (!floorHeightCompliant)
    violations.push(
      `Floor-to-floor height ${floorHeightM}m < ${OBC_MIN_FLOOR}m minimum (OBC §9.10.2.1) — ` +
      `increase height to ${r1(floorCount * OBC_MIN_FLOOR)}m or reduce floor count`
    );

  // ── Summary metrics ───────────────────────────────────────────────────────
  const gfaPerFloor  = Math.round(gfa_m2 / floorCount);
  const remainingLot = lotArea != null ? Math.max(0, lotArea - footprint_m2) : null;
  const coveragePct  = lotArea != null && lotArea > 0 ? r1((footprint_m2 / lotArea) * 100) : null;
  const liveFsi      = lotArea != null && lotArea > 0 ? r1(gfa_m2 / lotArea) : null;

  // ── Multiplex eligibility (By-law 474-2023) ───────────────────────────────
  // 4-unit multiplexes are as-of-right in all R/RD/RS/RT/RM zones city-wide
  // as of November 2023. CR, RA, employment and institutional zones excluded.
  const _mxResZones = ['R', 'RD', 'RS', 'RT', 'RM'];
  const _mxExclPfx  = ['CR', 'RA', 'E', 'O', 'I'];
  let mxEligible = false;
  let mxReason   = '';
  let mxUnits    = null;

  if (_mxResZones.includes(baseZone)) {
    mxEligible = true;
    mxUnits    = 4;
    mxReason   = '4 units as-of-right under By-law 474-2023 (effective Nov 2023)';
    if (c.rooming_house_area)
      mxReason += ' — verify heritage/rooming-house overlay restrictions';
    if (lotArea != null && lotArea < 150)
      mxReason += ` — lot ${lotArea}m² < 150m²: verify minimum lot area for multiplex`;
    // By-law 474-2023 also guarantees minimum 10m height for multiplexes —
    // warn if the user's height is insufficient for the number of units requested.
    if (units >= 2 && height_m < Math.max(6, units * 2.8)) {
      const minHt = r1(Math.max(6, units * 2.8));
      mxReason += ` — ⚠ height ${height_m}m may be insufficient for ${units} stacked units (suggest ≥ ${minHt}m)`;
    }
  } else if (_mxExclPfx.some(pfx => baseZone.startsWith(pfx))) {
    mxEligible = false;
    mxUnits    = null;
    mxReason   = 'Multiplex not as-of-right in this zone category';
  } else {
    mxEligible = false;
    mxUnits    = null;
    mxReason   = 'Multiplex eligibility — confirm zone classification';
  }

  // ── Garden suite feasibility (By-law 569-2013 §150.7) ────────────────────
  // Derive available rear depth from lot geometry.
  //   available_rear = lot_depth − front_yard − effective_building_depth − 1.5m clearance
  // Effective building depth: use actual value if set; otherwise estimate as
  // 55% of lot depth capped at 17m, min 10m (same logic as angular plane).
  const gsApplies = isRes;
  let gsFeasible = false, gsReason = '', gsNote = '';
  if (gsApplies) {
    const effectiveBuildDepth = building_depth_m
      || (lotDepth ? Math.min(17, Math.max(10, lotDepth * 0.55)) : 14);
    const availableRear  = (lotDepth || 0) - (front_yard_m || 0) - effectiveBuildDepth - 1.5;
    const rearYardActual = lotDepth
      ? lotDepth - (front_yard_m || 0) - effectiveBuildDepth
      : rear_yard_m;

    if (!lotArea || lotArea < 250) {
      gsReason = `Lot ${lotArea ? `${lotArea}m²` : 'unknown'} < 250m² minimum (§150.7.20)`;
    } else if (lotFrontage != null && lotFrontage < 7.5) {
      gsReason = `Frontage ${lotFrontage}m < 7.5m minimum (§150.7.20)`;
    } else if (availableRear < 4.5) {
      gsReason = `Only ${r1(Math.max(0, availableRear))}m available in rear — need ≥ 4.5m (§150.7.30)`;
    } else if (rearYardActual < 7.5) {
      gsReason = `Rear yard ${r1(rearYardActual)}m < 7.5m required behind main house (§150.7.30)`;
    } else {
      gsFeasible = true;
      gsReason   = `As-of-right — ${r1(availableRear)}m available in rear yard`;
      gsNote     = 'Max 60m² GFA (or 120m² over 2 storeys), max 6m height, 1.5m from all lot lines (§150.7.60)';
    }
  }

  const overallCompliant =
    footprintCompliant && gfaCompliant && heightCompliant && unitsCompliant &&
    frontCompliant && rearCompliant && sideCompliant && depthCompliant &&
    parkingCompliant && bicycleCompliant && angularCompliant &&
    fsiResCompliant && fsiCommCompliant && floorHeightCompliant;

  return {
    footprint: {
      value:     footprint_m2,
      max:       maxFootprint,
      pct_used:  footprintPct != null ? r1(footprintPct) : null,
      compliant: footprintCompliant,
      label:     maxFootprint != null
        ? `${footprint_m2}m² / ${maxFootprint.toFixed(1)}m² (${r1(footprintPct)}%)`
        : maxCovPct == null && isRes
          ? `${footprint_m2}m² (contextual — no fixed %)`
          : `${footprint_m2}m² (no limit)`,
    },
    gfa: {
      value:     gfa_m2,
      max:       maxGfa,
      pct_used:  gfaPct != null ? r1(gfaPct) : null,
      compliant: gfaCompliant,
      label:     maxGfa != null
        ? `${gfa_m2}m² / ${maxGfa.toFixed(1)}m² (${r1(gfaPct)}%)`
        : `${gfa_m2}m² (no limit)`,
    },
    height: {
      value:     height_m,
      max:       maxHeightM,
      pct_used:  heightPct != null ? r1(heightPct) : null,
      compliant: heightCompliant,
      label:     maxHeightM != null
        ? `${height_m}m / ${maxHeightM}m (${r1(heightPct)}%)`
        : `${height_m}m (no limit)`,
    },
    units: {
      value:     units,
      max:       maxUnits,
      compliant: unitsCompliant,
      label:     maxUnits != null
        ? `${units} / ${maxUnits} units`
        : `${units} unit${units !== 1 ? 's' : ''} (no limit)`,
    },
    front_yard: {
      value:     front_yard_m,
      min:       frontYardMin,
      compliant: frontCompliant,
      label:     frontYardMin != null ? `${front_yard_m}m (min ${frontYardMin}m)` : `${front_yard_m}m`,
    },
    rear_yard: {
      value:     rear_yard_m,
      min:       rearYardMin,
      compliant: rearCompliant,
      label:     rearYardMin != null ? `${rear_yard_m}m (min ${rearYardMin}m)` : `${rear_yard_m}m`,
    },
    side_yard: {
      value:     side_yard_m,
      min:       sideYardMin,
      compliant: sideCompliant,
      label:     sideYardMin != null ? `${side_yard_m}m (min ${sideYardMin}m)` : `${side_yard_m}m`,
    },
    building_depth: {
      value:     depthValue,
      max:       maxBuildingDepth,
      compliant: depthCompliant,
      label:     maxBuildingDepth != null && depthValue != null
        ? `${depthValue}m (max ${maxBuildingDepth}m)`
        : depthValue != null ? `${depthValue}m` : 'n/a',
    },
    parking: {
      value:     parking_spaces,
      min:       parkingMin,
      compliant: parkingCompliant,
      label:     parkingMin != null
        ? `${parking_spaces} / min ${parkingMin} space${parkingMin !== 1 ? 's' : ''}`
        : `${parking_spaces} spaces`,
    },
    bicycle: {
      value:     bicycle_spaces,
      min:       bicycleMin,
      compliant: bicycleCompliant,
      label:     bicycleMin != null
        ? `${bicycle_spaces} / min ${bicycleMin} space${bicycleMin !== 1 ? 's' : ''}`
        : `${bicycle_spaces} spaces`,
    },
    angular_plane: {
      applies:     angularApplies,
      compliant:   angularCompliant,
      min_front_m: angularMinFront,
      label:       angularLabel,
    },
    floor_count:      floorCount,
    floor_height_m:   floorHeightM,
    gfa_per_floor:    gfaPerFloor,
    remaining_lot_m2: remainingLot,
    coverage_pct:     coveragePct,
    live_fsi:         liveFsi,
    garden_suite: {
      applies:  gsApplies,
      feasible: gsFeasible,
      reason:   gsReason,
      note:     gsNote,
    },
    multiplex_eligibility: {
      eligible:      mxEligible,
      reason:        mxReason,
      units_allowed: mxUnits,
    },
    overall_compliant: overallCompliant,
    violations,
  };
}

function clamp(v, lo, hi) { return Math.min(Math.max(v, lo), hi); }
function r1(v) { return Math.round(v * 10) / 10; }
