import React, { useState } from 'react';
import { Radio, Flame, Zap, Eye, X, ArrowUpRight, ArrowDownRight, Target, ChevronDown, ChevronRight } from 'lucide-react';

/**
 * Radar alerts grouped into three tiers to cut alert fatigue:
 *   ACTIONABLE - prominent cards, at/near a real entry (e.g. pullback ready, ignition firing)
 *   WATCH      - compact muted rows, a setup is forming
 *   CONTEXT    - a single collapsed strip (market "weather": PCR, IV skew, gamma flip)
 *
 * Categorisation comes from the backend `alert.category`. Anything missing defaults to WATCH.
 */

function categoryOf(alert) {
  return alert.category || 'WATCH';
}

function iconFor(alert, color) {
  const t = alert.alert_type || '';
  if (t.includes('PULLBACK')) return <Target size={16} color={color} />;
  if (t.includes('GAMMA')) return <Eye size={16} color={color} />;
  if (t.includes('IMPULSE') || t.includes('ACCEL')) return <Zap size={16} color={color} />;
  if (t.includes('DRYUP') || t.includes('SQUEEZE') || t.includes('IGNITION')) return <Flame size={16} color={color} />;
  return <Radio size={16} color={color} />;
}

/* ---------------------------- ACTIONABLE card ---------------------------- */
function ActionableCard({ alert, onDismiss }) {
  const isCE = alert.direction === 'CE';
  const accent = isCE ? 'var(--ce-green)' : 'var(--pe-red)';
  const glow = isCE ? 'rgba(0, 230, 118, 0.14)' : 'rgba(255, 51, 102, 0.14)';
  const border = isCE ? 'rgba(0, 230, 118, 0.55)' : 'rgba(255, 51, 102, 0.55)';

  return (
    <div
      className="glass-panel"
      style={{
        padding: '0.8rem 1rem',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: '1rem',
        background: glow,
        borderColor: border,
        borderRadius: 'var(--radius-sm)'
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.85rem', flex: 1, minWidth: 0 }}>
        <div
          style={{
            width: '36px', height: '36px', borderRadius: '50%',
            background: glow, border: `1px solid ${border}`,
            display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0
          }}
        >
          {iconFor(alert, accent)}
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.2rem', minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
            <span
              className="mono"
              style={{
                fontSize: '0.62rem', fontWeight: 800, letterSpacing: '0.06em',
                padding: '2px 7px', borderRadius: '3px',
                background: glow, color: accent, border: `1px solid ${border}`
              }}
            >
              ACTIONABLE
            </span>
            <span
              style={{
                fontSize: '0.68rem', fontWeight: 600, padding: '2px 6px', borderRadius: '3px',
                background: 'rgba(255,255,255,0.05)', color: 'var(--text-secondary)',
                border: '1px solid rgba(255,255,255,0.10)', display: 'flex', alignItems: 'center', gap: '3px'
              }}
            >
              {isCE ? <ArrowUpRight size={11} /> : <ArrowDownRight size={11} />}
              {alert.instrument} {alert.direction}
            </span>
            <span style={{ fontSize: '0.82rem', fontWeight: 700, color: 'var(--text-primary)' }}>
              {alert.title}
            </span>
          </div>
          <div style={{ fontSize: '0.76rem', color: 'var(--text-secondary)' }}>{alert.message}</div>
        </div>
      </div>
      <button
        onClick={() => onDismiss(alert.id)}
        style={{
          background: 'transparent', border: 'none', color: 'var(--text-muted)',
          cursor: 'pointer', padding: '4px', display: 'flex', flexShrink: 0
        }}
        title="Dismiss"
      >
        <X size={15} />
      </button>
    </div>
  );
}

/* ------------------------------- WATCH row ------------------------------- */
function WatchRow({ alert, onDismiss }) {
  const isCE = alert.direction === 'CE';
  return (
    <div
      style={{
        padding: '0.4rem 0.7rem', display: 'flex', alignItems: 'center',
        justifyContent: 'space-between', gap: '0.75rem',
        background: 'rgba(30, 41, 59, 0.45)',
        border: '1px solid rgba(148, 163, 184, 0.18)', borderRadius: 'var(--radius-sm)'
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem', flex: 1, minWidth: 0 }}>
        {iconFor(alert, '#94a3b8')}
        <span
          className="mono"
          style={{
            fontSize: '0.58rem', fontWeight: 700, letterSpacing: '0.05em', padding: '1px 5px',
            borderRadius: '3px', background: 'rgba(148,163,184,0.12)', color: '#cbd5e1',
            border: '1px solid rgba(148,163,184,0.22)', flexShrink: 0
          }}
        >
          WATCH
        </span>
        <span style={{ fontSize: '0.7rem', fontWeight: 600, color: 'var(--text-secondary)', flexShrink: 0 }}>
          {isCE ? <ArrowUpRight size={10} style={{ verticalAlign: 'middle' }} /> : <ArrowDownRight size={10} style={{ verticalAlign: 'middle' }} />}
          {' '}{alert.instrument} {alert.direction}
        </span>
        <span
          style={{
            fontSize: '0.72rem', color: 'var(--text-primary)', overflow: 'hidden',
            textOverflow: 'ellipsis', whiteSpace: 'nowrap'
          }}
        >
          {alert.title}
        </span>
      </div>
      <button
        onClick={() => onDismiss(alert.id)}
        style={{ background: 'transparent', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', padding: '2px', display: 'flex', flexShrink: 0 }}
        title="Dismiss"
      >
        <X size={13} />
      </button>
    </div>
  );
}

/* ---------------------------- CONTEXT strip ------------------------------ */
function ContextStrip({ alerts }) {
  const [expanded, setExpanded] = useState(false);
  if (alerts.length === 0) return null;

  return (
    <div
      style={{
        padding: '0.35rem 0.7rem',
        background: 'rgba(20, 27, 40, 0.5)',
        border: '1px solid rgba(148, 163, 184, 0.12)',
        borderRadius: 'var(--radius-sm)'
      }}
    >
      <button
        onClick={() => setExpanded((e) => !e)}
        style={{
          background: 'transparent', border: 'none', color: 'var(--text-muted)',
          cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '0.4rem',
          width: '100%', fontSize: '0.68rem', fontWeight: 600, letterSpacing: '0.04em'
        }}
      >
        {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        <span className="mono" style={{ color: '#8291a8' }}>MARKET CONTEXT</span>
        <span
          style={{
            fontSize: '0.6rem', padding: '1px 6px', borderRadius: '10px',
            background: 'rgba(148,163,184,0.14)', color: '#a9b6c9'
          }}
        >
          {alerts.length}
        </span>
        <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}>
          {expanded ? '' : 'positioning / volatility signals — tap to view'}
        </span>
      </button>
      {expanded && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem', marginTop: '0.4rem' }}>
          {alerts.map((a) => (
            <div
              key={a.id}
              style={{
                display: 'flex', alignItems: 'center', gap: '0.5rem',
                fontSize: '0.7rem', color: 'var(--text-secondary)', paddingLeft: '1.1rem'
              }}
            >
              <span className="mono" style={{ color: '#6b7688', fontSize: '0.62rem', flexShrink: 0 }}>
                {a.instrument}
              </span>
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {a.title}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function RadarAlertBanner({ alerts, onDismiss }) {
  if (!alerts || alerts.length === 0) return null;

  const actionable = alerts.filter((a) => categoryOf(a) === 'ACTIONABLE');
  const watch = alerts.filter((a) => categoryOf(a) === 'WATCH');
  const context = alerts.filter((a) => categoryOf(a) === 'CONTEXT');

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem', marginBottom: '0.5rem' }}>
      {actionable.map((a) => (
        <ActionableCard key={a.id} alert={a} onDismiss={onDismiss} />
      ))}
      {watch.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.3rem' }}>
          {watch.map((a) => (
            <WatchRow key={a.id} alert={a} onDismiss={onDismiss} />
          ))}
        </div>
      )}
      <ContextStrip alerts={context} />
    </div>
  );
}
