import { r } from '../components/shared';

/**
 * Build preset parameter sets from constraints.
 * Pure utility — all values derived from constraints props.
 */
export function makeParamPresets({
  lotArea, maxCov, maxFsi, maxHt, maxUnits, maxBd,
  maxFpSl, maxGfaSl, frontMin, rearMin, sideMin, parkMin, bikMin, isRes,
}) {
  function makeParams(coveragePct, heightPct, unitsFrac, frontFrac, rearFrac, sideFrac) {
    const fp  = lotArea && maxCov  ? Math.round(lotArea * maxCov / 100 * coveragePct) : Math.round(maxFpSl * coveragePct);
    const gfa = lotArea && maxFsi  ? Math.round(lotArea * maxFsi * coveragePct)        : Math.round(maxGfaSl * coveragePct);
    return {
      footprint_m2:     Math.max(20, fp),
      gfa_m2:           Math.max(30, gfa),
      height_m:         maxHt    ? r(maxHt    * heightPct, 0.5) : 6.0,
      units:            maxUnits ? Math.max(1, Math.floor(maxUnits * unitsFrac)) : 1,
      front_yard_m:     frontMin != null ? r(frontMin * frontFrac, 0.1) : 3.0,
      rear_yard_m:      rearMin  != null ? r(rearMin  * rearFrac,  0.1) : 7.5,
      side_yard_m:      sideMin  != null ? r(sideMin  * sideFrac,  0.1) : 0.9,
      parking_spaces:   parkMin,
      bicycle_spaces:   bikMin,
      building_depth_m: isRes && maxBd ? r(maxBd * heightPct, 0.5) : null,
    };
  }

  const initParams    = () => makeParams(0.80, 0.80, 0.80, 1.20, 1.20, 1.20);
  const paramsMax     = () => makeParams(1.00, 1.00, 1.00, 1.00, 1.00, 1.00);
  const paramsConserv = () => makeParams(0.60, 0.65, 0.50, 1.50, 1.50, 1.50);
  const paramsGardenSuite = () => ({
    footprint_m2:     Math.min(50, maxFpSl),
    gfa_m2:           60,
    height_m:         5.5,
    units:            1,
    front_yard_m:     frontMin != null ? r(frontMin * 1.2, 0.1) : 3.0,
    rear_yard_m:      rearMin  != null ? r(rearMin  + 9,   0.1) : 16.0,
    side_yard_m:      Math.max(sideMin ?? 0.9, 1.5),
    parking_spaces:   0,
    bicycle_spaces:   1,
    building_depth_m: null,
  });
  const params4Plex = () => ({
    footprint_m2:     lotArea ? Math.round(lotArea * (maxCov ? maxCov / 100 : 0.4) * 0.95) : 160,
    gfa_m2:           lotArea && maxFsi ? Math.round(lotArea * maxFsi * 0.95) : 360,
    height_m:         maxHt ? r(maxHt * 0.95, 0.5) : 10.0,
    units:            Math.min(maxUnits ?? 4, 4),
    front_yard_m:     frontMin != null ? r(frontMin * 1.05, 0.1) : 3.0,
    rear_yard_m:      rearMin  != null ? r(rearMin  * 1.05, 0.1) : 7.5,
    side_yard_m:      sideMin  != null ? r(sideMin  * 1.05, 0.1) : 0.9,
    parking_spaces:   Math.max(parkMin, 2),
    bicycle_spaces:   Math.max(bikMin, 2),
    building_depth_m: isRes && maxBd ? r(maxBd * 0.95, 0.5) : null,
  });

  const safeInit = () => {
    try { return initParams(); }
    catch {
      return {
        footprint_m2: 100, gfa_m2: 200, height_m: 8, units: 2,
        front_yard_m: 3.0, rear_yard_m: 7.5, side_yard_m: 0.9,
        parking_spaces: 1, bicycle_spaces: 2, building_depth_m: 12,
      };
    }
  };

  const PRESETS = [
    { id: 'max',     label: 'Max Density',   desc: 'Legal limits',   fn: paramsMax },
    { id: 'typical', label: 'Typical Build', desc: '80% of limits',  fn: initParams },
    { id: 'conserv', label: 'Conservative',  desc: '60% of limits',  fn: paramsConserv },
    ...(isRes ? [
      { id: 'suite', label: 'Garden Suite',  desc: '1-storey rear',  fn: paramsGardenSuite },
      { id: '4plex', label: '4-Plex',        desc: 'Max multiplex',  fn: params4Plex },
    ] : []),
  ];

  return { initParams, safeInit, PRESETS };
}
