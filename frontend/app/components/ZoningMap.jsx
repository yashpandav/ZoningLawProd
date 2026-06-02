'use client';
import { useRef, useEffect, useState, useCallback } from 'react';
import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import './ZoningMap.css';
import ChatPanel from './ChatPanel';

// ── Official Toronto GIS endpoints ────────────────────────────────────────────

const GIS_IDENTIFY_URL = 'https://gis.toronto.ca/arcgis/rest/services/cot_geospatial11/FeatureServer/18/query';
const GIS_GEOM_URL     = 'https://gis.toronto.ca/arcgis/rest/services/cot_geospatial11/FeatureServer/3/query';
const GEOCODER_URL     = 'https://nominatim.openstreetmap.org/search';

const TORONTO_CENTER = [-79.3832, 43.6532];
const EMPTY = { type: 'FeatureCollection', features: [] };

const ZC = {
  R: '#F5F0C8', RD: '#F5F0C8', RS: '#F5F0C8', RT: '#F5F0C8',
  RM: '#EBD87F', RA: '#D4AA45', RAC: '#D4AA45',
  CL: '#9BAFD4', CR: '#7B9BC4', CRE: '#5A7EAF',
  E:  '#C0392B', EI: '#C0392B', EL: '#E74C3C',
  I:  '#C9A8C8', IS: '#C9A8C8',
  O:  '#7DC987', ON: '#7DC987', OR: '#7DC987',
  UT: '#D3D1C7',
  NA: '#5DCAA5',
};
const FB = '#D3D1C7';

function colourFor(sym) {
  if (!sym) return FB;
  const s = sym.trim().split(/[\s(]/)[0].toUpperCase();
  return ZC[s] || FB;
}

async function getParcelGeometry(lat, lng) {
  const pt = `${lng},${lat}`;
  const params = new URLSearchParams({
    geometry: pt, geometryType: 'esriGeometryPoint',
    spatialRel: 'esriSpatialRelIntersects',
    outFields: 'ZONE_CLASS', returnGeometry: 'true',
    f: 'geojson', inSR: '4326', outSR: '4326',
  });
  const r = await fetch(`${GIS_GEOM_URL}?${params}`);
  if (!r.ok) return null;
  const gj = await r.json();
  return gj.features?.[0] || null;
}

// ─────────────────────────────────────────────────────────────────────────────
// MAIN COMPONENT
// ─────────────────────────────────────────────────────────────────────────────
export default function ZoningMap({ apiBase = 'http://localhost:8000' }) {
  const divRef  = useRef(null);
  const mapRef  = useRef(null);
  const didInit = useRef(false);

  const [zoom,        setZoom]        = useState(13);
  const [parcel,      setParcel]      = useState(null);
  const [loading,     setLoading]     = useState(false);
  const [error,       setError]       = useState(null);
  const [chatMsg,          setChatMsg]          = useState('');
  const [chatHist,         setChatHist]         = useState([]);
  const [chatBusy,         setChatBusy]         = useState(false);
  const [chatMode,         setChatMode]         = useState('quick'); // 'quick' | 'full'
  const [streamingContent, setStreamingContent] = useState('');
  const [isStreaming,      setIsStreaming]      = useState(false);
  const [address,     setAddress]     = useState('');
  const [tilesOk,     setTilesOk]     = useState(true);
  const [suggestions, setSuggestions] = useState([]);
  const [showSugg,    setShowSugg]    = useState(false);
  const [searchBusy,  setSearchBusy]  = useState(false);
  // Session memory
  const [sessionId,     setSessionId]     = useState(null);
  const [sessionResume, setSessionResume] = useState(null); // { message_count, summary, updated_at }

  // Initialise anonymous user ID (persists in localStorage across visits)
  useEffect(() => {
    if (!localStorage.getItem('zoning_user_id')) {
      localStorage.setItem('zoning_user_id', crypto.randomUUID());
    }
  }, []);

  const selectLocationRef = useRef(null);
  const markerRef         = useRef(null);
  // AbortController for in-flight /api/parcel fetches — cancelled on new click
  const fetchAbortRef     = useRef(null);

  useEffect(() => {
    console.log('[Zoning] API base:', apiBase);
  }, [apiBase]);

  // ── Shared parcel lookup ──────────────────────────────────────────────────
  const selectLocation = useCallback(async (lat, lng) => {
    // Cancel any previous in-flight /api/parcel fetch so a rapid double-click
    // never lets a stale response overwrite a newer one.
    fetchAbortRef.current?.abort();
    const controller = new AbortController();
    fetchAbortRef.current = controller;

    setParcel(null);
    setChatHist([]);
    setChatBusy(false);
    setStreamingContent('');
    setIsStreaming(false);
    setChatMode('quick'); // each new parcel starts in quick mode
    setError(null);
    setLoading(true);
    setSessionId(null);
    setSessionResume(null);
    mapRef.current?.getSource('sel')?.setData(EMPTY);

    markerRef.current?.remove();
    if (mapRef.current) {
      markerRef.current = new maplibregl.Marker({ color: '#1A4A35', scale: 1.05 })
        .setLngLat([lng, lat])
        .addTo(mapRef.current);
    }

    try {
      const [feat, data] = await Promise.all([
        getParcelGeometry(lat, lng).catch(() => null),
        fetch(`${apiBase}/api/parcel?lat=${lat}&lng=${lng}`, { signal: controller.signal })
          .then(async (r) => {
            const payload = await r.json().catch(() => ({}));
            if (!r.ok) return { _fetchError: payload?.detail || `HTTP ${r.status}` };
            return payload;
          })
          .catch(err => {
            if (err.name === 'AbortError') return { _aborted: true };
            return { _fetchError: err.message };
          }),
      ]);

      // A newer click already took over — silently discard this response.
      if (data._aborted) return;

      if (feat) {
        mapRef.current?.getSource('sel')?.setData({ type: 'FeatureCollection', features: [feat] });
      }

      if (data._fetchError) {
        setError(`Cannot reach backend at ${apiBase}. Is FastAPI running?\n${data._fetchError}`);
      } else if (data.found === false) {
        setError(data.message || 'No zoning data for this location.');
      } else {
        setParcel(data);

        // Look up any prior session for this user + parcel
        const userId = localStorage.getItem('zoning_user_id') || 'anonymous';
        try {
          const sessRes  = await fetch(
            `${apiBase}/api/sessions?user_id=${encodeURIComponent(userId)}&lat=${lat}&lng=${lng}`,
            { signal: controller.signal },
          );
          const sessData = sessRes.ok ? await sessRes.json() : { found: false };
          if (sessData.found && !controller.signal.aborted) {
            setSessionId(sessData.session_id);
            setSessionResume({
              message_count: sessData.message_count,
              summary:       sessData.summary,
              updated_at:    sessData.updated_at,
            });
            // Pre-populate chat history from server so user sees prior conversation
            const msgRes  = await fetch(
              `${apiBase}/api/session/messages?session_id=${sessData.session_id}&limit=20`,
              { signal: controller.signal },
            );
            const msgData = msgRes.ok ? await msgRes.json() : { messages: [] };
            if (msgData.messages?.length > 0 && !controller.signal.aborted) {
              setChatHist(msgData.messages);
              setChatMode('full'); // resume in analysis mode
            }
          }
        } catch { /* ignore — session lookup is best-effort */ }
      }
    } catch (err) {
      if (err.name === 'AbortError') return;
      setError(`Error: ${err.message}`);
    } finally {
      // Only clear loading for the active request; the aborted one must not
      // clobber the loading state that the newer request already set.
      if (!controller.signal.aborted) {
        setLoading(false);
      }
    }
  }, [apiBase]);

  selectLocationRef.current = selectLocation;

  // ── Map init ──────────────────────────────────────────────────────────────
  useEffect(() => {
    if (didInit.current || !divRef.current) return;
    didInit.current = true;

    const map = new maplibregl.Map({
      container: divRef.current,
      style: {
        version: 8,
        sources: {
          cot_base: {
            type: 'raster',
            tiles: ['https://gis.toronto.ca/arcgis/rest/services/basemap/cot_topo/MapServer/tile/{z}/{y}/{x}'],
            tileSize: 256,
            attribution: '© <a href="https://www.toronto.ca">City of Toronto</a>',
            maxzoom: 19,
          },
          cot_zones: {
            type: 'raster',
            tiles: ['https://tiles.arcgis.com/tiles/As5CFN3ThbQpy8Ph/arcgis/rest/services/Toronto_Zones/MapServer/tile/{z}/{y}/{x}'],
            tileSize: 256,
            minzoom: 10,
            maxzoom: 17,
          },
        },
        layers: [
          { id: 'base',  type: 'raster', source: 'cot_base',  paint: { 'raster-opacity': 1 } },
          { id: 'zones', type: 'raster', source: 'cot_zones', paint: { 'raster-opacity': 0.85 } },
        ],
      },
      center: TORONTO_CENTER,
      zoom: 13,
    });
    mapRef.current = map;

    map.addControl(new maplibregl.NavigationControl(), 'top-right');
    map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left');

    map.on('load', () => {
      map.addSource('sel', { type: 'geojson', data: EMPTY });
      map.addLayer({ id: 'sel-fill', type: 'fill', source: 'sel',
        paint: { 'fill-color': '#1A4A35', 'fill-opacity': 0.10 } });
      map.addLayer({ id: 'sel-line', type: 'line', source: 'sel',
        paint: { 'line-color': '#1A4A35', 'line-width': 2 } });
    });

    map.on('zoom',  () => setZoom(map.getZoom()));
    map.on('error', (e) => {
      if (e?.sourceId === 'cot_base' || e?.sourceId === 'cot_zones') setTilesOk(false);
    });
    map.on('click', (e) => {
      const { lat, lng } = e.lngLat;
      selectLocationRef.current?.(lat, lng);
    });
    map.on('mousemove', () => { map.getCanvas().style.cursor = 'crosshair'; });

    return () => { map.remove(); mapRef.current = null; didInit.current = false; };
  }, [apiBase]);

  // ── Address autocomplete ──────────────────────────────────────────────────
  useEffect(() => {
    if (address.trim().length < 3) { setSuggestions([]); setShowSugg(false); return; }
    const timer = setTimeout(async () => {
      setSearchBusy(true);
      try {
        const params = new URLSearchParams({
          q: `${address}, Toronto, ON`,
          format: 'json', limit: '6', countrycodes: 'ca',
          viewbox: '-79.64,43.86,-79.11,43.58', bounded: '1', addressdetails: '1',
        });
        const res  = await fetch(`${GEOCODER_URL}?${params}`, {
          headers: { 'User-Agent': 'TorontoZoningApp/1.0' },
        });
        const data = await res.json();
        const candidates = data.map(r => ({
          address: r.display_name, lat: parseFloat(r.lat), lng: parseFloat(r.lon),
        }));
        setSuggestions(candidates);
        setShowSugg(candidates.length > 0);
      } catch {
        setSuggestions([]);
      } finally {
        setSearchBusy(false);
      }
    }, 300);
    return () => clearTimeout(timer);
  }, [address]);

  const pickSuggestion = useCallback((candidate) => {
    const { lat, lng } = candidate;
    setAddress(candidate.address);
    setSuggestions([]);
    setShowSugg(false);
    mapRef.current?.flyTo({ center: [lng, lat], zoom: 17, duration: 1000 });
    setTimeout(() => selectLocationRef.current?.(lat, lng), 1100);
  }, []);

  // ── Send chat (SSE streaming) ─────────────────────────────────────────────
  const sendChat = useCallback(async () => {
    if (!chatMsg.trim() || !parcel) return;
    const msg        = chatMsg.trim();
    const activeMode = chatMode;
    const endpoint   = activeMode === 'quick' ? '/api/quick-chat' : '/api/chat';
    const userId     = localStorage.getItem('zoning_user_id') || 'anonymous';

    setChatMsg('');
    setChatHist(h => [...h, { role: 'user', content: msg }]);
    setChatBusy(true);
    setStreamingContent('');
    setIsStreaming(false);
    const _t0 = performance.now();

    // /api/chat uses server-side history; /api/quick-chat is stateless
    const body = activeMode === 'quick'
      ? { lat: parcel.lat, lng: parcel.lng, message: msg }
      : { lat: parcel.lat, lng: parcel.lng, message: msg, user_id: userId, session_id: sessionId || undefined };

    try {
      const res = await fetch(`${apiBase}${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData?.detail || `HTTP ${res.status}`);
      }

      const reader  = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer      = '';
      let accumulated = '';
      let doneData    = null;

      setIsStreaming(true);

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop(); // keep the last (possibly incomplete) line

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const raw = line.slice(6).trim();
          if (!raw) continue;
          try {
            const event = JSON.parse(raw);
            if (event.type === 'token') {
              accumulated += event.content;
              setStreamingContent(accumulated);
            } else if (event.type === 'done') {
              doneData = event;
              if (event.session_id && !sessionId) setSessionId(event.session_id);
            } else if (event.type === 'error') {
              throw new Error(event.message);
            }
          } catch { /* ignore individual parse errors */ }
        }
      }

      const reply = doneData?.reply || accumulated;
      console.group(`⏱ [ZoningMap] ${activeMode.toUpperCase()} — ${endpoint} (SSE)`);
      console.log(`  Total round-trip (incl streaming):  ${(performance.now() - _t0).toFixed(0)} ms`);
      console.log(`  Reply length:                       ${reply.length} chars`);
      console.log(`  Chunks used:                        ${doneData?.chunks_count ?? '?'}`);
      console.log(`  Sections used:`, doneData?.sections_used ?? []);
      console.groupEnd();

      setChatHist(h => [...h, {
        role: 'assistant', content: reply, mode: activeMode,
        message_id: doneData?.message_id || null,
        session_id: doneData?.session_id || sessionId || null,
      }]);
    } catch (err) {
      console.warn(`⏱ [ZoningMap] ${endpoint} FAILED:`, err.message);
      setChatHist(h => [...h, { role: 'assistant', content: `Error: ${err.message}`, mode: activeMode }]);
    } finally {
      setStreamingContent('');
      setIsStreaming(false);
      setChatBusy(false);
    }
  }, [chatMsg, parcel, chatMode, sessionId, apiBase]);

  const showPanel = loading || error || parcel;
  const zoneColor = parcel ? colourFor(parcel.zone_symbol) : null;

  // ── Render ────────────────────────────────────────────────────────────────
  //
  // KEY LAYOUT NOTE:
  //   divRef must be a direct flex child with flex-1 h-full so MapLibre
  //   can read its offsetWidth/offsetHeight correctly and render tiles.
  //   Overlay elements (search, legend, hints) sit in the same root div
  //   which is `position: relative`, so they anchor to the full app width.
  //
  return (
    <div className="relative w-full h-screen flex overflow-hidden" style={{ background: 'var(--color-bg-surface)' }}>

      {/* ── MapLibre canvas (flex-1 so it fills all space left of panel) ── */}
      <div ref={divRef} className="flex-1 h-full min-w-0" />

      {/* ── Tile error banner ── */}
      {!tilesOk && (
        <div className="absolute top-0 inset-x-0 z-30 bg-amber-50 border-b border-amber-200 px-4 py-2 text-xs text-amber-800 text-center font-medium">
          ⚠ Toronto GIS tiles unavailable — try refreshing
        </div>
      )}

      {/* ── Search bar ── */}
      <div className="absolute top-4 left-1/2 -translate-x-1/2 z-20 w-[460px] max-w-[calc(100%-2rem)]">
        <div className="relative">

          <div className="relative flex items-center">
            <svg className="absolute left-3.5 w-4 h-4 pointer-events-none z-10" style={{ color: 'var(--color-text-hint)' }}
              fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
            <input
              className="w-full pl-10 pr-10 py-3 rounded-lg text-sm outline-none transition-all"
              style={{ background: '#FFFFFF', border: '1px solid var(--color-border)', color: 'var(--color-text-primary)', boxShadow: '0 2px 12px rgba(0,0,0,0.08)', fontFamily: 'var(--font-sans)' }}
              placeholder="Search Toronto address…"
              value={address}
              onChange={e => setAddress(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && suggestions.length > 0) pickSuggestion(suggestions[0]);
                if (e.key === 'Escape') { setSuggestions([]); setShowSugg(false); }
              }}
              onFocus={e => { e.currentTarget.style.borderColor = 'var(--color-forest)'; if (suggestions.length > 0) setShowSugg(true); }}
              onBlur={e => { e.currentTarget.style.borderColor = 'var(--color-border)'; setTimeout(() => setShowSugg(false), 150); }}
              autoComplete="off"
            />
            {searchBusy && <div className="spin-sm absolute right-3.5" />}
          </div>

          {showSugg && suggestions.length > 0 && (
            <div className="absolute top-full mt-1.5 inset-x-0 rounded-lg overflow-hidden z-50"
              style={{ background: '#FFFFFF', border: '1px solid var(--color-border)', boxShadow: '0 8px 24px rgba(0,0,0,0.12)' }}>
              {suggestions.map((c, i) => (
                <button key={i}
                  className="w-full flex items-start gap-2.5 px-4 py-2.5 text-left cursor-pointer"
                  style={{ borderBottom: i < suggestions.length - 1 ? '1px solid var(--color-border-light)' : 'none', background: 'transparent', color: 'var(--color-text-muted)', fontSize: 12 }}
                  onMouseEnter={e => { e.currentTarget.style.background = 'var(--color-forest-wash)'; e.currentTarget.style.color = 'var(--color-forest-deep)'; }}
                  onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--color-text-muted)'; }}
                  onMouseDown={() => pickSuggestion(c)}
                >
                  <svg className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" style={{ color: 'var(--color-forest-mid)' }} fill="currentColor" viewBox="0 0 20 20">
                    <path fillRule="evenodd" d="M5.05 4.05a7 7 0 119.9 9.9L10 18.9l-4.95-4.95a7 7 0 010-9.9zM10 11a2 2 0 100-4 2 2 0 000 4z" clipRule="evenodd" />
                  </svg>
                  <span className="leading-snug">{c.address}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* ── Legend ── */}
      <div className="absolute bottom-10 left-3 z-10 rounded-lg px-3.5 py-3"
        style={{ background: 'var(--color-bg-primary)', border: '1px solid var(--color-border)' }}>
        <div style={{ fontSize: 9, fontWeight: 700, color: 'var(--color-text-hint)', textTransform: 'uppercase', letterSpacing: '0.12em', marginBottom: 10 }}>
          Zone Categories
        </div>
        {[
          ['Residential',         ZC.R],
          ['Res. Multiple / Apt', ZC.RM],
          ['Commercial',          ZC.CR],
          ['Employment',          ZC.E],
          ['Institutional',       ZC.I],
          ['Open Space',          ZC.O],
          ['Utility / Transport', ZC.UT],
        ].map(([lbl, col]) => (
          <div key={lbl} className="flex items-center gap-2 mb-1.5 last:mb-0">
            <span className="w-2.5 h-2.5 rounded-[3px] flex-shrink-0"
              style={{ background: col, boxShadow: 'inset 0 0 0 1px rgba(0,0,0,0.12)' }} />
            <span style={{ fontSize: 11, color: 'var(--color-text-muted)' }}>{lbl}</span>
          </div>
        ))}
        <div style={{ fontSize: 9, color: 'var(--color-text-hint)', marginTop: 10, paddingTop: 8, borderTop: '1px solid var(--color-border-light)' }}>
          © City of Toronto
        </div>
      </div>

      {/* ── Hints ── */}
      {zoom < 12 && (
        <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 pointer-events-none z-20 text-sm px-5 py-2.5 rounded-lg whitespace-nowrap"
          style={{ background: 'var(--color-bg-primary)', border: '1px solid var(--color-border)', color: 'var(--color-text-muted)' }}>
          Zoom in to see individual parcels
        </div>
      )}
      {!showPanel && zoom >= 12 && (
        <div className="absolute bottom-14 left-1/2 -translate-x-1/2 pointer-events-none z-20 text-xs px-4 py-2 rounded-full whitespace-nowrap"
          style={{ background: 'var(--color-bg-primary)', border: '1px solid var(--color-border)', color: 'var(--color-text-muted)' }}>
          Click any parcel to see zoning rules
        </div>
      )}

      {/* ── Side panel ── */}
      {showPanel && (
        <div
          className="w-[380px] h-full flex flex-col flex-shrink-0 overflow-hidden z-10"
          style={{ background: 'var(--color-bg-primary)', borderLeft: '1px solid var(--color-border)' }}
        >
          {/* Loading */}
          {loading && (
            <div className="flex items-center gap-3 px-5 py-8 text-sm" style={{ color: 'var(--color-text-muted)' }}>
              <div className="spin" />
              Loading parcel data…
            </div>
          )}

          {/* Error */}
          {error && !loading && (
            <div className="m-4 p-4 rounded-lg text-sm leading-relaxed whitespace-pre-wrap"
              style={{ color: 'var(--color-violation-text)', background: 'var(--color-violation-bg)', border: '1px solid var(--color-violation-border)' }}>
              {error}
            </div>
          )}

          {/* Parcel data */}
          {parcel && !loading && (
            <>
              {/* Zone header — colored left-border accent */}
              <div className="flex flex-shrink-0" style={{ borderBottom: '1px solid var(--color-border)' }}>
                <div className="w-1 flex-shrink-0" style={{ background: 'var(--color-forest-deep)' }} />
                <div className="px-5 pt-5 pb-4 flex-1 min-w-0">
                  {/* Civic address */}
                  <div className="text-[13px] font-semibold leading-snug mb-2.5 truncate" style={{ color: 'var(--color-text-secondary)' }} title={parcel.address || undefined}>
                    {parcel.address
                      ? parcel.address
                      : <span style={{ color: 'var(--color-text-hint)', fontFamily: 'var(--font-mono)', fontSize: 11 }}>{parcel.lat.toFixed(5)}, {parcel.lng.toFixed(5)}</span>
                    }
                  </div>
                  <div style={{ fontFamily: 'var(--font-mono)', fontSize: 30, fontWeight: 700, color: 'var(--color-forest-deep)', lineHeight: 1, letterSpacing: '-0.02em' }}>
                    {parcel.zone_symbol}
                  </div>
                  <div className="text-sm mt-1.5 leading-snug pr-2" style={{ color: 'var(--color-text-muted)' }}>
                    {parcel.zone_label}
                  </div>
                  {parcel.address && (
                    <div style={{ fontSize: 10, color: 'var(--color-text-hint)', fontFamily: 'var(--font-mono)', marginTop: 8 }}>
                      {parcel.lat.toFixed(5)}, {parcel.lng.toFixed(5)}
                    </div>
                  )}
                </div>
              </div>

              {/* Under appeal */}
              {parcel.zone_under_appeal && (
                <div className="px-5 py-2.5 flex-shrink-0 text-xs font-medium"
                  style={{ background: 'var(--color-warn-bg)', borderBottom: '1px solid var(--color-warn-border)', color: 'var(--color-warn-text)' }}>
                  ⚠ Under appeal — not in full force
                </div>
              )}

              {/* Exception */}
              {parcel.exception_number && (
                <div className="px-5 py-2.5 flex-shrink-0 flex justify-between items-center gap-4"
                  style={{ background: 'var(--color-copper-wash)', borderBottom: '1px solid var(--color-copper-border)' }}>
                  <span className="text-xs font-medium whitespace-nowrap" style={{ color: 'var(--color-copper)' }}>
                    Exception #{parcel.exception_number}
                  </span>
                  {parcel.chapter_links?.exception_chapter && (
                    <a href={parcel.chapter_links.exception_chapter.url}
                      target="_blank" rel="noreferrer"
                      className="text-[11px] font-semibold transition-colors whitespace-nowrap"
                      style={{ color: 'var(--color-copper)', fontFamily: 'var(--font-mono)' }}>
                      {parcel.chapter_links.exception_chapter.file} ↗
                    </a>
                  )}
                </div>
              )}

              {/* Scrollable sections */}
              <div className="flex-1 overflow-y-auto panel-scroll">

                <Section title="Lot">
                  <Row label="Frontage"  value={parcel.lot_frontage_m ? `${parcel.lot_frontage_m} m`  : '—'} />
                  <Row label="Area"      value={parcel.lot_area_m2    ? `${parcel.lot_area_m2} m²`    : '—'} />
                  <Row label="Max units" value={parcel.max_units > 0  ? parcel.max_units              : '—'} />
                </Section>

                <Section title="FSI / Density">
                  <Row label="Total FSI"       value={parcel.floor_space_index ?? '—'} />
                  <Row label="Commercial FSI"  value={parcel.fsi_commercial    ?? '—'} />
                  <Row label="Residential FSI" value={parcel.fsi_residential   ?? '—'} />
                  <Row label="Base coverage"   value={parcel.base_coverage_pct ? `${parcel.base_coverage_pct}%` : '—'} />
                </Section>

                <Section title="Overlays">
                  <Row label="Max height"
                    value={parcel.height_overlay_m ? `${parcel.height_overlay_m} m` : 'No overlay'}
                    href={parcel.chapter_links?.height_overlay_chapter?.url} pill="Ch.995" />
                  <Row label="Max coverage"
                    value={parcel.coverage_overlay_pct ? `${parcel.coverage_overlay_pct}%` : 'No overlay'}
                    href={parcel.chapter_links?.coverage_overlay_chapter?.url} pill="Ch.995" />
                  <Row label="Parking zone"
                    value={parcel.parking_zone || 'Standard'}
                    href={parcel.chapter_links?.parking_regulations_chapter?.url} pill="Ch.200" />
                  <Row label="Road class"
                    value={parcel.road_classification ?? '—'}
                    href={parcel.chapter_links?.policy_area_chapter?.url} pill="Ch.970" />
                  {parcel.downtown_setback_applies && (
                    <Row label="Downtown setback" value="Applies"
                      href={parcel.chapter_links?.building_setback_chapter?.url} pill="Ch.600" />
                  )}
                  {parcel.rooming_house_permitted && (
                    <Row label="Rooming house"
                      value={`Area ${parcel.rooming_house_area}, ${parcel.rooming_house_code}`}
                      href={parcel.chapter_links?.rooming_house_chapter?.url} pill="Ch.150" />
                  )}
                  {parcel.retail_frontage_required && (
                    <Row label="Retail frontage" value="Required"
                      href={parcel.chapter_links?.retail_frontage_chapter?.url} pill="Ch.600" />
                  )}
                </Section>

                <Section title="By-law Chapters">
                  <div className="flex flex-wrap gap-1.5 pt-0.5">
                    {parcel.chapter_links &&
                      Object.values(parcel.chapter_links)
                        .filter(Boolean)
                        .map(ch => (
                          <a key={ch.url} href={ch.url} target="_blank" rel="noreferrer"
                            className="inline-block px-2.5 py-1 rounded text-[11px] font-medium transition-colors"
                            style={{ background: 'var(--color-forest-wash)', border: '1px solid var(--color-forest-border)', color: 'var(--color-forest-deep)', fontFamily: 'var(--font-mono)' }}>
                            {ch.file}
                          </a>
                        ))}
                  </div>
                </Section>

                {/* External links — full chat + parameter tweaker */}
                <div className="px-5 pt-3 pb-1 flex flex-col gap-2 flex-shrink-0">
                  <a
                    href={`/chat?lat=${parcel.lat}&lng=${parcel.lng}`}
                    target="_blank"
                    rel="noreferrer"
                    className="flex items-center justify-between w-full px-3.5 py-2.5 rounded-lg text-xs font-medium transition-all group"
                    style={{ border: '1px solid var(--color-border)', color: 'var(--color-text-muted)', background: 'transparent' }}
                    onMouseEnter={e => { e.currentTarget.style.background = 'var(--color-bg-surface)'; e.currentTarget.style.color = 'var(--color-text-primary)'; }}
                    onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--color-text-muted)'; }}
                  >
                    <span className="flex items-center gap-2">
                      <svg className="w-3.5 h-3.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 16 16">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
                          d="M14 9v4a1 1 0 01-1 1H3a1 1 0 01-1-1V3a1 1 0 011-1h4M10 2h4v4M14 2L7 9" />
                      </svg>
                      Open in full chat
                    </span>
                    <span style={{ fontSize: 10, fontFamily: 'var(--font-mono)', color: 'var(--color-text-hint)' }}>
                      More space to chat
                    </span>
                  </a>
                  <a
                    href={`/chat?lat=${parcel.lat}&lng=${parcel.lng}&tab=tweaker`}
                    target="_blank"
                    rel="noreferrer"
                    className="flex items-center justify-between w-full px-3.5 py-2.5 rounded-lg text-xs font-medium transition-all group"
                    style={{ border: '1px solid var(--color-copper-border)', color: 'var(--color-copper)', background: 'var(--color-copper-wash)' }}
                    onMouseEnter={e => { e.currentTarget.style.background = 'var(--color-copper-wash)'; e.currentTarget.style.borderColor = 'var(--color-copper)'; }}
                    onMouseLeave={e => { e.currentTarget.style.background = 'var(--color-copper-wash)'; e.currentTarget.style.borderColor = 'var(--color-copper-border)'; }}
                  >
                    <span className="flex items-center gap-2">
                      <svg className="w-3.5 h-3.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 16 16">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
                          d="M3 8h10M8 3v10M5 5l6 6M11 5l-6 6" />
                      </svg>
                      Parameter Tweaker
                    </span>
                    <span style={{ fontSize: 10, fontFamily: 'var(--font-mono)', color: 'var(--color-copper)' }}>
                      Building envelope calc
                    </span>
                  </a>
                </div>

                {/* Chat — separate component */}
                <ChatPanel
                  chatHist={chatHist}
                  chatBusy={chatBusy}
                  chatMsg={chatMsg}
                  setChatMsg={setChatMsg}
                  onSend={sendChat}
                  mode={chatMode}
                  onModeChange={setChatMode}
                  streamingContent={streamingContent}
                  isStreaming={isStreaming}
                  sessionResume={sessionResume}
                />

              </div>
            </>
          )}
        </div>
      )}

    </div>
  );
}

function Section({ title, children }) {
  return (
    <div className="px-5 py-4" style={{ borderBottom: '1px solid var(--color-border-light)' }}>
      <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--color-text-hint)', textTransform: 'uppercase', letterSpacing: '0.12em', marginBottom: 10 }}>
        {title}
      </div>
      {children}
    </div>
  );
}

function Row({ label, value, href, pill }) {
  return (
    <div className="flex justify-between items-start gap-4 py-1.5">
      <span style={{ fontSize: 13, color: 'var(--color-text-muted)', flexShrink: 0, lineHeight: '20px', paddingTop: 1 }}>{label}</span>
      <span className="flex items-center gap-1.5 flex-wrap justify-end text-right">
        <span style={{ fontSize: 13, fontWeight: 500, fontFamily: 'var(--font-mono)', color: 'var(--color-text-primary)', lineHeight: '20px' }}>{value ?? '—'}</span>
        {href && (
          <a href={href} target="_blank" rel="noreferrer"
            className="inline-block px-2 py-0.5 rounded text-[10px] font-semibold transition-colors whitespace-nowrap"
            style={{ background: 'var(--color-forest-wash)', border: '1px solid var(--color-forest-border)', color: 'var(--color-forest-deep)', fontFamily: 'var(--font-mono)' }}>
            {pill}
          </a>
        )}
      </span>
    </div>
  );
}
