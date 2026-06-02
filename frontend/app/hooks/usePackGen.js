'use client';
import { useState } from 'react';
import { PACK_STEPS, AI_PACK_STEPS } from '../components/plan/PlanPreview';

/**
 * Encapsulates all pack-generation state and async handlers so
 * ParameterTweaker.jsx stays a thin container.
 */
export default function usePackGen({ parcel, params, zoneSymbol, c, ov, apiBase }) {
  const [showPack,        setShowPack]        = useState(false);
  const [packState,       setPackState]       = useState({ status: 'idle', error: null, stepsLog: [] });
  const [packToken,       setPackToken]       = useState(null);
  const [packZipFilename, setPackZipFilename] = useState(null);
  const [packSvgA,        setPackSvgA]        = useState(null);
  const [packSvgB,        setPackSvgB]        = useState(null);
  const [roomBrief,       setRoomBrief]       = useState(null);

  async function runPackGen(typologyId = null, overrideBrief = null) {
    if (!parcel?.lot_polygon_wkt) {
      alert('No lot polygon available for this parcel. The parcel must be loaded from the map.');
      return;
    }
    setPackSvgA(null);
    setPackSvgB(null);
    setPackToken(null);
    setPackState({ status: 'loading', error: null, stepsLog: [] });
    setShowPack(true);

    const base = apiBase || 'http://localhost:8000';
    const activeBrief = overrideBrief;

    const body = {
      polygon_wkt:           parcel.lot_polygon_wkt,
      zone_symbol:           zoneSymbol || parcel.zone_symbol || '',
      exception_number:      c.exception_number ?? null,
      exception_constraints: Object.keys(ov).length ? ov : null,
      // units_target: use the brief's actual unit count if a brief was provided.
      // This prevents the envelope slider (params.units) from silently multiplying
      // the room program into an unwanted multi-unit building.
      // The envelope calculation in the backend still uses req.units_target for setbacks.
      units_target:          activeBrief ? Math.max(1, activeBrief.units.length) : (params.units ?? 1),
      lot_frontage_m:        c.lot_frontage_m ?? null,
      include_laneway:       false,
      address:               parcel.address || parcel.street_address || '',
      // Per-parcel overlay values from PostGIS — authoritative for this specific lot.
      // These override zone-letter defaults so the generator uses real map values.
      ...(c.max_fsi           != null ? { overlay_fsi:          c.max_fsi }           : {}),
      ...(c.max_height_m      != null ? { overlay_height_m:     c.max_height_m }      : {}),
      ...(c.max_coverage_pct  != null ? { overlay_coverage_pct: c.max_coverage_pct }  : {}),
      ...(params.floors != null ? { target_floors: params.floors } : {}),
      ...(activeBrief?.stack_preference ? { target_stacking: activeBrief.stack_preference } : {}),
      ...(typologyId ? { typology_id: typologyId } : {}),
      ...(activeBrief ? { room_brief: activeBrief } : {}),
    };

    const activeSteps = activeBrief ? AI_PACK_STEPS : PACK_STEPS;

    try {
      const res = await fetch(`${base}/api/generate-pack/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        const detail = Array.isArray(err.detail)
          ? err.detail.map(e => e.msg || JSON.stringify(e)).join('; ')
          : err.detail;
        throw new Error(detail || `HTTP ${res.status}`);
      }

      const reader  = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const raw = line.slice(6).trim();
          if (!raw) continue;
          try {
            const ev = JSON.parse(raw);
            if (ev.type === 'progress') {
              setPackState(s => ({ ...s, stepsLog: [...s.stepsLog, ev.message] }));
            } else if (ev.type === 'done') {
              setPackToken(ev.token);
              if (ev.meta?.zip_filename) setPackZipFilename(ev.meta.zip_filename);
              // SVG previews are included directly in the SSE done payload —
              // no ZIP download needed just to show the floor plan thumbnail.
              if (ev.meta?.svg_a) setPackSvgA(ev.meta.svg_a);
              if (ev.meta?.svg_b) setPackSvgB(ev.meta.svg_b);
              setPackState(s => ({ ...s, status: 'done', stepsLog: activeSteps.slice() }));
            } else if (ev.type === 'error') {
              throw new Error(ev.message);
            }
          } catch (parseErr) {
            if (parseErr.message && !parseErr.message.includes('JSON')) throw parseErr;
          }
        }
      }
    } catch (err) {
      setPackState({ status: 'error', error: err.message, stepsLog: [] });
    }
  }

  async function downloadPack() {
    if (!parcel?.lot_polygon_wkt) return;
    const base = apiBase || 'http://localhost:8000';
    const zone = (zoneSymbol || 'pack').replace(/[^a-z0-9]/gi, '_');

    const filename = packZipFilename || `pack_${zone}.zip`;
    const triggerDownload = (blob) => {
      const url = URL.createObjectURL(blob);
      const a   = document.createElement('a');
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    };

    // Primary path: use cached token from the stream run (instant, no re-generation)
    if (packToken) {
      try {
        const res = await fetch(`${base}/api/generate-pack/download?token=${packToken}`);
        if (res.ok) {
          triggerDownload(await res.blob());
          return;
        }
        // Token expired (410) or not found (404) — fall through to re-generation
      } catch { /* network error — fall through */ }
    }

    // Fallback: re-generate via the synchronous direct endpoint
    const body = {
      polygon_wkt:           parcel.lot_polygon_wkt,
      zone_symbol:           zoneSymbol || parcel.zone_symbol || '',
      exception_number:      c.exception_number ?? null,
      exception_constraints: Object.keys(ov).length ? ov : null,
      units_target:          params.units ?? 2,
      lot_frontage_m:        c.lot_frontage_m ?? null,
      include_laneway:       false,
      address:               parcel.address || parcel.street_address || '',
      ...(c.max_fsi           != null ? { overlay_fsi:          c.max_fsi }           : {}),
      ...(c.max_height_m      != null ? { overlay_height_m:     c.max_height_m }      : {}),
      ...(c.max_coverage_pct  != null ? { overlay_coverage_pct: c.max_coverage_pct }  : {}),
      ...(roomBrief ? { room_brief: roomBrief } : {}),
    };
    try {
      const res = await fetch(`${base}/api/generate-pack/direct`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      triggerDownload(await res.blob());
    } catch (err) {
      alert(`Download failed: ${err.message}`);
    }
  }

  return {
    showPack, setShowPack,
    packState,
    packSvgA, packSvgB,
    roomBrief, setRoomBrief,
    runPackGen,
    downloadPack,
  };
}
