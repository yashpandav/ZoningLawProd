'use client';
import { useState, useId, useRef } from 'react';

const T = {
  accent:    'var(--color-forest-deep)',
  success:   'var(--color-ok-text)',
  danger:    'var(--color-violation-text)',
  violet:    'var(--color-copper)',
  border:    'var(--color-border)',
  borderHi:  'var(--color-border-strong)',
  surface:   'var(--color-bg-wash)',
  t1:        'var(--color-text-primary)',
  t2:        'var(--color-text-muted)',
  t3:        'var(--color-text-hint)',
};

const PRESET_ROOMS = [
  { role: 'powder_room',  label: 'Powder Room' },
  { role: 'dining',       label: 'Dining Room' },
  { role: 'balcony',      label: 'Balcony' },
  { role: 'laundry',      label: 'Laundry' },
  { role: 'home_office',  label: 'Home Office' },
  { role: 'study',        label: 'Study' },
  { role: 'mudroom',      label: 'Mudroom' },
  { role: 'storage',      label: 'Storage Room' },
  { role: 'custom',       label: 'Custom…' },
];

function FloorChip({ label, selected, onClick, disabled = false }) {
  return (
    <button
      onClick={disabled ? undefined : onClick}
      style={{
        padding: '2px 7px', borderRadius: 4,
        cursor: disabled ? 'default' : 'pointer',
        fontSize: 9.5, fontWeight: 700, lineHeight: 1.4,
        background: selected ? 'var(--color-forest-wash)' : 'var(--color-bg-muted)',
        border: `1px solid ${selected ? 'var(--color-forest-border)' : T.border}`,
        color: selected ? 'var(--color-forest-deep)' : disabled ? T.t3 : T.t2,
        opacity: disabled ? 0.35 : 1,
      }}
    >{label}</button>
  );
}

function FloorRow({ label, value, onChange, hasBasement }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      padding: '6px 10px', borderRadius: 6,
      background: T.surface, border: `1px solid ${T.border}`,
      marginBottom: 4,
    }}>
      <span style={{ fontSize: 11, color: T.t1 }}>{label}</span>
      <div style={{ display: 'flex', gap: 3 }}>
        <FloorChip label="B" selected={value === -1} onClick={() => onChange(-1)} disabled={!hasBasement} />
        <FloorChip label="G" selected={value === 0}  onClick={() => onChange(0)} />
        <FloorChip label="U" selected={value === 1}  onClick={() => onChange(1)} />
      </div>
    </div>
  );
}

function Counter({ label, value, onChange, min = 0, max = 10 }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      padding: '9px 12px', borderRadius: 8,
      background: T.surface, border: `1px solid ${T.border}`,
      marginBottom: 6,
    }}>
      <span style={{ fontSize: 12, color: T.t1, fontWeight: 500 }}>{label}</span>
      <div style={{ display: 'flex', alignItems: 'center', gap: 0 }}>
        <button
          onClick={() => onChange(Math.max(min, value - 1))}
          disabled={value <= min}
          style={{
            width: 28, height: 28, borderRadius: '6px 0 0 6px', cursor: value > min ? 'pointer' : 'default',
            background: 'var(--color-bg-muted)', border: `1px solid ${T.border}`,
            color: value > min ? T.t1 : T.t3, fontSize: 16, lineHeight: 1,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}
        >−</button>
        <div style={{
          width: 36, height: 28, display: 'flex', alignItems: 'center', justifyContent: 'center',
          background: 'var(--color-bg-surface)', borderTop: `1px solid ${T.border}`,
          borderBottom: `1px solid ${T.border}`, fontSize: 13, fontWeight: 700, color: T.t1,
          fontFamily: 'var(--font-mono)',
        }}>
          {value}
        </div>
        <button
          onClick={() => onChange(Math.min(max, value + 1))}
          disabled={value >= max}
          style={{
            width: 28, height: 28, borderRadius: '0 6px 6px 0', cursor: value < max ? 'pointer' : 'default',
            background: 'var(--color-bg-muted)', border: `1px solid ${T.border}`,
            color: value < max ? T.t1 : T.t3, fontSize: 16, lineHeight: 1,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}
        >+</button>
      </div>
    </div>
  );
}

function CustomRoomRow({ room, onChange, onRemove, hasBasement }) {
  const sp = room.storeyPref ?? 0;
  return (
    <div style={{
      padding: '8px 10px', borderRadius: 8,
      background: T.surface, border: `1px solid ${T.border}`,
      marginBottom: 6,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: room.showInstruction ? 6 : 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flex: 1, flexWrap: 'wrap' }}>
          <span style={{
            fontSize: 11, fontWeight: 600, color: 'var(--color-forest-deep)',
            background: 'var(--color-forest-wash)', border: '1px solid var(--color-forest-border)',
            borderRadius: 5, padding: '2px 8px',
          }}>
            {room.label}
          </span>
          <button
            onClick={() => onChange({ ...room, showInstruction: !room.showInstruction })}
            style={{
              fontSize: 9.5, color: T.t3, background: 'none', border: 'none',
              cursor: 'pointer', padding: 0, textDecoration: 'underline dotted',
            }}
          >
            {room.showInstruction ? 'hide note' : '+ note'}
          </button>
          <div style={{ display: 'flex', gap: 2 }}>
            <FloorChip label="B" selected={sp === -1} onClick={() => onChange({ ...room, storeyPref: -1 })} disabled={!hasBasement} />
            <FloorChip label="G" selected={sp === 0}  onClick={() => onChange({ ...room, storeyPref: 0 })} />
            <FloorChip label="U" selected={sp === 1}  onClick={() => onChange({ ...room, storeyPref: 1 })} />
          </div>
        </div>
        <button onClick={onRemove} style={{
          width: 20, height: 20, borderRadius: 4, cursor: 'pointer',
          background: 'var(--color-violation-bg)', border: '1px solid var(--color-violation-border)',
          color: T.danger, fontSize: 13, lineHeight: 1,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          flexShrink: 0,
        }}>×</button>
      </div>
      {room.showInstruction && (
        <input
          type="text"
          value={room.instruction}
          onChange={e => onChange({ ...room, instruction: e.target.value })}
          placeholder="e.g. north-facing, ensuite to master, open to living"
          style={{
            width: '100%', boxSizing: 'border-box',
            background: 'var(--color-bg-muted)', border: `1px solid ${T.border}`,
            borderRadius: 5, color: T.t1, fontSize: 10.5, padding: '4px 8px',
            fontFamily: 'inherit',
          }}
        />
      )}
    </div>
  );
}

export default function RoomBriefForm({
  unitsCount, onUnitsCountChange,
  bedrooms, onBedroomsChange,
  living, onLivingChange,
  bathrooms, onBathroomsChange,
  customRooms, onCustomRoomsChange,
  stackPref, onStackPrefChange,
  notes, onNotesChange,
  mode, onModeChange,
  freeText, onFreeTextChange,
  parsing, parseError, parsedBanner,
  hasBasement, onHasBasementChange,
  floorAssignment, onFloorAssignmentChange,
}) {
  const [addingRoom, setAddingRoom] = useState(false);
  const uid = useId();
  const counterRef = useRef(0);

  function addPreset(preset) {
    setAddingRoom(false);
    if (preset.role === 'custom') {
      const name = window.prompt('Room name:');
      if (!name?.trim()) return;
      const newRoom = {
        id: `${uid}-${++counterRef.current}`,
        role: name.trim().toLowerCase().replace(/\s+/g, '_'),
        label: name.trim(),
        instruction: '',
        showInstruction: false,
        storeyPref: 0,
      };
      onCustomRoomsChange(prev => [...prev, newRoom]);
    } else {
      const newRoom = {
        id: `${uid}-${++counterRef.current}`,
        role: preset.role,
        label: preset.label,
        instruction: '',
        showInstruction: false,
        storeyPref: 0,
      };
      onCustomRoomsChange(prev => [...prev, newRoom]);
    }
  }

  function updateRoom(id, updates) {
    onCustomRoomsChange(prev => prev.map(r => r.id === id ? { ...r, ...updates } : r));
  }

  function removeRoom(id) {
    onCustomRoomsChange(prev => prev.filter(r => r.id !== id));
  }

  return (
    <div style={{ padding: '0 24px 8px' }}>
      <div style={{ fontSize: 13, fontWeight: 700, color: T.t1, marginBottom: 4 }}>
        Describe your program
      </div>
      <div style={{ fontSize: 10.5, color: T.t3, marginBottom: 14 }}>
        Optional — helps AI rank configurations for your needs. You can skip this step.
      </div>

      {/* Mode toggle */}
      <div style={{ display: 'flex', gap: 6, marginBottom: 18 }}>
        {[['structured', '⊞ Rooms'], ['freetext', '✎ Free text']].map(([m, label]) => (
          <button key={m} onClick={() => onModeChange(m)} style={{
            padding: '5px 14px', borderRadius: 7, cursor: 'pointer',
            fontSize: 11, fontWeight: 600,
            background: mode === m ? 'var(--color-forest-wash)' : T.surface,
            border: `1px solid ${mode === m ? 'var(--color-forest-border)' : T.border}`,
            color: mode === m ? 'var(--color-forest-deep)' : T.t2,
          }}>{label}</button>
        ))}
      </div>

      {mode === 'structured' && (
        <div>
          {/* Dwelling units — the most important decision */}
          <div style={{ fontSize: 9, color: T.t3, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8 }}>
            Dwelling Units
          </div>
          <Counter label="Units in this building" value={unitsCount ?? 1} onChange={onUnitsCountChange} min={1} max={6} />
          <div style={{
            fontSize: 9.5, color: T.t3, lineHeight: 1.5, marginBottom: 16,
            padding: '6px 10px', borderRadius: 6,
            background: 'var(--color-bg-surface)', border: `1px solid ${T.border}`,
          }}>
            {(unitsCount ?? 1) === 1
              ? '1 unit — single-family house. Rooms below describe the whole house.'
              : `${unitsCount} units — each unit gets the same room program below.`}
          </div>

          {/* Core room counters — describe ONE unit's program */}
          <div style={{ fontSize: 9, color: T.t3, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8 }}>
            Rooms {(unitsCount ?? 1) > 1 ? 'per unit' : ''}
          </div>

          <Counter label="Bedrooms" value={bedrooms} onChange={onBedroomsChange} min={0} max={10} />
          <Counter label="Living Room" value={living} onChange={onLivingChange} min={0} max={3} />
          <Counter label="Bathrooms" value={bathrooms} onChange={onBathroomsChange} min={1} max={6} />

          {/* Kitchen pill — always included */}
          <div style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            padding: '9px 12px', borderRadius: 8, marginBottom: 14,
            background: 'var(--color-ok-bg)', border: '1px solid var(--color-ok-border)',
          }}>
            <span style={{ fontSize: 12, color: 'var(--color-ok-text)', fontWeight: 500 }}>Kitchen</span>
            <span style={{ fontSize: 9.5, color: 'var(--color-ok-text)', opacity: 0.6 }}>always included</span>
          </div>

          {/* Custom rooms */}
          {customRooms.length > 0 && (
            <div style={{ marginBottom: 10 }}>
              <div style={{ fontSize: 9, color: T.t3, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8 }}>
                Extra rooms
              </div>
              {customRooms.map(room => (
                <CustomRoomRow
                  key={room.id}
                  room={room}
                  onChange={updates => updateRoom(room.id, updates)}
                  onRemove={() => removeRoom(room.id)}
                  hasBasement={hasBasement}
                />
              ))}
            </div>
          )}

          {/* Add room */}
          <div style={{ position: 'relative', marginBottom: 16 }}>
            <button
              onClick={() => setAddingRoom(v => !v)}
              style={{
                padding: '6px 14px', borderRadius: 7, cursor: 'pointer',
                fontSize: 11, fontWeight: 600,
                background: addingRoom ? 'var(--color-copper-wash)' : T.surface,
                border: `1px solid ${addingRoom ? 'var(--color-copper-border)' : T.border}`,
                color: addingRoom ? 'var(--color-copper)' : T.t2,
              }}
            >
              {addingRoom ? '✕ Cancel' : '+ Add a room'}
            </button>
            {addingRoom && (
              <div style={{
                position: 'absolute', top: '100%', left: 0, marginTop: 4, zIndex: 10,
                background: 'var(--color-bg-primary)', border: `1px solid ${T.borderHi}`,
                borderRadius: 8, padding: 6, minWidth: 180,
                boxShadow: '0 8px 24px rgba(0,0,0,0.12)',
              }}>
                {PRESET_ROOMS.map(p => (
                  <button
                    key={p.role}
                    onClick={() => addPreset(p)}
                    style={{
                      display: 'block', width: '100%', textAlign: 'left',
                      padding: '7px 12px', borderRadius: 6, cursor: 'pointer',
                      background: 'none', border: 'none',
                      color: p.role === 'custom' ? T.violet : T.t1,
                      fontSize: 11, fontWeight: p.role === 'custom' ? 600 : 400,
                    }}
                    onMouseEnter={e => { e.currentTarget.style.background = 'var(--color-bg-surface)'; }}
                    onMouseLeave={e => { e.currentTarget.style.background = 'none'; }}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* Floor placement */}
          <div style={{ marginBottom: 14 }}>
            <div style={{ fontSize: 9, color: T.t3, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8 }}>
              Floor placement
            </div>

            {/* Basement toggle */}
            <div style={{
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              padding: '8px 10px', borderRadius: 7, marginBottom: 8,
              background: hasBasement ? 'var(--color-copper-wash)' : T.surface,
              border: `1px solid ${hasBasement ? 'var(--color-copper-border)' : T.border}`,
            }}>
              <div>
                <div style={{ fontSize: 11, color: hasBasement ? 'var(--color-copper)' : T.t2, fontWeight: 500 }}>
                  Includes basement
                </div>
                <div style={{ fontSize: 9, color: T.t3, marginTop: 1 }}>
                  B = basement · G = ground · U = upper
                </div>
              </div>
              <button
                onClick={() => onHasBasementChange(!hasBasement)}
                style={{
                  width: 36, height: 20, borderRadius: 10, cursor: 'pointer',
                  background: hasBasement ? 'var(--color-copper)' : 'var(--color-bg-muted)',
                  border: `1px solid ${hasBasement ? 'var(--color-copper)' : T.border}`,
                  position: 'relative', padding: 0, flexShrink: 0,
                }}
              >
                <div style={{
                  width: 14, height: 14, borderRadius: '50%', background: '#fff',
                  position: 'absolute', top: 2,
                  left: hasBasement ? 18 : 2,
                  transition: 'left 0.18s',
                }} />
              </button>
            </div>

            {/* Per-room-instance floor rows — each room gets its own B/G/U picker */}
            {bedrooms > 0 && (() => {
              const arr = Array.isArray(floorAssignment.bedrooms)
                ? floorAssignment.bedrooms.slice(0, bedrooms)
                : Array(bedrooms).fill(floorAssignment.bedrooms);
              return arr.map((floor, i) => (
                <FloorRow
                  key={`br-${i}`}
                  label={bedrooms === 1 ? 'Bedroom' : `Bedroom ${i + 1}`}
                  value={floor}
                  onChange={v => {
                    const next = [...arr]; next[i] = v;
                    onFloorAssignmentChange({ ...floorAssignment, bedrooms: next });
                  }}
                  hasBasement={hasBasement}
                />
              ));
            })()}
            {living > 0 && (() => {
              const arr = Array.isArray(floorAssignment.living)
                ? floorAssignment.living.slice(0, living)
                : Array(living).fill(floorAssignment.living);
              return arr.map((floor, i) => (
                <FloorRow
                  key={`lv-${i}`}
                  label={living === 1 ? 'Living Room' : `Living Room ${i + 1}`}
                  value={floor}
                  onChange={v => {
                    const next = [...arr]; next[i] = v;
                    onFloorAssignmentChange({ ...floorAssignment, living: next });
                  }}
                  hasBasement={hasBasement}
                />
              ));
            })()}
            {bathrooms > 0 && (() => {
              const arr = Array.isArray(floorAssignment.bathrooms)
                ? floorAssignment.bathrooms.slice(0, bathrooms)
                : Array(bathrooms).fill(floorAssignment.bathrooms);
              return arr.map((floor, i) => (
                <FloorRow
                  key={`ba-${i}`}
                  label={bathrooms === 1 ? 'Bathroom' : `Bathroom ${i + 1}`}
                  value={floor}
                  onChange={v => {
                    const next = [...arr]; next[i] = v;
                    onFloorAssignmentChange({ ...floorAssignment, bathrooms: next });
                  }}
                  hasBasement={hasBasement}
                />
              ));
            })()}
            <FloorRow
              label="Kitchen"
              value={typeof floorAssignment.kitchen === 'number' ? floorAssignment.kitchen : 0}
              onChange={v => onFloorAssignmentChange({ ...floorAssignment, kitchen: v })}
              hasBasement={hasBasement}
            />
          </div>

          {/* Stacking preference */}
          <div style={{ marginBottom: 14 }}>
            <div style={{ fontSize: 9, color: T.t3, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 6 }}>
              Stacking preference
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              {[['vertical', '↕ Vertical (stacked)'], ['horizontal', '↔ Horizontal (side-by-side)']].map(([opt, label]) => (
                <button key={opt} onClick={() => onStackPrefChange(opt)} style={{
                  padding: '5px 14px', borderRadius: 7, cursor: 'pointer',
                  fontSize: 10, fontWeight: 600,
                  background: stackPref === opt ? 'var(--color-copper-wash)' : T.surface,
                  border: `1px solid ${stackPref === opt ? 'var(--color-copper-border)' : T.border}`,
                  color: stackPref === opt ? 'var(--color-copper)' : T.t2,
                }}>{label}</button>
              ))}
            </div>
          </div>

          {/* Notes */}
          <div>
            <div style={{ fontSize: 9, color: T.t3, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 6 }}>
              Additional notes (optional)
            </div>
            <textarea value={notes} onChange={e => onNotesChange(e.target.value)}
              placeholder="e.g. open-concept ground floor, master suite on top, accessible entrance…"
              rows={2} style={{
                background: 'var(--color-bg-muted)', border: `1px solid ${T.border}`,
                borderRadius: 5, color: T.t1, fontSize: 11, padding: '5px 9px',
                width: '100%', resize: 'vertical', fontFamily: 'inherit', lineHeight: 1.5,
                boxSizing: 'border-box',
              }} />
          </div>
        </div>
      )}

      {mode === 'freetext' && (
        <div>
          <textarea
            value={freeText}
            onChange={e => onFreeTextChange(e.target.value)}
            placeholder={
              'e.g. Three bedrooms, two bathrooms, open-plan living and dining. ' +
              'Home office on the ground floor. Laundry in basement.'
            }
            rows={6}
            style={{
              background: 'var(--color-bg-muted)', border: `1px solid ${T.borderHi}`,
              borderRadius: 7, color: T.t1, fontSize: 12, padding: '10px 12px',
              width: '100%', resize: 'vertical', fontFamily: 'inherit', lineHeight: 1.6,
              boxSizing: 'border-box',
            }}
          />

          {parsing && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 10, color: T.t2, fontSize: 11 }}>
              <div style={{
                width: 12, height: 12, borderRadius: '50%', flexShrink: 0,
                border: `2px solid ${T.accent}`, borderTopColor: 'transparent',
                animation: 'cp-spin 0.8s linear infinite',
              }} />
              Parsing brief with AI…
            </div>
          )}

          {parseError && !parsing && (
            <div style={{
              marginTop: 10, padding: '8px 12px', borderRadius: 7,
              background: 'var(--color-violation-bg)', border: '1px solid var(--color-violation-border)',
              color: T.danger, fontSize: 11,
            }}>
              {parseError}
            </div>
          )}

          {parsedBanner && !parsing && (
            <div style={{
              marginTop: 10, padding: '8px 12px', borderRadius: 7,
              background: 'var(--color-ok-bg)', border: '1px solid var(--color-ok-border)',
              color: T.success, fontSize: 11,
            }}>
              ✓ {parsedBanner}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
