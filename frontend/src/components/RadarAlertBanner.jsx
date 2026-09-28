import React from 'react';
import { Radio, Flame, Zap, Eye, X, ArrowUpRight, ArrowDownRight } from 'lucide-react';

export function RadarAlertBanner({ alerts, onDismiss }) {
  if (!alerts || alerts.length === 0) return null;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.6rem', marginBottom: '0.5rem' }}>
      {alerts.map((alert) => {
        const isCE = alert.direction === 'CE';
        const isDryUp = alert.alert_type?.includes('DRYUP') || alert.alert_type?.includes('SQUEEZE');
        const isContact = alert.alert_type?.includes('CONTACT');
        const isGamma = alert.alert_type?.includes('GAMMA');
        const isImpulse = alert.alert_type?.includes('IMPULSE');

        const accentColor = isImpulse
          ? 'var(--accent-cyan)'
          : isGamma
          ? 'var(--accent-amber)'
          : isCE
          ? 'var(--ce-green)'
          : 'var(--pe-red)';

        const glowBg = isImpulse
          ? 'rgba(0, 240, 255, 0.12)'
          : isGamma
          ? 'rgba(255, 170, 0, 0.08)'
          : isCE
          ? 'rgba(0, 230, 118, 0.08)'
          : 'rgba(255, 51, 102, 0.08)';

        const borderColor = isImpulse
          ? 'rgba(0, 240, 255, 0.50)'
          : isGamma
          ? 'rgba(255, 170, 0, 0.35)'
          : isCE
          ? 'rgba(0, 230, 118, 0.35)'
          : 'rgba(255, 51, 102, 0.35)';

        return (
          <div
            key={alert.id}
            className="glass-panel"
            style={{
              padding: '0.75rem 1rem',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: '1rem',
              background: 'rgba(30, 41, 59, 0.55)',
              borderColor: 'rgba(148, 163, 184, 0.22)',
              boxShadow: 'none',
              animation: 'none',
              borderRadius: 'var(--radius-sm)'
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.85rem', flex: 1, minWidth: 0 }}>
              {/* Muted Watch Icon */}
              <div
                style={{
                  width: '34px',
                  height: '34px',
                  borderRadius: '50%',
                  background: 'rgba(255, 255, 255, 0.04)',
                  border: '1px solid rgba(148, 163, 184, 0.25)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  flexShrink: 0
                }}
              >
                {isGamma ? (
                  <Eye size={17} color="#94a3b8" />
                ) : isImpulse ? (
                  <Zap size={17} color="#94a3b8" />
                ) : isDryUp ? (
                  <Flame size={17} color="#94a3b8" />
                ) : (
                  <Radio size={17} color="#94a3b8" />
                )}
              </div>

              {/* Alert Content */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.2rem', minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
                  {/* High-visibility NOT A TRADE label */}
                  <span
                    className="mono"
                    style={{
                      fontSize: '0.65rem',
                      fontWeight: 800,
                      letterSpacing: '0.06em',
                      padding: '2px 7px',
                      borderRadius: '3px',
                      background: 'rgba(248, 113, 113, 0.12)',
                      color: '#f87171',
                      border: '1px solid rgba(248, 113, 113, 0.30)',
                      display: 'inline-flex',
                      alignItems: 'center'
                    }}
                  >
                    NOT A TRADE
                  </span>

                  <span
                    className="mono"
                    style={{
                      fontSize: '0.65rem',
                      fontWeight: 700,
                      letterSpacing: '0.04em',
                      padding: '2px 6px',
                      borderRadius: '3px',
                      background: 'rgba(148, 163, 184, 0.12)',
                      color: '#cbd5e1',
                      border: '1px solid rgba(148, 163, 184, 0.22)'
                    }}
                  >
                    WATCH ONLY
                  </span>

                  <span
                    style={{
                      fontSize: '0.68rem',
                      fontWeight: 600,
                      letterSpacing: '0.04em',
                      padding: '2px 6px',
                      borderRadius: '3px',
                      background: 'rgba(255, 255, 255, 0.05)',
                      color: 'var(--text-secondary)',
                      border: '1px solid rgba(255, 255, 255, 0.10)',
                      display: 'flex',
                      alignItems: 'center',
                      gap: '3px'
                    }}
                  >
                    {isCE ? <ArrowUpRight size={11} /> : <ArrowDownRight size={11} />}
                    {alert.instrument} {alert.direction === 'CE' ? 'TOP' : 'BOTTOM'}
                  </span>

                  <span
                    style={{
                      fontSize: '0.8rem',
                      fontWeight: 600,
                      color: 'var(--text-primary)'
                    }}
                  >
                    {alert.title}
                  </span>
                </div>

                <div
                  style={{
                    fontSize: '0.76rem',
                    color: 'var(--text-secondary)',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap'
                  }}
                >
                  {alert.message}
                </div>
              </div>
            </div>

            {/* Auto-Dismiss Notice & Close Button */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem', flexShrink: 0 }}>
              <span
                className="mono"
                style={{
                  fontSize: '0.65rem',
                  color: 'var(--text-muted)'
                }}
              >
                Auto-clears
              </span>

              <button
                onClick={() => onDismiss(alert.id)}
                style={{
                  background: 'transparent',
                  border: 'none',
                  color: 'var(--text-muted)',
                  cursor: 'pointer',
                  padding: '4px',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  borderRadius: '4px',
                  transition: 'color 0.2s ease'
                }}
                onMouseEnter={(e) => (e.currentTarget.style.color = 'var(--text-primary)')}
                onMouseLeave={(e) => (e.currentTarget.style.color = 'var(--text-muted)')}
                title="Dismiss Alert"
              >
                <X size={15} />
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

