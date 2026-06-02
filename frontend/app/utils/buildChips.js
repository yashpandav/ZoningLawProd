/**
 * Build the compliance-chip descriptor array for ParameterTweaker.
 * Pure function — no React dependencies.
 */
export function buildChips({
  params, result, c, ov,
  maxCov, maxFsi, maxUnits, maxBd,
  isRes, zoneSymbol, onAskClaude,
}) {
  function chipAskHandler(label, value) {
    if (!onAskClaude) return undefined;
    return () => onAskClaude(
      `Violation in my Parameter Tweaker: ${label} = ${value} is non-compliant.\n` +
      `Zone: ${zoneSymbol || '—'} | Lot: ${c.lot_area_m2 || '?'}m² | Frontage: ${c.lot_frontage_m || '?'}m\n` +
      `${c.exception_number ? `Exception #${c.exception_number} applies.\n` : ''}` +
      `Please cite the specific By-law 569-2013 section that sets this limit, state the exact maximum/minimum value, ` +
      `and give me 2–3 practical ways to bring this into compliance.`
    );
  }

  return [
    ...(maxCov   != null ? [{ key:'footprint',     label:'Coverage',    val:`${params.footprint_m2}m²` }] : []),
    ...(maxFsi   != null ? [{ key:'gfa',           label:'GFA / FSI',   val:`${params.gfa_m2}m²`       }] : []),
    { key:'height',         label:'Height',        val:`${params.height_m}m`     },
    ...(maxUnits != null ? [{ key:'units',          label:'Units',       val:`${params.units}`           }] : []),
    { key:'front_yard',     label:'Front yard',    val:`${params.front_yard_m}m`  },
    { key:'rear_yard',      label:'Rear yard',     val:`${params.rear_yard_m}m`   },
    { key:'side_yard',      label:'Side yard',     val:`${params.side_yard_m}m`   },
    ...(isRes && maxBd != null ? [{ key:'building_depth', label:'Bldg depth', val:`${params.building_depth_m}m` }] : []),
    ...(isRes && result.angular_plane.applies ? [{ key:'angular_plane', label:'Angular plane', val:`${params.height_m}m` }] : []),
    { key:'parking',        label:'Parking',       val:`${params.parking_spaces}` },
    { key:'bicycle',        label:'Bicycle',       val:`${params.bicycle_spaces}` },
    ...(isRes ? [{
      key:       'multiplex',
      label:     '4-Plex',
      val:       `${params.units} units`,
      compliant: result.multiplex_eligibility?.eligible ?? true,
      _askOverride: onAskClaude ? () => onAskClaude(
        `Is this ${zoneSymbol} parcel eligible for a 4-unit multiplex under By-law 156-2023? ` +
        `What are the exact requirements, and do any site-specific conditions (exception #${c.exception_number || 'none'}, ` +
        `lot area ${c.lot_area_m2 || '?'}m², frontage ${c.lot_frontage_m || '?'}m) affect eligibility?`
      ) : undefined,
    }] : []),
  ].map(ch => ({ ...ch, _ask: ch._askOverride ?? chipAskHandler(ch.label, ch.val) }));
}
