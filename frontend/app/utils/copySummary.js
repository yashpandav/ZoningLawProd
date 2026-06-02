/**
 * Build the plain-text compliance summary that gets copied to clipboard.
 * Pure function — no React dependencies.
 */
export function buildCopySummary({
  params, c, result, zoneSymbol,
  maxCov, maxFsi, maxHt, maxUnits, frontMin, rearMin, sideMin,
  isRes, isHolding, isUnderAppeal,
}) {
  const today  = new Date().toLocaleDateString('en-CA');
  const ov     = c.exception_overrides || {};
  const status = result.overall_compliant
    ? 'COMPLIANT'
    : `NON-COMPLIANT — ${result.violations.length} violation(s)`;

  const lines = [
    `TORONTO ZONING COMPLIANCE SUMMARY`,
    `Generated: ${today}  |  Zone: ${zoneSymbol || '—'}  |  Status: ${status}`,
    ``,
    `PARCEL`,
    `  Lot area:  ${c.lot_area_m2 || '?'} m²  |  Frontage: ${c.lot_frontage_m || '?'} m  |  Depth: ${c.lot_depth_m || '?'} m`,
    c.exception_number ? `  Exception #${c.exception_number} applies — base zone rules modified` : null,
    isHolding    ? `  ⛔  HOLDING ZONE — no permit without bylaw amendment` : null,
    isUnderAppeal ? `  ⚠️   UNDER APPEAL — provisions may change` : null,
    ``,
    `PROPOSED DEVELOPMENT`,
    `  Footprint:      ${params.footprint_m2} m²  (${result.coverage_pct ?? '?'}% coverage)`,
    `  GFA:            ${params.gfa_m2} m²  (FSI ${result.live_fsi ?? '?'})`,
    `  Height:         ${params.height_m} m  (${result.floor_count} floor${result.floor_count !== 1 ? 's' : ''})`,
    maxUnits != null ? `  Units:          ${params.units}` : null,
    `  Front yard:     ${params.front_yard_m} m`,
    `  Rear yard:      ${params.rear_yard_m} m`,
    `  Side yard:      ${params.side_yard_m} m`,
    isRes && params.building_depth_m ? `  Building depth: ${params.building_depth_m} m` : null,
    `  Parking:        ${params.parking_spaces} space(s)`,
    `  Bicycle:        ${params.bicycle_spaces} space(s)`,
    ``,
    `LIMITS (By-law 569-2013${c.exception_number ? ` + Exception #${c.exception_number}` : ''})`,
    maxCov  != null ? `  Max coverage:   ${maxCov}%  (${result.footprint.max?.toFixed(1) ?? '?'} m²)` : `  Coverage:       Contextual (no fixed %)`,
    maxFsi  != null ? `  Max FSI:        ${maxFsi}` : `  FSI:            No cap`,
    maxHt   != null ? `  Max height:     ${maxHt} m` : `  Height:         No overlay`,
    maxUnits != null ? `  Max units:      ${maxUnits}` : null,
    frontMin != null ? `  Front yard min: ${frontMin} m` : `  Front yard:     Contextual`,
    `  Rear yard min:  ${rearMin ?? '?'} m`,
    `  Side yard min:  ${sideMin ?? '?'} m`,
    ``,
    result.violations.length === 0
      ? `COMPLIANCE: All parameters within By-law limits.`
      : `VIOLATIONS:\n${result.violations.map(v => `  ✗ ${v}`).join('\n')}`,
    ``,
    result.garden_suite?.applies
      ? `GARDEN SUITE: ${result.garden_suite.feasible
          ? `Feasible — ${result.garden_suite.reason}`
          : `Not feasible — ${result.garden_suite.reason}`}`
      : null,
    ``,
    `NOTE: This analysis is based on City of Toronto GIS data and By-law 569-2013.`,
    `Verify with a registered planner before permit application.`,
    c.bylaw_chapter ? `By-law Chapter: ${c.bylaw_chapter}` : null,
  ].filter(l => l !== null);

  return lines.join('\n');
}
