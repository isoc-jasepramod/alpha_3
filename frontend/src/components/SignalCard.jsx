import React, { useState, useEffect } from 'react';
import { Timer, ArrowUpRight, ArrowDownRight, AlertTriangle, CheckCircle2, XCircle, ShieldCheck } from 'lucide-react';

export function SignalCard({ signal }) {
  const {
    signal_id,
    instrument,
    strategy,
    direction,
    strike,
    option_type,
    option_symbol,
    spot_entry,
    entry_price,
    stop_loss,
    target,
    lot_size,
    quantity,
    risk_amount,
    created_at,
    status,
    live_ltp,
    confidence,
    target_1r,
    target_2r,
    partial_exit_guidance,
    deviation_pct: backendDev
  } = signal;

  // Local upward ticking timer in seconds
  const [elapsedSec, setElapsedSec] = useState(0);

  useEffect(() => {
    // Calculate initial elapsed time from created_at
    const startTs = new Date(created_at).getTime();
    const updateTimer = () => {
      const now = Date.now();
      const diffSec = Math.max(0, Math.floor((now - startTs) / 1000));
      setElapsedSec(diffSec);
    };

    updateTimer();
    const interval = setInterval(updateTimer, 1000);
    return () => clearInterval(interval);
  }, [created_at]);

  const ltp = live_ltp || entry_price;
  const devPct = backendDev !== undefined ? backendDev : (((ltp - entry_price) / entry_price) * 100).toFixed(2);
  const isPos = devPct >= 0;

  // Runaway guardrail logic: status managed by backend signal tracker
  const isChasePrevented = status === 'INVALID_CHASE_PREVENTED';
  const isTargetHit = status === 'TARGET_HIT';
  const isStopHit = status === 'STOP_HIT';
  const isResolved = isChasePrevented || isTargetHit || isStopHit;


  const targetGain = ((target - entry_price) * quantity).toFixed(0);
  const stopLossAmount = risk_amount || ((entry_price - stop_loss) * quantity).toFixed(0);

  return (
    <div className={`signal-card ${direction === 'CE' ? 'ce-border' : 'pe-border'}`}>
      {/* Non-blocking Outcome Status Banner */}
      {isTargetHit && (
        <div className="outcome-banner banner-target">
          <CheckCircle2 size={16} color="var(--ce-green)" />
          <span><strong>TARGET REACHED (₹{target})</strong> | Gain: +₹{targetGain}</span>
        </div>
      )}

      {isStopHit && (
        <div className="outcome-banner banner-stop">
          <XCircle size={16} color="var(--pe-red)" />
          <span><strong>STOP REACHED (₹{stop_loss})</strong> | Risk: -₹{stopLossAmount}</span>
        </div>
      )}

      {isChasePrevented && (
        <div className="outcome-banner banner-chase">
          <AlertTriangle size={16} color="var(--accent-amber)" />
          <span><strong>CHASE PREVENTED</strong> | Surged +{devPct}% within {elapsedSec}s</span>
        </div>
      )}

      {/* Card Header */}
      <div className="card-top">
        <div>
          <div className="symbol-badge-group" style={{ marginBottom: '0.35rem' }}>
            <span className={`dir-badge ${direction === 'CE' ? 'ce' : 'pe'}`}>
              {instrument} {direction}
            </span>
            <span className="strategy-tag">{strategy.replace('_', ' ')}</span>
            {confidence && (
              <span className="strategy-tag" style={{ background: 'rgba(0, 240, 255, 0.15)', color: '#00f0ff', borderColor: 'rgba(0, 240, 255, 0.3)' }}>
                {confidence}% CONF
              </span>
            )}
          </div>
          <h3 className="strike-title mono">
            {strike} {option_type}
          </h3>
        </div>

        {/* Live Timer & Deviation */}
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '4px' }}>
          <div className="timer-pill running mono">
            <Timer size={12} />
            <span>{elapsedSec}s</span>
          </div>
          <span
            className="mono"
            style={{
              fontSize: '0.75rem',
              fontWeight: '700',
              color: isPos ? 'var(--ce-green)' : 'var(--pe-red)'
            }}
          >
            {isPos ? '+' : ''}{devPct}%
          </span>
        </div>
      </div>

      {/* Pricing Grid */}
      <div className="price-strip mono">
        <div className="price-item">
          <span className="price-item-label">Entry Reference</span>
          <span className="price-item-val" style={{ color: 'var(--text-secondary)' }}>
            ₹{entry_price.toFixed(2)}
          </span>
        </div>
        <div className="price-item">
          <span className="price-item-label">Live Premium</span>
          <span
            className="price-item-val"
            style={{
              color: isPos ? 'var(--ce-green)' : 'var(--pe-red)',
              textShadow: isPos ? '0 0 10px var(--ce-green-glow)' : '0 0 10px var(--pe-red-glow)'
            }}
          >
            ₹{Number(ltp).toFixed(2)}
          </span>
        </div>
      </div>

      {/* SL and TGT Grid */}
      <div className="target-sl-grid mono">
        <div className="sl-box">
          <span className="price-item-label" style={{ color: 'var(--pe-red)' }}>Stop Loss (SL)</span>
          <span style={{ fontSize: '1rem', fontWeight: '700', color: '#ff6688' }}>
            ₹{stop_loss.toFixed(2)}
          </span>
          <span style={{ fontSize: '0.65rem', color: 'var(--text-muted)' }}>
            Risk: -₹{stopLossAmount}
          </span>
        </div>

        <div className="tgt-box">
          <span className="price-item-label" style={{ color: 'var(--ce-green)' }}>
            {target_1r ? 'Target (T1 / T2)' : 'Target (1:2 RR)'}
          </span>
          <span style={{ fontSize: target_1r ? '0.9rem' : '1rem', fontWeight: '700', color: '#33ff99' }}>
            {target_1r ? `₹${target_1r.toFixed(1)} / ₹${target.toFixed(1)}` : `₹${target.toFixed(2)}`}
          </span>
          <span style={{ fontSize: '0.65rem', color: 'var(--text-muted)' }}>
            {target_1r ? '50% at T1 (+1R) | Trail to T2' : `Reward: +₹${targetGain}`}
          </span>
        </div>
      </div>

      {/* Sizing & Spot reference bar */}
      <div className="risk-qty-bar mono">
        <span>Qty: <strong>{quantity}</strong> ({quantity / lot_size} lots)</span>
        <span>Spot Entry: <strong>{spot_entry.toFixed(1)}</strong></span>
      </div>
    </div>
  );
}
