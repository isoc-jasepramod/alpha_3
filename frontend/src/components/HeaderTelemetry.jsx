import React from 'react';
import { Activity, ShieldAlert, Volume2, VolumeX, PlayCircle, Layers } from 'lucide-react';

export function HeaderTelemetry({
  telemetry,
  spotData = {},
  regimes = {},
  connected,
  soundEnabled,
  onToggleSound,
  onOpenReplay
}) {
  const pnl = telemetry.realized_pnl || 0.0;
  const pnlPct = telemetry.realized_pnl_pct || 0.0;
  const isPos = pnl >= 0;
  const isTripped = telemetry.circuit_breaker_tripped;

  // Circuit breaker math (-2.5% max limit)
  const maxLossPct = 2.5;
  const currentDrawdownPct = pnl < 0 ? Math.min(Math.abs(pnlPct), maxLossPct) : 0;
  const drawdownFillPct = (currentDrawdownPct / maxLossPct) * 100;

  // Spot Data resolution (accurate ATM & live LTP)
  const niftyData = spotData.NIFTY || {};
  const sensexData = spotData.SENSEX || {};

  const niftyAtm = niftyData.atm || telemetry?.current_atm?.NIFTY || 23350;
  const niftyLtp = niftyData.ltp || 23346.40;
  const niftyChange = niftyData.change ?? 0.0;
  const niftyChangePct = niftyData.change_pct ?? 0.0;

  const sensexAtm = sensexData.atm || telemetry?.current_atm?.SENSEX || 74300;
  const sensexLtp = sensexData.ltp || 74294.96;
  const sensexChange = sensexData.change ?? 0.0;
  const sensexChangePct = sensexData.change_pct ?? 0.0;

  // Session Regime Data
  const niftyRegime = regimes?.NIFTY || telemetry?.regimes?.NIFTY;
  const sensexRegime = regimes?.SENSEX || telemetry?.regimes?.SENSEX;

  const renderRegimePill = (regimeInfo) => {
    if (!regimeInfo || !regimeInfo.regime) return null;
    const regime = regimeInfo.regime;
    const score = Math.round(regimeInfo.score || 50);

    let text = `⚖️ NEUTRAL ${score}`;
    let bg = 'rgba(255, 179, 0, 0.12)';
    let color = '#ffb300';
    let border = '1px solid rgba(255, 179, 0, 0.35)';

    if (regime === 'TRENDING_BULL') {
      text = `🔺 TREND UP ${score}`;
      bg = 'rgba(0, 230, 118, 0.15)';
      color = '#00e676';
      border = '1px solid rgba(0, 230, 118, 0.40)';
    } else if (regime === 'TRENDING_BEAR') {
      text = `🔻 TREND DOWN ${score}`;
      bg = 'rgba(255, 51, 102, 0.15)';
      color = '#ff3366';
      border = '1px solid rgba(255, 51, 102, 0.40)';
    } else if (regime === 'CHOPPY') {
      text = `〰️ CHOPPY ${score}`;
      bg = 'rgba(179, 136, 255, 0.12)';
      color = '#b388ff';
      border = '1px solid rgba(179, 136, 255, 0.35)';
    }

    return (
      <span
        className="mono"
        style={{
          fontSize: '0.62rem',
          fontWeight: 700,
          letterSpacing: '0.04em',
          padding: '2px 6px',
          borderRadius: '4px',
          background: bg,
          color: color,
          border: border,
          display: 'inline-flex',
          alignItems: 'center',
          gap: '3px'
        }}
        title={`Session Regime: ${regime} (Score: ${score}/100)`}
      >
        {text}
      </span>
    );
  };

  return (
    <header className="glass-panel telemetry-bar">
      <div className="telemetry-left">
        <div className="brand-title">
          <span>PROJECT ALPHA</span>
          <span className="version-pill">2.0 PRO</span>
        </div>

        <div className="status-badge">
          <span className={`status-dot ${connected ? 'online' : 'offline'}`}></span>
          <span>{connected ? 'LIVE 250ms WS' : 'CONNECTING...'}</span>
        </div>

        {isTripped && (
          <div className="status-badge" style={{ borderColor: 'var(--pe-red)', background: 'var(--pe-red-bg)', color: 'var(--pe-red)' }}>
            <ShieldAlert size={14} />
            <span>CIRCUIT BREAKER ACTIVE (-2.5%)</span>
          </div>
        )}
      </div>

      {/* Live Spot Index Ticker Widgets */}
      <div className="spot-ticker-group">
        {/* NIFTY 50 */}
        <div className="spot-card" title={`NIFTY 50 Spot | High: ${niftyData.high || niftyLtp} | Low: ${niftyData.low || niftyLtp}`}>
          <div className="spot-header">
            <span className="spot-name">NIFTY 50</span>
            <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
              {renderRegimePill(niftyRegime)}
              <span className="spot-atm-tag mono">ATM {niftyAtm}</span>
            </div>
          </div>
          <div className="spot-body">
            <span className={`spot-ltp mono ${niftyChange >= 0 ? 'price-up' : 'price-down'}`}>
              ₹{Number(niftyLtp).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
            </span>
            <span className={`spot-change mono ${niftyChange >= 0 ? 'pos' : 'neg'}`}>
              {niftyChange >= 0 ? '+' : ''}{niftyChange.toFixed(2)} ({niftyChange >= 0 ? '+' : ''}{niftyChangePct.toFixed(2)}%)
            </span>
          </div>
        </div>

        {/* SENSEX */}
        <div className="spot-card" title={`SENSEX Spot | High: ${sensexData.high || sensexLtp} | Low: ${sensexData.low || sensexLtp}`}>
          <div className="spot-header">
            <span className="spot-name">SENSEX</span>
            <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
              {renderRegimePill(sensexRegime)}
              <span className="spot-atm-tag mono">ATM {sensexAtm}</span>
            </div>
          </div>
          <div className="spot-body">
            <span className={`spot-ltp mono ${sensexChange >= 0 ? 'price-up' : 'price-down'}`}>
              ₹{Number(sensexLtp).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
            </span>
            <span className={`spot-change mono ${sensexChange >= 0 ? 'pos' : 'neg'}`}>
              {sensexChange >= 0 ? '+' : ''}{sensexChange.toFixed(2)} ({sensexChange >= 0 ? '+' : ''}{sensexChangePct.toFixed(2)}%)
            </span>
          </div>
        </div>
      </div>

      <div className="telemetry-stats">
        {/* Equity */}
        <div className="stat-item">
          <span className="stat-label">ACCOUNT EQUITY</span>
          <span className="stat-value mono">
            ₹{Number(telemetry.total_equity || 100000).toLocaleString('en-IN')}
          </span>
        </div>

        {/* Realized Daily PnL */}
        <div className="stat-item">
          <span className="stat-label">THEORETICAL PNL</span>
          <span className={`stat-value mono ${isPos ? 'pnl-pos' : 'pnl-neg'}`}>
            {isPos ? '+' : ''}₹{pnl.toFixed(2)} ({isPos ? '+' : ''}{pnlPct.toFixed(2)}%)
          </span>
        </div>

        {/* Circuit Breaker Meter */}
        <div className="cb-meter-container">
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.65rem' }}>
            <span style={{ color: 'var(--text-muted)' }}>DRAWDOWN CAP</span>
            <span className="mono" style={{ color: drawdownFillPct > 70 ? 'var(--pe-red)' : 'var(--text-secondary)' }}>
              {currentDrawdownPct.toFixed(1)}% / 2.5%
            </span>
          </div>
          <div className="cb-meter-bar">
            <div
              className="cb-meter-fill"
              style={{
                width: `${drawdownFillPct}%`,
                background: drawdownFillPct > 70 ? 'var(--pe-red)' : 'var(--accent-amber)'
              }}
            ></div>
          </div>
        </div>

        {/* Action Controls */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
          <button className="btn-secondary" onClick={onOpenReplay} title="Open Market Replay Simulator">
            <PlayCircle size={14} color="var(--accent-cyan)" />
            <span>Lab Replay</span>
          </button>

          <button
            className="btn-secondary"
            onClick={onToggleSound}
            style={{ padding: '6px 9px' }}
            title={soundEnabled ? 'Mute Audio Alerts' : 'Unmute Audio Alerts'}
          >
            {soundEnabled ? <Volume2 size={15} color="var(--ce-green)" /> : <VolumeX size={15} color="var(--text-muted)" />}
          </button>
        </div>
      </div>
    </header>
  );
}
