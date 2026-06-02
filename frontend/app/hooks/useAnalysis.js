'use client';
import { useState } from 'react';

/**
 * Encapsulates analysis-report state and the streaming fetch so
 * ParameterTweaker.jsx stays a thin container.
 */
export default function useAnalysis({ parcel, params, zoneSymbol, result, apiBase }) {
  const [showReport,      setShowReport]      = useState(false);
  const [reportContent,   setReportContent]   = useState('');
  const [reportStreaming, setReportStreaming] = useState(false);

  async function runAnalysis() {
    setReportContent('');
    setReportStreaming(true);
    setShowReport(true);

    const base = apiBase || 'http://localhost:8000';
    const body = {
      lat:               parcel?.lat ?? 0,
      lng:               parcel?.lng ?? 0,
      zone_symbol:       zoneSymbol || '',
      footprint_m2:      params.footprint_m2,
      gfa_m2:            params.gfa_m2,
      height_m:          params.height_m,
      units:             params.units ?? 1,
      front_yard_m:      params.front_yard_m,
      rear_yard_m:       params.rear_yard_m,
      side_yard_m:       params.side_yard_m,
      parking_spaces:    params.parking_spaces,
      bicycle_spaces:    params.bicycle_spaces,
      building_depth_m:  params.building_depth_m ?? null,
      overall_compliant: result.overall_compliant,
      violations:        result.violations,
      coverage_pct:      result.coverage_pct ?? null,
      live_fsi:          result.live_fsi     ?? null,
      floor_count:       result.floor_count  ?? 1,
    };

    try {
      const res = await fetch(`${base}/api/analyze-report`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }

      const reader  = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '', accumulated = '';

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
            if (ev.type === 'token') { accumulated += ev.content; setReportContent(accumulated); }
          } catch {}
        }
      }
    } catch (err) {
      setReportContent(`# Error\n\nFailed to generate analysis: ${err.message}\n\nPlease try again.`);
    } finally {
      setReportStreaming(false);
    }
  }

  return { showReport, setShowReport, reportContent, reportStreaming, runAnalysis };
}
