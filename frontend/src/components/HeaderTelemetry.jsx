import React from 'react';
import { Activity, ShieldAlert, Volume2, VolumeX, PlayCircle, Layers } from 'lucide-react';

export function HeaderTelemetry({
  telemetry,
  spotData = {},
  gammaData = {},
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

  // Gamma Exposure Resolution
  const niftyGamma = niftyData.gamma || gammaData?.NIFTY || {};
  const niftyNetGex = niftyGamma.net_gex ?? 0.0;
  const niftyGammaRegime = niftyGamma.regime || (niftyNetGex > 5 ? 'POSITIVE' : niftyNetGex < -5 ? 'NEGATIVE' : 'NEUTRAL');
  const niftyCallWall = niftyGamma.call_wall || 0;
  const niftyPutWall = niftyGamma.put_wall || 0;
  const niftyGammaBadge = niftyGammaRegime === 'POSITIVE'
    ? `+Γ DAMPENER`
    : niftyGammaRegime === 'NEGATIVE'
    ? `-Γ AMPLIFIER`
    : `Γ NEUTRAL`;
  const niftyGammaTitle = `NIFTY Gamma: ${niftyGammaRegime} (${niftyNetGex > 0 ? '+' : ''}${niftyNetGex} Cr) | Call Wall: ${niftyCallWall || 'N/A'} | Put Wall: ${niftyPutWall || 'N/A'}`;

  const sensexGamma = sensexData.gamma || gammaData?.SENSEX || {};
  const sensexNetGex = sensexGamma.net_gex ?? 0.0;
  const sensexGammaRegime = sensexGamma.regime || (sensexNetGex > 5 ? 'POSITIVE' : sensexNetGex < -5 ? 'NEGATIVE' : 'NEUTRAL');
  const sensexCallWall = sensexGamma.call_wall || 0;
  const sensexPutWall = sensexGamma.put_wall || 0;
  const sensexGammaBadge = sensexGammaRegime === 'POSITIVE'
    ? `+Γ DAMPENER`
    : sensexGammaRegime === 'NEGATIVE'
    ? `-Γ AMPLIFIER`
    : `Γ NEUTRAL`;
  const sensexGammaTitle = `SENSEX Gamma: ${sensexGammaRegime} (${sensexNetGex > 0 ? '+' : ''}${sensexNetGex} Cr) | Call Wall: ${sensexCallWall || 'N/A'} | Put Wall: ${sensexPutWall || 'N/A'}`;

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
            <div className="spot-header-tags">
              <span className="spot-atm-tag mono">ATM {niftyAtm}</span>
              <span
                className={`spot-gamma-tag mono ${niftyGammaRegime === 'POSITIVE' ? 'gamma-pos' : niftyGammaRegime === 'NEGATIVE' ? 'gamma-neg' : 'gamma-neutral'}`}
                title={niftyGammaTitle}
              >
                {niftyGammaBadge}
              </span>
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
            <div className="spot-header-tags">
              <span className="spot-atm-tag mono">ATM {sensexAtm}</span>
              <span
                className={`spot-gamma-tag mono ${sensexGammaRegime === 'POSITIVE' ? 'gamma-pos' : sensexGammaRegime === 'NEGATIVE' ? 'gamma-neg' : 'gamma-neutral'}`}
                title={sensexGammaTitle}
              >
                {sensexGammaBadge}
              </span>
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
