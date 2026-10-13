import React, { useState, useEffect, useCallback } from 'react';
import { 
  ShieldAlert, 
  Power, 
  Sliders, 
  CheckCircle2, 
  XCircle, 
  AlertTriangle, 
  Clock, 
  TrendingUp, 
  RefreshCw, 
  Activity, 
  Lock, 
  Unlock, 
  Layers, 
  ExternalLink,
  ChevronDown,
  ChevronUp
} from 'lucide-react';

export function LiveTradeTerminal() {
  const [status, setStatus] = useState(null);
  const [positions, setPositions] = useState([]);
  const [orders, setOrders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [activeSubTab, setActiveSubTab] = useState('positions'); // 'positions' | 'orders' | 'config'
  
  // Kill Switch & Confirmations
  const [isKillModalOpen, setIsKillModalOpen] = useState(false);
  const [isExitingSigId, setIsExitingSigId] = useState(null);
  const [actionError, setActionError] = useState(null);
  const [actionSuccess, setActionSuccess] = useState(null);

  // Runtime Configuration state
  const [config, setConfig] = useState({
    allowed_strategies: ['OI_SQUEEZE_SENTINEL', 'BREAKOUT_ORB', 'VWAP_EMA_ALIGNMENT', 'GAMMA_SCALP', 'MOMENTUM_IMPULSE'],
    max_capital_per_trade: 25000,
    max_daily_loss: 4000,
    nifty_lots: 1,
    sensex_lots: 1
  });
  const [configSaving, setConfigSaving] = useState(false);

  // Fetch status, positions, orders
  const fetchData = useCallback(async () => {
    try {
      const [statusRes, posRes, ordRes] = await Promise.all([
        fetch('http://localhost:8001/api/execution/status').then(r => r.json()).catch(() => null),
        fetch('http://localhost:8001/api/execution/positions').then(r => r.json()).catch(() => []),
        fetch('http://localhost:8001/api/execution/orders').then(r => r.json()).catch(() => [])
      ]);

      if (statusRes) {
        setStatus(statusRes);
        setConfig(prev => ({
          ...prev,
          allowed_strategies: statusRes.allowed_strategies || prev.allowed_strategies,
          max_capital_per_trade: statusRes.max_capital_per_trade || prev.max_capital_per_trade,
          max_daily_loss: statusRes.max_daily_loss || prev.max_daily_loss,
          nifty_lots: statusRes.nifty_lots || prev.nifty_lots,
          sensex_lots: statusRes.sensex_lots || prev.sensex_lots
        }));
      }
      if (Array.isArray(posRes)) setPositions(posRes);
      if (Array.isArray(ordRes)) setOrders(ordRes);
    } catch (err) {
      console.error('Failed to fetch execution state:', err);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 3000);
    return () => clearInterval(interval);
  }, [fetchData]);

  // Master Arm/Disarm Toggle
  const handleToggleArm = async () => {
    if (!status) return;
    const targetArmed = !status.enabled;
    setActionError(null);
    try {
      const res = await fetch(`http://localhost:8001/api/execution/toggle-arm?armed=${targetArmed}`, {
        method: 'POST'
      }).then(r => r.json());
      if (res.status === 'SUCCESS') {
        setStatus(prev => ({ ...prev, enabled: targetArmed }));
        setActionSuccess(`Execution engine ${targetArmed ? 'ARMED' : 'DISARMED'} successfully.`);
        setTimeout(() => setActionSuccess(null), 4000);
      }
    } catch (err) {
      setActionError('Failed to toggle armed state');
    }
  };

  // Master Kill Switch
  const handleKillSwitch = async () => {
    setActionError(null);
    try {
      const res = await fetch('http://localhost:8001/api/execution/kill-switch?active=true', {
        method: 'POST'
      }).then(r => r.json());
      if (res.status === 'SUCCESS') {
        setStatus(prev => ({ ...prev, kill_switch_active: true, enabled: false }));
        setIsKillModalOpen(false);
        setActionSuccess('KILL SWITCH ACTIVATED: All orders cancelled and positions squared off.');
        fetchData();
        setTimeout(() => setActionSuccess(null), 6000);
      }
    } catch (err) {
      setActionError('Kill switch invocation failed');
    }
  };

  // Reset Kill Switch
  const handleResetKillSwitch = async () => {
    try {
      const res = await fetch('http://localhost:8001/api/execution/kill-switch?active=false', {
        method: 'POST'
      }).then(r => r.json());
      if (res.status === 'SUCCESS') {
        setStatus(prev => ({ ...prev, kill_switch_active: false }));
        setActionSuccess('Kill Switch deactivated. System returned to standby.');
        setTimeout(() => setActionSuccess(null), 4000);
      }
    } catch (err) {
      setActionError('Failed to reset kill switch');
    }
  };

  // Individual Position Exit
  const handleExitPosition = async (signalId) => {
    setActionError(null);
    try {
      const res = await fetch(`http://localhost:8001/api/execution/exit-position?signal_id=${signalId}`, {
        method: 'POST'
      }).then(r => r.json());
      if (res.status === 'SUCCESS') {
        setActionSuccess(`Position ${signalId} exited @ Market successfully.`);
        setIsExitingSigId(null);
        fetchData();
        setTimeout(() => setActionSuccess(null), 4000);
      } else {
        setActionError(res.message || 'Exit failed');
      }
    } catch (err) {
      setActionError('Failed to exit position');
    }
  };

  // Save Configuration
  const handleSaveConfig = async () => {
    setConfigSaving(true);
    setActionError(null);
    try {
      const res = await fetch('http://localhost:8001/api/execution/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(config)
      }).then(r => r.json());
      if (res.status === 'SUCCESS') {
        setActionSuccess('Configuration saved and applied to runtime bridge.');
        setTimeout(() => setActionSuccess(null), 4000);
      }
    } catch (err) {
      setActionError('Failed to save config');
    } finally {
      setConfigSaving(false);
    }
  };

  const toggleStrategy = (stratName) => {
    setConfig(prev => {
      const exists = prev.allowed_strategies.includes(stratName);
      const updated = exists 
        ? prev.allowed_strategies.filter(s => s !== stratName)
        : [...prev.allowed_strategies, stratName];
      return { ...prev, allowed_strategies: updated };
    });
  };

  const allAvailableStrategies = [
    { id: 'OI_SQUEEZE_SENTINEL', name: 'OI Squeeze Sentinel', desc: 'Aggressive call/put open interest unwinding & ITM trapped shorts.' },
    { id: 'VWAP_EMA_ALIGNMENT', name: 'VWAP / EMA Alignment', desc: 'Trend day impulse continuation with EMA9/21 pullbacks.' },
    { id: 'BREAKOUT_ORB', name: 'Opening Range Breakout (ORB)', desc: 'Morning session 15-min range breakout with volume surge.' },
    { id: 'GAMMA_SCALP', name: '0-DTE Gamma Scalp', desc: 'High delta acceleration scalp off major Call/Put Walls.' },
    { id: 'MOMENTUM_IMPULSE', name: 'Momentum Impulse Detector', desc: 'High-frequency 5–15s directional spot velocity surge with anti-chase guards.' }
  ];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
      {/* Toast Notification Banner */}
      {actionSuccess && (
        <div style={{
          padding: '0.75rem 1rem',
          borderRadius: 'var(--radius-sm)',
          background: 'rgba(0, 230, 118, 0.1)',
          border: '1px solid var(--ce-green)',
          color: 'var(--ce-green)',
          display: 'flex',
          alignItems: 'center',
          gap: '0.6rem',
          fontSize: '0.85rem'
        }}>
          <CheckCircle2 size={16} />
          <span>{actionSuccess}</span>
        </div>
      )}

      {actionError && (
        <div style={{
          padding: '0.75rem 1rem',
          borderRadius: 'var(--radius-sm)',
          background: 'rgba(255, 51, 102, 0.1)',
          border: '1px solid var(--pe-red)',
          color: 'var(--pe-red)',
          display: 'flex',
          alignItems: 'center',
          gap: '0.6rem',
          fontSize: '0.85rem'
        }}>
          <AlertTriangle size={16} />
          <span>{actionError}</span>
        </div>
      )}

      {/* Top Master Control & Execution Banner */}
      <div className="glass-panel" style={{ padding: '1.25rem 1.5rem' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '1rem' }}>
          
          {/* Left: Mode & Status */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '1.25rem' }}>
            <div style={{
              width: '44px',
              height: '44px',
              borderRadius: 'var(--radius-sm)',
              background: status?.enabled ? 'rgba(0, 230, 118, 0.12)' : 'rgba(255, 170, 0, 0.12)',
              border: `1px solid ${status?.enabled ? 'var(--ce-green)' : 'var(--accent-amber)'}`,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center'
            }}>
              <Activity size={22} color={status?.enabled ? 'var(--ce-green)' : 'var(--accent-amber)'} />
            </div>

            <div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem' }}>
                <h2 style={{ fontSize: '1.2rem', fontWeight: '700', letterSpacing: '-0.02em' }}>
                  Angel One Execution Terminal
                </h2>
                <span style={{
                  padding: '2px 8px',
                  borderRadius: 'var(--radius-full)',
                  fontSize: '0.68rem',
                  fontWeight: '700',
                  letterSpacing: '0.05em',
                  background: status?.mode === 'PAPER' ? 'rgba(0, 240, 255, 0.15)' : 'rgba(255, 51, 102, 0.15)',
                  color: status?.mode === 'PAPER' ? 'var(--accent-cyan)' : 'var(--pe-red)',
                  border: `1px solid ${status?.mode === 'PAPER' ? 'var(--accent-cyan)' : 'var(--pe-red)'}`
                }}>
                  {status?.mode === 'PAPER' ? 'PHASE 1: PAPER / DRY-RUN' : 'PHASE 2: LIVE BROKER'}
                </span>

                {status?.dry_run && (
                  <span style={{
                    padding: '2px 8px',
                    borderRadius: 'var(--radius-full)',
                    fontSize: '0.68rem',
                    background: 'rgba(255, 255, 255, 0.05)',
                    color: 'var(--text-secondary)',
                    border: '1px solid var(--border-subtle)'
                  }}>
                    ZERO FINANCIAL RISK
                  </span>
                )}
              </div>
              <p style={{ fontSize: '0.78rem', color: 'var(--text-muted)', marginTop: '2px' }}>
                SmartAPI Gateway: <strong style={{ color: 'var(--text-secondary)' }}>NFO / BFO Direct</strong> | Slippage buffer: 0.5% | Partial exits: 1R Booked + BE Trailing
              </p>
            </div>
          </div>

          {/* Right: Controls & Kill Switch */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
            
            {/* Refresh Button */}
            <button 
              className="btn-secondary" 
              onClick={() => { setRefreshing(true); fetchData(); }}
              style={{ padding: '8px 12px', fontSize: '0.8rem' }}
              title="Refresh status"
            >
              <RefreshCw size={14} className={refreshing ? 'spin' : ''} />
            </button>

            {/* Arm / Disarm Toggle */}
            <button
              onClick={handleToggleArm}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '0.5rem',
                padding: '8px 16px',
                borderRadius: 'var(--radius-sm)',
                fontWeight: '700',
                fontSize: '0.8rem',
                cursor: 'pointer',
                transition: 'all 0.2s ease',
                background: status?.enabled ? 'rgba(0, 230, 118, 0.15)' : 'rgba(255, 170, 0, 0.15)',
                color: status?.enabled ? 'var(--ce-green)' : 'var(--accent-amber)',
                border: `1px solid ${status?.enabled ? 'var(--ce-green)' : 'var(--accent-amber)'}`
              }}
            >
              {status?.enabled ? <Unlock size={15} /> : <Lock size={15} />}
              <span>{status?.enabled ? 'LIVE ORDERS: ARMED' : 'STANDBY: DISARMED'}</span>
            </button>

            {/* Master Emergency Kill Switch */}
            {status?.kill_switch_active ? (
              <button
                onClick={handleResetKillSwitch}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '0.5rem',
                  padding: '8px 16px',
                  borderRadius: 'var(--radius-sm)',
                  fontWeight: '700',
                  fontSize: '0.8rem',
                  cursor: 'pointer',
                  background: 'rgba(255, 170, 0, 0.2)',
                  color: 'var(--accent-amber)',
                  border: '1px solid var(--accent-amber)'
                }}
              >
                <Power size={15} />
                <span>RESET KILL SWITCH</span>
              </button>
            ) : (
              <button
                onClick={() => setIsKillModalOpen(true)}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '0.5rem',
                  padding: '8px 16px',
                  borderRadius: 'var(--radius-sm)',
                  fontWeight: '700',
                  fontSize: '0.8rem',
                  cursor: 'pointer',
                  background: 'rgba(255, 51, 102, 0.2)',
                  color: 'var(--pe-red)',
                  border: '1px solid var(--pe-red)',
                  boxShadow: '0 0 12px rgba(255, 51, 102, 0.25)'
                }}
              >
                <ShieldAlert size={16} />
                <span>EMERGENCY KILL SWITCH</span>
              </button>
            )}

          </div>
        </div>

        {/* Telemetry Strip */}
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
          gap: '1rem',
          marginTop: '1.25rem',
          paddingTop: '1rem',
          borderTop: '1px solid var(--border-subtle)'
        }}>
          <div>
            <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>ACTIVE POSITIONS</div>
            <div className="mono" style={{ fontSize: '1.2rem', fontWeight: '700', color: positions.length > 0 ? 'var(--accent-cyan)' : 'var(--text-primary)' }}>
              {positions.length}
            </div>
          </div>

          <div>
            <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>SESSION ORDERS PLACED</div>
            <div className="mono" style={{ fontSize: '1.2rem', fontWeight: '700' }}>
              {status?.total_orders_placed || orders.length}
            </div>
          </div>

          <div>
            <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>MAX CAP / TRADE</div>
            <div className="mono" style={{ fontSize: '1.2rem', fontWeight: '700', color: 'var(--text-secondary)' }}>
              ₹{(status?.max_capital_per_trade || 25000).toLocaleString('en-IN')}
            </div>
          </div>

          <div>
            <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>MAX DAILY LOSS LIMIT</div>
            <div className="mono" style={{ fontSize: '1.2rem', fontWeight: '700', color: 'var(--pe-red)' }}>
              -₹{(status?.max_daily_loss || 4000).toLocaleString('en-IN')}
            </div>
          </div>

          <div>
            <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>LOT SIZING</div>
            <div className="mono" style={{ fontSize: '0.9rem', fontWeight: '600', color: 'var(--text-secondary)', marginTop: '4px' }}>
              NIFTY: {config.nifty_lots} lot ({config.nifty_lots * 65}q) | SENSEX: {config.sensex_lots} lot ({config.sensex_lots * 20}q)
            </div>
          </div>
        </div>
      </div>

      {/* Navigation Sub-Tabs */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', borderBottom: '1px solid var(--border-subtle)', paddingBottom: '0.5rem' }}>
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <button
            className={`btn-secondary ${activeSubTab === 'positions' ? 'btn-primary' : ''}`}
            onClick={() => setActiveSubTab('positions')}
            style={{ fontSize: '0.8rem', padding: '6px 14px' }}
          >
            <Activity size={14} />
            <span>Active Broker Positions</span>
            {positions.length > 0 && <span className="badge-count">{positions.length}</span>}
          </button>

          <button
            className={`btn-secondary ${activeSubTab === 'orders' ? 'btn-primary' : ''}`}
            onClick={() => setActiveSubTab('orders')}
            style={{ fontSize: '0.8rem', padding: '6px 14px' }}
          >
            <Layers size={14} />
            <span>Order Audit Log</span>
            {orders.length > 0 && <span className="badge-count">{orders.length}</span>}
          </button>

          <button
            className={`btn-secondary ${activeSubTab === 'config' ? 'btn-primary' : ''}`}
            onClick={() => setActiveSubTab('config')}
            style={{ fontSize: '0.8rem', padding: '6px 14px' }}
          >
            <Sliders size={14} />
            <span>Rules & Sizing Config</span>
          </button>
        </div>
      </div>

      {/* SUB-TAB 1: ACTIVE POSITIONS */}
      {activeSubTab === 'positions' && (
        <div>
          {positions.length === 0 ? (
            <div className="glass-panel" style={{ padding: '3.5rem 2rem', textAlign: 'center' }}>
              <div style={{
                width: '48px',
                height: '48px',
                borderRadius: '50%',
                background: 'rgba(0, 240, 255, 0.05)',
                border: '1px solid rgba(0, 240, 255, 0.2)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                margin: '0 auto 1rem'
              }}>
                <Activity size={22} color="var(--accent-cyan)" />
              </div>
              <h3 style={{ fontSize: '1.05rem', fontWeight: '600' }}>No Active Broker Positions</h3>
              <p style={{ maxWidth: '460px', margin: '0.4rem auto 0', fontSize: '0.78rem', color: 'var(--text-muted)' }}>
                When strategies generate signals and the bridge is armed, simulated paper orders will appear here with live tracking and 1-click square-off.
              </p>
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
              {positions.map((pos) => {
                const fillPrice = pos.fill_price || pos.entry_price || 0;
                const currentPrice = pos.current_ltp || fillPrice;
                const remQty = pos.remaining_quantity || pos.quantity || 0;
                const unrlPnl = ((currentPrice - fillPrice) * remQty);
                const isProfitable = unrlPnl >= 0;

                return (
                  <div 
                    key={pos.signal_id || pos.order_id} 
                    className="glass-panel"
                    style={{
                      padding: '1.25rem',
                      borderLeft: `4px solid ${pos.direction === 'CE' ? 'var(--ce-green)' : 'var(--pe-red)'}`
                    }}
                  >
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: '1rem' }}>
                      {/* Left: Info */}
                      <div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem' }}>
                          <span style={{
                            padding: '2px 8px',
                            borderRadius: 'var(--radius-sm)',
                            fontWeight: '700',
                            fontSize: '0.75rem',
                            background: pos.direction === 'CE' ? 'var(--ce-green-bg)' : 'var(--pe-red-bg)',
                            color: pos.direction === 'CE' ? 'var(--ce-green)' : 'var(--pe-red)',
                            border: `1px solid ${pos.direction === 'CE' ? 'var(--ce-green)' : 'var(--pe-red)'}`
                          }}>
                            {pos.instrument} {pos.strike} {pos.direction}
                          </span>

                          <span style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                            {pos.option_symbol || pos.tradingsymbol}
                          </span>

                          <span style={{
                            padding: '2px 6px',
                            borderRadius: 'var(--radius-sm)',
                            background: 'rgba(255, 255, 255, 0.05)',
                            fontSize: '0.7rem',
                            color: 'var(--text-secondary)'
                          }}>
                            {pos.strategy || 'QUANT_STRATEGY'}
                          </span>
                        </div>

                        {/* Price Details */}
                        <div style={{ display: 'flex', gap: '1.5rem', marginTop: '0.75rem', fontSize: '0.8rem' }}>
                          <div>
                            <span style={{ color: 'var(--text-muted)' }}>Fill Price: </span>
                            <span className="mono" style={{ fontWeight: '600' }}>₹{fillPrice.toFixed(2)}</span>
                          </div>
                          <div>
                            <span style={{ color: 'var(--text-muted)' }}>Current LTP: </span>
                            <span className="mono" style={{ fontWeight: '600' }}>₹{currentPrice.toFixed(2)}</span>
                          </div>
                          <div>
                            <span style={{ color: 'var(--text-muted)' }}>Qty: </span>
                            <span className="mono" style={{ fontWeight: '600' }}>{remQty}</span>
                          </div>
                          <div>
                            <span style={{ color: 'var(--text-muted)' }}>Target 1: </span>
                            <span className="mono" style={{ color: 'var(--ce-green)', fontWeight: '600' }}>
                              ₹{pos.target_1r || '-'} {pos.target_1_hit ? '(HIT 🎯)' : ''}
                            </span>
                          </div>
                          <div>
                            <span style={{ color: 'var(--text-muted)' }}>Stop Loss: </span>
                            <span className="mono" style={{ color: 'var(--pe-red)', fontWeight: '600' }}>
                              ₹{pos.breakeven_sl || pos.stop_loss || '-'}
                            </span>
                          </div>
                        </div>
                      </div>

                      {/* Right: P&L & Individual Exit Button */}
                      <div style={{ display: 'flex', alignItems: 'center', gap: '1.25rem' }}>
                        <div style={{ textAlign: 'right' }}>
                          <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>UNREALIZED P&L</div>
                          <div 
                            className="mono" 
                            style={{ 
                              fontSize: '1.25rem', 
                              fontWeight: '700',
                              color: isProfitable ? 'var(--ce-green)' : 'var(--pe-red)'
                            }}
                          >
                            {isProfitable ? '+' : ''}₹{unrlPnl.toFixed(2)}
                          </div>
                        </div>

                        {/* Individual Exit Button */}
                        {isExitingSigId === pos.signal_id ? (
                          <div style={{ display: 'flex', gap: '0.4rem' }}>
                            <button
                              onClick={() => handleExitPosition(pos.signal_id)}
                              style={{
                                padding: '6px 12px',
                                background: 'var(--pe-red)',
                                color: '#fff',
                                border: 'none',
                                borderRadius: 'var(--radius-sm)',
                                fontSize: '0.75rem',
                                fontWeight: '700',
                                cursor: 'pointer'
                              }}
                            >
                              CONFIRM EXIT
                            </button>
                            <button
                              onClick={() => setIsExitingSigId(null)}
                              className="btn-secondary"
                              style={{ padding: '6px 10px', fontSize: '0.75rem' }}
                            >
                              Cancel
                            </button>
                          </div>
                        ) : (
                          <button
                            onClick={() => setIsExitingSigId(pos.signal_id)}
                            style={{
                              padding: '8px 14px',
                              borderRadius: 'var(--radius-sm)',
                              background: 'rgba(255, 51, 102, 0.1)',
                              border: '1px solid var(--pe-red)',
                              color: 'var(--pe-red)',
                              fontSize: '0.78rem',
                              fontWeight: '700',
                              cursor: 'pointer',
                              display: 'flex',
                              alignItems: 'center',
                              gap: '0.4rem'
                            }}
                          >
                            <XCircle size={14} />
                            <span>EXIT POSITION</span>
                          </button>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* SUB-TAB 2: ORDERS AUDIT LOG */}
      {activeSubTab === 'orders' && (
        <div className="glass-panel" style={{ padding: '1rem', overflowX: 'auto' }}>
          {orders.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '2.5rem', color: 'var(--text-muted)', fontSize: '0.85rem' }}>
              No orders placed in current session yet.
            </div>
          ) : (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.78rem' }}>
              <thead>
                <tr style={{ borderBottom: '1px solid var(--border-subtle)', color: 'var(--text-muted)', textAlign: 'left' }}>
                  <th style={{ padding: '8px' }}>Order ID</th>
                  <th style={{ padding: '8px' }}>Time</th>
                  <th style={{ padding: '8px' }}>Symbol</th>
                  <th style={{ padding: '8px' }}>Type</th>
                  <th style={{ padding: '8px' }}>Qty</th>
                  <th style={{ padding: '8px' }}>Fill Price</th>
                  <th style={{ padding: '8px' }}>Status</th>
                  <th style={{ padding: '8px' }}>Realized P&L</th>
                </tr>
              </thead>
              <tbody>
                {orders.map((ord, idx) => (
                  <tr key={ord.order_id || idx} style={{ borderBottom: '1px solid rgba(255,255,255,0.03)' }}>
                    <td className="mono" style={{ padding: '8px', color: 'var(--accent-cyan)' }}>{ord.order_id}</td>
                    <td style={{ padding: '8px', color: 'var(--text-muted)' }}>
                      {ord.timestamp ? new Date(ord.timestamp).toLocaleTimeString() : '-'}
                    </td>
                    <td style={{ padding: '8px', fontWeight: '600' }}>{ord.symbol || ord.option_symbol || '-'}</td>
                    <td style={{ padding: '8px', color: ord.direction === 'CE' ? 'var(--ce-green)' : 'var(--pe-red)' }}>
                      {ord.action || ord.transaction_type || 'BUY'}
                    </td>
                    <td className="mono" style={{ padding: '8px' }}>{ord.quantity || ord.quantity_sold || '-'}</td>
                    <td className="mono" style={{ padding: '8px' }}>₹{ord.fill_price || ord.exit_price || '-'}</td>
                    <td style={{ padding: '8px' }}>
                      <span style={{
                        padding: '2px 6px',
                        borderRadius: 'var(--radius-sm)',
                        fontSize: '0.7rem',
                        background: ord.status === 'SIMULATED_FILLED' ? 'rgba(0, 230, 118, 0.1)' : 'rgba(255, 255, 255, 0.05)',
                        color: ord.status === 'SIMULATED_FILLED' ? 'var(--ce-green)' : 'var(--text-secondary)'
                      }}>
                        {ord.status || ord.action || 'COMPLETED'}
                      </span>
                    </td>
                    <td className="mono" style={{ padding: '8px', fontWeight: '600' }}>
                      {ord.realized_pnl !== undefined 
                        ? (ord.realized_pnl >= 0 ? `+₹${ord.realized_pnl}` : `-₹${Math.abs(ord.realized_pnl)}`)
                        : '-'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* SUB-TAB 3: CONFIGURATION */}
      {activeSubTab === 'config' && (
        <div className="glass-panel" style={{ padding: '1.5rem', display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
          <div>
            <h3 style={{ fontSize: '1rem', fontWeight: '700' }}>Allowed Strategies</h3>
            <p style={{ fontSize: '0.78rem', color: 'var(--text-muted)', marginTop: '2px' }}>
              Only signals generated by checked strategies will be routed to the Angel One execution bridge.
            </p>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: '1rem', marginTop: '1rem' }}>
              {allAvailableStrategies.map((strat) => {
                const isSelected = config.allowed_strategies.includes(strat.id);
                return (
                  <div
                    key={strat.id}
                    onClick={() => toggleStrategy(strat.id)}
                    style={{
                      padding: '1rem',
                      borderRadius: 'var(--radius-sm)',
                      background: isSelected ? 'rgba(0, 240, 255, 0.05)' : 'rgba(255, 255, 255, 0.02)',
                      border: `1px solid ${isSelected ? 'var(--accent-cyan)' : 'var(--border-subtle)'}`,
                      cursor: 'pointer',
                      transition: 'all 0.15s ease'
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                      <span style={{ fontWeight: '600', fontSize: '0.85rem', color: isSelected ? 'var(--accent-cyan)' : 'var(--text-primary)' }}>
                        {strat.name}
                      </span>
                      <input 
                        type="checkbox" 
                        checked={isSelected} 
                        onChange={() => {}} // handled by parent div onClick
                        style={{ accentColor: 'var(--accent-cyan)' }}
                      />
                    </div>
                    <p style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginTop: '0.4rem' }}>
                      {strat.desc}
                    </p>
                  </div>
                );
              })}
            </div>
          </div>

          <div style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: '1.25rem' }}>
            <h3 style={{ fontSize: '1rem', fontWeight: '700' }}>Lot Sizing & Capital Allocation</h3>
            
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '1rem', marginTop: '1rem' }}>
              <div>
                <label style={{ fontSize: '0.75rem', color: 'var(--text-muted)', display: 'block', marginBottom: '4px' }}>
                  NIFTY Lots (65 qty / lot)
                </label>
                <input
                  type="number"
                  min="1"
                  max="10"
                  value={config.nifty_lots}
                  onChange={(e) => setConfig({ ...config, nifty_lots: parseInt(e.target.value) || 1 })}
                  style={{
                    width: '100%',
                    padding: '8px 12px',
                    borderRadius: 'var(--radius-sm)',
                    background: 'var(--bg-secondary)',
                    border: '1px solid var(--border-subtle)',
                    color: '#fff',
                    fontFamily: 'var(--font-mono)'
                  }}
                />
              </div>

              <div>
                <label style={{ fontSize: '0.75rem', color: 'var(--text-muted)', display: 'block', marginBottom: '4px' }}>
                  SENSEX Lots (20 qty / lot)
                </label>
                <input
                  type="number"
                  min="1"
                  max="10"
                  value={config.sensex_lots}
                  onChange={(e) => setConfig({ ...config, sensex_lots: parseInt(e.target.value) || 1 })}
                  style={{
                    width: '100%',
                    padding: '8px 12px',
                    borderRadius: 'var(--radius-sm)',
                    background: 'var(--bg-secondary)',
                    border: '1px solid var(--border-subtle)',
                    color: '#fff',
                    fontFamily: 'var(--font-mono)'
                  }}
                />
              </div>

              <div>
                <label style={{ fontSize: '0.75rem', color: 'var(--text-muted)', display: 'block', marginBottom: '4px' }}>
                  Max Capital Per Trade (₹)
                </label>
                <input
                  type="number"
                  step="5000"
                  value={config.max_capital_per_trade}
                  onChange={(e) => setConfig({ ...config, max_capital_per_trade: parseFloat(e.target.value) || 25000 })}
                  style={{
                    width: '100%',
                    padding: '8px 12px',
                    borderRadius: 'var(--radius-sm)',
                    background: 'var(--bg-secondary)',
                    border: '1px solid var(--border-subtle)',
                    color: '#fff',
                    fontFamily: 'var(--font-mono)'
                  }}
                />
              </div>

              <div>
                <label style={{ fontSize: '0.75rem', color: 'var(--text-muted)', display: 'block', marginBottom: '4px' }}>
                  Max Daily Loss Circuit (₹)
                </label>
                <input
                  type="number"
                  step="1000"
                  value={config.max_daily_loss}
                  onChange={(e) => setConfig({ ...config, max_daily_loss: parseFloat(e.target.value) || 4000 })}
                  style={{
                    width: '100%',
                    padding: '8px 12px',
                    borderRadius: 'var(--radius-sm)',
                    background: 'var(--bg-secondary)',
                    border: '1px solid var(--border-subtle)',
                    color: '#fff',
                    fontFamily: 'var(--font-mono)'
                  }}
                />
              </div>
            </div>

            <button
              className="btn-primary"
              onClick={handleSaveConfig}
              disabled={configSaving}
              style={{ marginTop: '1.25rem', padding: '8px 20px', fontSize: '0.85rem' }}
            >
              {configSaving ? 'Saving...' : 'Save Configuration'}
            </button>
          </div>
        </div>
      )}

      {/* KILL SWITCH CONFIRMATION MODAL */}
      {isKillModalOpen && (
        <div style={{
          position: 'fixed',
          inset: 0,
          background: 'rgba(0, 0, 0, 0.8)',
          backdropFilter: 'blur(8px)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          zIndex: 1000,
          padding: '1rem'
        }}>
          <div className="glass-panel" style={{
            maxWidth: '460px',
            width: '100%',
            padding: '1.75rem',
            border: '1px solid var(--pe-red)',
            boxShadow: '0 0 32px rgba(255, 51, 102, 0.4)'
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', color: 'var(--pe-red)' }}>
              <ShieldAlert size={28} />
              <h3 style={{ fontSize: '1.2rem', fontWeight: '700' }}>Confirm Emergency Kill Switch</h3>
            </div>

            <p style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', marginTop: '0.75rem', lineHeight: '1.5' }}>
              Activating the Kill Switch will:
            </p>

            <ul style={{ fontSize: '0.8rem', color: 'var(--text-muted)', margin: '0.5rem 0 1.25rem 1.25rem', lineHeight: '1.6' }}>
              <li><strong>Disarm</strong> all automated execution immediately.</li>
              <li>Fire <strong>MARKET square-off orders</strong> for all {positions.length} active positions.</li>
              <li>Cancel all pending SL and Target limit orders on the exchange.</li>
              <li>Lock the bridge until manually reset.</li>
            </ul>

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '0.75rem' }}>
              <button
                className="btn-secondary"
                onClick={() => setIsKillModalOpen(false)}
                style={{ padding: '8px 16px' }}
              >
                Cancel
              </button>
              <button
                onClick={handleKillSwitch}
                style={{
                  padding: '8px 20px',
                  borderRadius: 'var(--radius-sm)',
                  background: 'var(--pe-red)',
                  color: '#fff',
                  border: 'none',
                  fontWeight: '700',
                  fontSize: '0.85rem',
                  cursor: 'pointer'
                }}
              >
                EXECUTE KILL SWITCH
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
