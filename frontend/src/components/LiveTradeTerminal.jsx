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
  ChevronUp,
  Calendar,
  ArrowRight
} from 'lucide-react';

export function LiveTradeTerminal({ activeSignals = [] }) {
  const [status, setStatus] = useState(null);
  const [positions, setPositions] = useState([]);
  const [orders, setOrders] = useState([]);
  const [dailySummaries, setDailySummaries] = useState([]);
  const [selectedOrderDate, setSelectedOrderDate] = useState('');
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [activeSubTab, setActiveSubTab] = useState('positions'); // 'positions' | 'orders' | 'daywise' | 'config'
  
  // Kill Switch & Confirmations
  const [isKillModalOpen, setIsKillModalOpen] = useState(false);
  const [isExitingSigId, setIsExitingSigId] = useState(null);
  const [actionError, setActionError] = useState(null);
  const [actionSuccess, setActionSuccess] = useState(null);

  // Runtime Configuration state
  const [config, setConfig] = useState({
    allowed_strategies: ['OI_SQUEEZE', 'ORB_BREAKOUT', 'BREAKOUT_ORB', 'VWAP_EMA', 'SIMPLIFIED_VWAP_EMA', 'GAMMA_SCALP', 'MOMENTUM_IMPULSE'],
    max_capital_per_trade: 25000,
    max_daily_loss: 4000,
    nifty_lots: 1,
    sensex_lots: 1
  });
  const [configSaving, setConfigSaving] = useState(false);
  const [isConfigDirty, setIsConfigDirty] = useState(false);

  // Load initial config from backend ONCE on mount
  useEffect(() => {
    let isMounted = true;
    fetch('http://localhost:8001/api/execution/config')
      .then(r => r.json())
      .then(cfg => {
        if (!isMounted || !cfg) return;
        setConfig(prev => ({
          ...prev,
          allowed_strategies: cfg.allowed_strategies || prev.allowed_strategies,
          max_capital_per_trade: cfg.max_capital_per_trade || prev.max_capital_per_trade,
          max_daily_loss: cfg.max_daily_loss || prev.max_daily_loss,
          nifty_lots: cfg.nifty_lots || prev.nifty_lots,
          sensex_lots: cfg.sensex_lots || prev.sensex_lots
        }));
      })
      .catch(err => console.error('Failed to load initial execution config:', err));
    return () => { isMounted = false; };
  }, []);

  // Fetch status, positions, orders, daily summary (Strictly Telemetry only - NEVER overwrites user form config)
  const fetchData = useCallback(async () => {
    try {
      const ordersUrl = selectedOrderDate
        ? `http://localhost:8001/api/execution/orders?date_str=${selectedOrderDate}`
        : 'http://localhost:8001/api/execution/orders';

      const [statusRes, posRes, ordRes, dailyRes] = await Promise.all([
        fetch('http://localhost:8001/api/execution/status').then(r => r.json()).catch(() => null),
        fetch('http://localhost:8001/api/execution/positions').then(r => r.json()).catch(() => []),
        fetch(ordersUrl).then(r => r.json()).catch(() => []),
        fetch('http://localhost:8001/api/execution/daily-summary').then(r => r.json()).catch(() => [])
      ]);

      if (statusRes) {
        setStatus(statusRes);
      }
      if (Array.isArray(posRes)) setPositions(posRes);
      if (Array.isArray(ordRes)) setOrders(ordRes);
      if (Array.isArray(dailyRes)) setDailySummaries(dailyRes);
    } catch (err) {
      console.error('Failed to fetch execution state:', err);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [selectedOrderDate]);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 1500);
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
        setIsConfigDirty(false);
        setActionSuccess('Configuration saved and applied to runtime bridge.');
        if (res.config) {
          setConfig(prev => ({
            ...prev,
            allowed_strategies: res.config.allowed_strategies || prev.allowed_strategies,
            max_capital_per_trade: res.config.max_capital_per_trade || prev.max_capital_per_trade,
            max_daily_loss: res.config.max_daily_loss || prev.max_daily_loss,
            nifty_lots: res.config.nifty_lots || prev.nifty_lots,
            sensex_lots: res.config.sensex_lots || prev.sensex_lots
          }));
          setStatus(prev => ({
            ...prev,
            allowed_strategies: res.config.allowed_strategies,
            max_capital_per_trade: res.config.max_capital_per_trade,
            max_daily_loss: res.config.max_daily_loss,
            nifty_lots: res.config.nifty_lots,
            sensex_lots: res.config.sensex_lots
          }));
        }
        setTimeout(() => setActionSuccess(null), 4000);
      } else {
        setActionError(res.message || 'Failed to save configuration');
      }
    } catch (err) {
      setActionError('Failed to save config');
    } finally {
      setConfigSaving(false);
    }
  };

  const isStrategySelected = (stratId, allowedList = []) => {
    if (!Array.isArray(allowedList)) return false;
    if (allowedList.includes(stratId)) return true;
    if (stratId === 'ORB_BREAKOUT' && allowedList.includes('BREAKOUT_ORB')) return true;
    if (stratId === 'BREAKOUT_ORB' && allowedList.includes('ORB_BREAKOUT')) return true;
    return false;
  };

  const toggleStrategy = (stratId) => {
    setIsConfigDirty(true);
    setConfig(prev => {
      const allowed = Array.isArray(prev.allowed_strategies) ? [...prev.allowed_strategies] : [];
      const isCurrentlySelected = isStrategySelected(stratId, allowed);
      
      let updated;
      if (isCurrentlySelected) {
        // Remove canonical ID and any legacy alias
        updated = allowed.filter(s => s !== stratId && !(stratId === 'ORB_BREAKOUT' && s === 'BREAKOUT_ORB') && !(stratId === 'BREAKOUT_ORB' && s === 'ORB_BREAKOUT'));
      } else {
        // Add canonical ID
        const canonicalId = (stratId === 'BREAKOUT_ORB') ? 'ORB_BREAKOUT' : stratId;
        updated = [...allowed.filter(s => s !== 'BREAKOUT_ORB' && s !== 'ORB_BREAKOUT'), canonicalId];
      }
      return { ...prev, allowed_strategies: updated };
    });
  };

  const allAvailableStrategies = [
    { id: 'OI_SQUEEZE', name: 'OI Squeeze Sentinel', desc: 'Aggressive call/put open interest unwinding & ITM trapped shorts.' },
    { id: 'VWAP_EMA', name: 'VWAP & EMA Alignment (Institutional)', desc: 'Trend day impulse continuation with ADX trend mode & EMA9/21 pullbacks.' },
    { id: 'SIMPLIFIED_VWAP_EMA', name: 'Simplified VWAP Price Action', desc: '3-min candle rejection wick pullbacks with green/red candle confirmation & option RVOL.' },
    { id: 'ORB_BREAKOUT', name: 'Opening Range Breakout (ORB)', desc: 'Morning session 15-min range breakout with volume surge.' },
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
          gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))',
          gap: '1rem',
          marginTop: '1.25rem',
          paddingTop: '1rem',
          borderTop: '1px solid var(--border-subtle)'
        }}>
          <div>
            <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>CAPITAL POOL</div>
            <div className="mono" style={{ fontSize: '1.15rem', fontWeight: '700', color: 'var(--text-primary)' }}>
              ₹{(status?.capital_metrics?.total_capital || 25000).toLocaleString('en-IN')}
            </div>
          </div>

          <div>
            <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>DEPLOYED MARGIN</div>
            <div className="mono" style={{ fontSize: '1.15rem', fontWeight: '700', color: (status?.capital_metrics?.deployed_capital || 0) > 0 ? 'var(--accent-amber)' : 'var(--text-muted)' }}>
              ₹{(status?.capital_metrics?.deployed_capital || 0).toLocaleString('en-IN')}
              <span style={{ fontSize: '0.75rem', fontWeight: '500', marginLeft: '4px', color: 'var(--text-secondary)' }}>
                ({status?.capital_metrics?.utilization_pct || 0}%)
              </span>
            </div>
          </div>

          <div>
            <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>AVAILABLE CASH</div>
            <div className="mono" style={{ fontSize: '1.15rem', fontWeight: '700', color: (status?.capital_metrics?.available_capital ?? 25000) > 5000 ? 'var(--ce-green)' : 'var(--pe-red)' }}>
              ₹{(status?.capital_metrics?.available_capital ?? 25000).toLocaleString('en-IN')}
            </div>
          </div>

          <div>
            <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>ACTIVE POSITIONS</div>
            <div className="mono" style={{ fontSize: '1.15rem', fontWeight: '700', color: positions.length > 0 ? 'var(--accent-cyan)' : 'var(--text-primary)' }}>
              {positions.length} / {status?.capital_metrics?.max_active_positions || 2}
            </div>
          </div>

          <div>
            <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>MAX DAILY LOSS</div>
            <div className="mono" style={{ fontSize: '1.15rem', fontWeight: '700', color: 'var(--pe-red)' }}>
              -₹{(status?.max_daily_loss || 4000).toLocaleString('en-IN')}
            </div>
          </div>

          <div>
            <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>SESSION REALIZED P&L</div>
            <div className="mono" style={{ 
              fontSize: '1.15rem', 
              fontWeight: '700', 
              color: (status?.session_realized_pnl ?? 0) >= 0 ? 'var(--ce-green)' : 'var(--pe-red)' 
            }}>
              {(status?.session_realized_pnl ?? 0) >= 0 ? '+' : ''}₹{(status?.session_realized_pnl ?? 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
              <span style={{ 
                fontSize: '0.72rem', 
                fontWeight: '600', 
                marginLeft: '4px', 
                color: (status?.session_roi_pct ?? 0) >= 0 ? 'var(--ce-green)' : 'var(--pe-red)' 
              }}>
                ({(status?.session_roi_pct ?? 0) >= 0 ? '+' : ''}{(status?.session_roi_pct ?? 0).toFixed(2)}%)
              </span>
            </div>
          </div>

          <div>
            <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>PHASE 2 SIZING RULE</div>
            <div className="mono" style={{ fontSize: '0.82rem', fontWeight: '700', color: 'var(--accent-cyan)', marginTop: '4px' }}>
              {status?.force_single_lot ? '🛡️ STRICT 1-LOT LOCK' : 'DYNAMIC MULTI-LOT'}
            </div>
            <div style={{ fontSize: '0.7rem', color: 'var(--text-secondary)' }}>
              NIFTY: 1L (65q) | SENSEX: 1L (20q)
            </div>
          </div>
        </div>
      </div>

      {/* Navigation Sub-Tabs */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', borderBottom: '1px solid var(--border-subtle)', paddingBottom: '0.5rem' }}>
        <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
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
            className={`btn-secondary ${activeSubTab === 'daywise' ? 'btn-primary' : ''}`}
            onClick={() => setActiveSubTab('daywise')}
            style={{ fontSize: '0.8rem', padding: '6px 14px' }}
          >
            <Calendar size={14} />
            <span>Day-Wise P&L Journal</span>
            {dailySummaries.length > 0 && <span className="badge-count">{dailySummaries.length}d</span>}
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
                const meta = pos.metadata || {};
                const broker = pos.broker_payload || {};
                const fillPrice = pos.fill_price || pos.entry_price || meta.expected_entry || 0;

                // Priority 1: Live tick from activeSignals WebSocket stream (updates in real-time)
                // Priority 2: pos.current_ltp from API poll
                // Priority 3: fillPrice
                const posToken = String(pos.option_token || meta.option_token || broker.symboltoken || '');
                const liveSig = (activeSignals || []).find(s => 
                  (s.signal_id && s.signal_id === pos.signal_id) || 
                  (posToken && s.option_token && String(s.option_token) === posToken)
                );
                const currentPrice = (liveSig && liveSig.live_ltp > 0) 
                  ? liveSig.live_ltp 
                  : (pos.current_ltp && pos.current_ltp > 0 ? pos.current_ltp : fillPrice);

                const remQty = pos.remaining_quantity || pos.quantity || meta.quantity || 0;
                const unrlPnl = pos.unrealized_pnl !== undefined && (!liveSig || liveSig.live_ltp === pos.current_ltp)
                  ? pos.unrealized_pnl
                  : ((currentPrice - fillPrice) * remQty);
                const isProfitable = unrlPnl >= 0;
                const pnlPct = fillPrice > 0 ? ((currentPrice - fillPrice) / fillPrice) * 100 : 0;

                const inst = pos.instrument || meta.instrument || 'INDEX';
                const strike = pos.strike || meta.strike || '';
                const dir = pos.direction || meta.direction || (pos.broker_payload?.tradingsymbol?.endsWith('PE') ? 'PE' : 'CE');
                const optSymbol = pos.option_symbol || meta.option_symbol || pos.tradingsymbol || pos.broker_payload?.tradingsymbol || `${inst} ${strike} ${dir}`;
                const strat = pos.strategy || meta.strategy || meta.strategy_name || 'MOMENTUM_IMPULSE';
                const lotsCount = pos.lots_count ?? meta.lots_count ?? (pos.quantity ? Math.max(1, Math.round(pos.quantity / (pos.lot_size || 1))) : 1);
                const target1 = pos.target_1r ?? meta.target_1r ?? meta.target ?? null;
                const target2 = pos.target_2r ?? meta.target_2r ?? null;
                const stopLoss = pos.breakeven_sl ?? pos.stop_loss ?? meta.breakeven_sl ?? meta.stop_loss ?? null;
                const isSingleLot = lotsCount <= 1;

                return (
                  <div 
                    key={pos.signal_id || pos.order_id} 
                    className="glass-panel"
                    style={{
                      padding: '1.25rem',
                      borderLeft: `4px solid ${dir === 'CE' ? 'var(--ce-green)' : 'var(--pe-red)'}`
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
                            background: dir === 'CE' ? 'var(--ce-green-bg)' : 'var(--pe-red-bg)',
                            color: dir === 'CE' ? 'var(--ce-green)' : 'var(--pe-red)',
                            border: `1px solid ${dir === 'CE' ? 'var(--ce-green)' : 'var(--pe-red)'}`
                          }}>
                            {inst} {strike} {dir}
                          </span>

                          <span style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                            {optSymbol}
                          </span>

                          <span style={{
                            padding: '2px 6px',
                            borderRadius: 'var(--radius-sm)',
                            background: 'rgba(255, 255, 255, 0.05)',
                            fontSize: '0.7rem',
                            color: 'var(--text-secondary)'
                          }}>
                            {strat}
                          </span>

                          <span style={{
                            padding: '2px 6px',
                            borderRadius: 'var(--radius-sm)',
                            background: isSingleLot ? 'rgba(0, 240, 255, 0.1)' : 'rgba(255, 170, 0, 0.1)',
                            border: `1px solid ${isSingleLot ? 'var(--accent-cyan)' : 'var(--accent-amber)'}`,
                            fontSize: '0.68rem',
                            fontWeight: '600',
                            color: isSingleLot ? 'var(--accent-cyan)' : 'var(--accent-amber)'
                          }}>
                            {isSingleLot ? '1-LOT (EXIT @ T1)' : '2-LOTS (1:2 TRAIL)'}
                          </span>
                        </div>

                        {/* Price Details */}
                        <div style={{ display: 'flex', gap: '1.5rem', marginTop: '0.75rem', fontSize: '0.8rem', flexWrap: 'wrap', alignItems: 'center' }}>
                          <div>
                            <span style={{ color: 'var(--text-muted)' }}>Fill Price: </span>
                            <span className="mono" style={{ fontWeight: '600' }}>₹{fillPrice.toFixed(2)}</span>
                          </div>
                          <div>
                            <span style={{ color: 'var(--text-muted)' }}>Current LTP: </span>
                            <span 
                              className="mono" 
                              style={{ 
                                fontWeight: '700', 
                                color: currentPrice > fillPrice ? 'var(--ce-green)' : (currentPrice < fillPrice ? 'var(--pe-red)' : 'var(--text-primary)') 
                              }}
                            >
                              ₹{currentPrice.toFixed(2)}
                            </span>
                            <span 
                              style={{
                                display: 'inline-block',
                                width: '6px',
                                height: '6px',
                                borderRadius: '50%',
                                backgroundColor: 'var(--accent-cyan)',
                                marginLeft: '6px',
                                boxShadow: '0 0 6px var(--accent-cyan)',
                                verticalAlign: 'middle'
                              }} 
                              title="Live dynamic pricing active" 
                            />
                          </div>
                          <div>
                            <span style={{ color: 'var(--text-muted)' }}>Qty: </span>
                            <span className="mono" style={{ fontWeight: '600' }}>{remQty} ({lotsCount}L)</span>
                          </div>
                          <div>
                            <span style={{ color: 'var(--text-muted)' }}>{isSingleLot ? 'Target (+1R Full): ' : 'Target 1 (50%): '}</span>
                            <span className="mono" style={{ color: 'var(--ce-green)', fontWeight: '600' }}>
                              {target1 ? `₹${Number(target1).toFixed(2)}` : '₹-'} {pos.target_1_hit ? '(HIT 🎯)' : ''}
                            </span>
                          </div>
                          {!isSingleLot && target2 && (
                            <div>
                              <span style={{ color: 'var(--text-muted)' }}>Target 2 (Runner): </span>
                              <span className="mono" style={{ color: 'var(--accent-cyan)', fontWeight: '600' }}>
                                ₹{Number(target2).toFixed(2)}
                              </span>
                            </div>
                          )}
                          <div>
                            <span style={{ color: 'var(--text-muted)' }}>Stop Loss: </span>
                            <span className="mono" style={{ color: 'var(--pe-red)', fontWeight: '600' }}>
                              {stopLoss ? `₹${Number(stopLoss).toFixed(2)}` : '₹-'}
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
                            <span style={{ fontSize: '0.75rem', marginLeft: '6px', fontWeight: '500' }}>
                              ({isProfitable ? '+' : ''}{pnlPct.toFixed(2)}%)
                            </span>
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
          {/* Filter Bar */}
          <div style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            marginBottom: '1rem',
            paddingBottom: '0.75rem',
            borderBottom: '1px solid var(--border-subtle)',
            flexWrap: 'wrap',
            gap: '0.75rem'
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <Layers size={16} color="var(--accent-cyan)" />
              <span style={{ fontWeight: '700', fontSize: '0.88rem' }}>Order Execution Audit Trail</span>
              <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>({orders.length} events logged)</span>
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <Calendar size={14} color="var(--text-muted)" />
              <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Filter Date:</span>
              <select
                value={selectedOrderDate}
                onChange={(e) => setSelectedOrderDate(e.target.value)}
                style={{
                  background: 'rgba(255, 255, 255, 0.05)',
                  color: 'var(--text-primary)',
                  border: '1px solid var(--border-subtle)',
                  borderRadius: 'var(--radius-sm)',
                  padding: '4px 10px',
                  fontSize: '0.78rem',
                  outline: 'none',
                  cursor: 'pointer'
                }}
              >
                <option value="" style={{ background: '#0a0d14' }}>Today / Active Session</option>
                {dailySummaries.map(d => (
                  <option key={d.date} value={d.date} style={{ background: '#0a0d14' }}>
                    {d.date} ({d.realized_pnl >= 0 ? '+' : ''}₹{d.realized_pnl.toFixed(2)})
                  </option>
                ))}
              </select>
              {selectedOrderDate && (
                <button
                  className="btn-secondary"
                  onClick={() => setSelectedOrderDate('')}
                  style={{ padding: '4px 10px', fontSize: '0.72rem' }}
                >
                  Reset to Today
                </button>
              )}
            </div>
          </div>

          {orders.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '2.5rem', color: 'var(--text-muted)', fontSize: '0.85rem' }}>
              {selectedOrderDate ? `No order records found for ${selectedOrderDate}.` : 'No orders placed in current session yet.'}
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
                {orders.map((ord, idx) => {
                  const meta = ord.metadata || {};
                  const broker = ord.broker_payload || {};
                  const timeVal = ord.timestamp || ord.closed_at || ord.filled_at || ord.placed_at;
                  const timeStr = timeVal ? new Date(timeVal).toLocaleTimeString() : '-';
                  const sym = ord.symbol || ord.option_symbol || broker.tradingsymbol || meta.option_symbol || '-';
                  const dir = ord.direction || meta.direction || (sym.endsWith('PE') ? 'PE' : 'CE');
                  const actionType = ord.action || broker.transactiontype || 'BUY';
                  const qtyVal = ord.quantity || ord.quantity_sold || meta.quantity || '-';
                  const fillVal = ord.fill_price || ord.exit_price || meta.expected_entry || '-';
                  const pnl = ord.total_realized_pnl !== undefined ? ord.total_realized_pnl : (ord.realized_pnl !== undefined ? ord.realized_pnl : 0);
                  const isProfit = pnl >= 0;

                  return (
                    <tr key={ord.order_id || idx} style={{ borderBottom: '1px solid rgba(255,255,255,0.03)' }}>
                      <td className="mono" style={{ padding: '8px', color: 'var(--accent-cyan)' }}>{ord.order_id}</td>
                      <td style={{ padding: '8px', color: 'var(--text-muted)' }}>{timeStr}</td>
                      <td style={{ padding: '8px', fontWeight: '600' }}>{sym}</td>
                      <td style={{ padding: '8px' }}>
                        <span style={{
                          padding: '2px 6px',
                          borderRadius: 'var(--radius-sm)',
                          fontSize: '0.7rem',
                          fontWeight: '700',
                          background: dir === 'CE' ? 'var(--ce-green-bg)' : 'var(--pe-red-bg)',
                          color: dir === 'CE' ? 'var(--ce-green)' : 'var(--pe-red)'
                        }}>
                          {actionType} {dir}
                        </span>
                      </td>
                      <td className="mono" style={{ padding: '8px' }}>{qtyVal}</td>
                      <td className="mono" style={{ padding: '8px' }}>₹{typeof fillVal === 'number' ? fillVal.toFixed(2) : fillVal}</td>
                      <td style={{ padding: '8px' }}>
                        {ord.status === 'FILLED' && !ord.exit_reason ? (
                          <span style={{
                            padding: '2px 8px',
                            borderRadius: 'var(--radius-sm)',
                            fontSize: '0.7rem',
                            fontWeight: '600',
                            background: 'rgba(0, 240, 255, 0.12)',
                            color: 'var(--accent-cyan)',
                            border: '1px solid rgba(0, 240, 255, 0.3)',
                            display: 'inline-flex',
                            alignItems: 'center',
                            gap: '4px'
                          }}>
                            <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: 'var(--accent-cyan)' }} />
                            OPEN (HOLDING)
                          </span>
                        ) : (
                          <span style={{
                            padding: '2px 6px',
                            borderRadius: 'var(--radius-sm)',
                            fontSize: '0.7rem',
                            background: ord.status === 'CLOSED' ? 'rgba(0, 240, 255, 0.1)' : 'rgba(0, 230, 118, 0.1)',
                            color: ord.status === 'CLOSED' ? 'var(--accent-cyan)' : 'var(--ce-green)'
                          }}>
                            {ord.exit_reason || ord.status || 'COMPLETED'}
                          </span>
                        )}
                      </td>
                      <td className="mono" style={{ padding: '8px', fontWeight: '700' }}>
                        {ord.status === 'FILLED' && !ord.exit_reason ? (
                          <span style={{ color: 'var(--text-muted)', fontSize: '0.72rem', fontStyle: 'italic', fontWeight: 'normal' }}>
                            {ord.unrealized_pnl !== undefined ? (
                              <span style={{ color: ord.unrealized_pnl >= 0 ? 'var(--ce-green)' : 'var(--pe-red)', fontStyle: 'normal', fontWeight: '700' }}>
                                MTM: {ord.unrealized_pnl >= 0 ? '+' : ''}₹{ord.unrealized_pnl.toFixed(2)}
                              </span>
                            ) : (
                              '— (Holding / MTM)'
                            )}
                          </span>
                        ) : (
                          <span style={{ color: isProfit ? 'var(--ce-green)' : 'var(--pe-red)' }}>
                            {isProfit ? `+₹${pnl.toFixed(2)}` : `-₹${Math.abs(pnl).toFixed(2)}`}
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* SUB-TAB: DAY-WISE P&L JOURNAL */}
      {activeSubTab === 'daywise' && (() => {
        const totalRealizedPnl = dailySummaries.reduce((acc, d) => acc + (d.realized_pnl || 0), 0);
        const totalGrossProfit = dailySummaries.reduce((acc, d) => acc + (d.gross_profit || 0), 0);
        const totalGrossLoss = dailySummaries.reduce((acc, d) => acc + (d.gross_loss || 0), 0);
        const totalTrades = dailySummaries.reduce((acc, d) => acc + (d.trades_count || 0), 0);
        const totalWins = dailySummaries.reduce((acc, d) => acc + (d.wins || 0), 0);
        const totalLosses = dailySummaries.reduce((acc, d) => acc + (d.losses || 0), 0);
        const poolCap = status?.capital_metrics?.total_capital || 25000;
        const totalRoiPct = (totalRealizedPnl / poolCap) * 100;
        const winRate = totalTrades > 0 ? ((totalWins / totalTrades) * 100).toFixed(1) : '0.0';
        const profitFactor = totalGrossLoss > 0 ? (totalGrossProfit / totalGrossLoss).toFixed(2) : (totalGrossProfit > 0 ? '∞' : '0.00');

        return (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
            {/* All-Time Broker Performance Strip */}
            <div className="glass-panel" style={{ padding: '1.25rem' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '1rem', borderBottom: '1px solid var(--border-subtle)', paddingBottom: '0.75rem' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                  <TrendingUp size={18} color="var(--accent-cyan)" />
                  <span style={{ fontWeight: '700', fontSize: '0.95rem' }}>Live Broker Performance Overview</span>
                </div>
                <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                  Aggregated from {dailySummaries.length} recorded session{dailySummaries.length === 1 ? '' : 's'}
                </span>
              </div>

              <div style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))',
                gap: '1rem'
              }}>
                <div>
                  <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>CUMULATIVE NET P&L</div>
                  <div className="mono" style={{
                    fontSize: '1.25rem',
                    fontWeight: '800',
                    color: totalRealizedPnl >= 0 ? 'var(--ce-green)' : 'var(--pe-red)'
                  }}>
                    {totalRealizedPnl >= 0 ? '+' : ''}₹{totalRealizedPnl.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  </div>
                  <div style={{ fontSize: '0.75rem', fontWeight: '600', color: totalRoiPct >= 0 ? 'var(--ce-green)' : 'var(--pe-red)' }}>
                    {totalRoiPct >= 0 ? '+' : ''}{totalRoiPct.toFixed(2)}% on ₹{(poolCap).toLocaleString('en-IN')}
                  </div>
                </div>

                <div>
                  <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>TOTAL COMPLETED TRADES</div>
                  <div className="mono" style={{ fontSize: '1.25rem', fontWeight: '800', color: 'var(--text-primary)' }}>
                    {totalTrades}
                  </div>
                  <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
                    <span style={{ color: 'var(--ce-green)', fontWeight: '700' }}>{totalWins}W</span> / <span style={{ color: 'var(--pe-red)', fontWeight: '700' }}>{totalLosses}L</span>
                  </div>
                </div>

                <div>
                  <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>OVERALL WIN RATE</div>
                  <div className="mono" style={{
                    fontSize: '1.25rem',
                    fontWeight: '800',
                    color: Number(winRate) >= 50 ? 'var(--ce-green)' : 'var(--accent-amber)'
                  }}>
                    {winRate}%
                  </div>
                  <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                    {totalWins} wins out of {totalTrades} exits
                  </div>
                </div>

                <div>
                  <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>PROFIT FACTOR</div>
                  <div className="mono" style={{ fontSize: '1.25rem', fontWeight: '800', color: Number(profitFactor) >= 1.5 ? 'var(--ce-green)' : 'var(--accent-cyan)' }}>
                    {profitFactor}
                  </div>
                  <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                    Gross: +₹{Math.round(totalGrossProfit).toLocaleString('en-IN')} / -₹{Math.round(totalGrossLoss).toLocaleString('en-IN')}
                  </div>
                </div>

                <div>
                  <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>RECORDED DAYS</div>
                  <div className="mono" style={{ fontSize: '1.25rem', fontWeight: '800', color: 'var(--accent-cyan)' }}>
                    {dailySummaries.length}
                  </div>
                  <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                    Daily audit archives
                  </div>
                </div>
              </div>
            </div>

            {/* Day by Day Breakdown Table */}
            <div className="glass-panel" style={{ padding: '1.25rem', overflowX: 'auto' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '1rem', borderBottom: '1px solid var(--border-subtle)', paddingBottom: '0.75rem' }}>
                <div>
                  <h4 style={{ fontSize: '0.92rem', fontWeight: '700' }}>Day-by-Day Broker Settlement Breakdown</h4>
                  <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: '2px' }}>
                    Historical daily performance derived directly from Angel One paper execution fill logs.
                  </p>
                </div>
              </div>

              {dailySummaries.length === 0 ? (
                <div style={{ textAlign: 'center', padding: '3rem 1rem', color: 'var(--text-muted)', fontSize: '0.85rem' }}>
                  No historical order settlement logs recorded yet.
                </div>
              ) : (
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.8rem' }}>
                  <thead>
                    <tr style={{ borderBottom: '1px solid var(--border-subtle)', color: 'var(--text-muted)', textAlign: 'left' }}>
                      <th style={{ padding: '10px 8px' }}>Date</th>
                      <th style={{ padding: '10px 8px' }}>Trades (Exits)</th>
                      <th style={{ padding: '10px 8px' }}>Wins / Losses</th>
                      <th style={{ padding: '10px 8px' }}>Win Rate</th>
                      <th style={{ padding: '10px 8px' }}>Gross Profit</th>
                      <th style={{ padding: '10px 8px' }}>Gross Loss</th>
                      <th style={{ padding: '10px 8px' }}>Profit Factor</th>
                      <th style={{ padding: '10px 8px' }}>Net Realized P&L</th>
                      <th style={{ padding: '10px 8px' }}>ROI %</th>
                      <th style={{ padding: '10px 8px', textAlign: 'center' }}>Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {dailySummaries.map((day) => {
                      const isProfit = day.realized_pnl >= 0;
                      const todayStr = new Date().toISOString().split('T')[0];
                      const isToday = day.date === todayStr;

                      return (
                        <tr key={day.date} style={{ borderBottom: '1px solid rgba(255,255,255,0.03)' }}>
                          <td style={{ padding: '10px 8px', fontWeight: '600' }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                              <Calendar size={13} color="var(--accent-cyan)" />
                              <span className="mono">{day.date}</span>
                              {isToday && (
                                <span style={{
                                  fontSize: '0.65rem',
                                  padding: '2px 6px',
                                  borderRadius: 'var(--radius-sm)',
                                  background: 'rgba(0, 240, 255, 0.15)',
                                  color: 'var(--accent-cyan)',
                                  fontWeight: '700'
                                }}>
                                  TODAY
                                </span>
                              )}
                            </div>
                          </td>

                          <td className="mono" style={{ padding: '10px 8px' }}>
                            {day.trades_count}
                          </td>

                          <td style={{ padding: '10px 8px' }}>
                            <span style={{ color: 'var(--ce-green)', fontWeight: '700' }}>{day.wins}W</span>
                            {' / '}
                            <span style={{ color: 'var(--pe-red)', fontWeight: '700' }}>{day.losses}L</span>
                            {day.breakeven > 0 && <span style={{ color: 'var(--text-muted)' }}> ({day.breakeven} BE)</span>}
                          </td>

                          <td className="mono" style={{ padding: '10px 8px', fontWeight: '700', color: day.win_rate >= 50 ? 'var(--ce-green)' : 'var(--accent-amber)' }}>
                            {day.win_rate}%
                          </td>

                          <td className="mono" style={{ padding: '10px 8px', color: 'var(--ce-green)' }}>
                            +₹{day.gross_profit.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                          </td>

                          <td className="mono" style={{ padding: '10px 8px', color: 'var(--pe-red)' }}>
                            -₹{day.gross_loss.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                          </td>

                          <td className="mono" style={{ padding: '10px 8px', fontWeight: '700' }}>
                            {day.profit_factor}
                          </td>

                          <td className="mono" style={{ padding: '10px 8px', fontWeight: '800', fontSize: '0.88rem' }}>
                            <span style={{ color: isProfit ? 'var(--ce-green)' : 'var(--pe-red)' }}>
                              {isProfit ? `+₹${day.realized_pnl.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : `-₹${Math.abs(day.realized_pnl).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
                            </span>
                          </td>

                          <td className="mono" style={{ padding: '10px 8px', fontWeight: '700' }}>
                            <span style={{ color: isProfit ? 'var(--ce-green)' : 'var(--pe-red)' }}>
                              {isProfit ? '+' : ''}{day.roi_pct.toFixed(2)}%
                            </span>
                          </td>

                          <td style={{ padding: '10px 8px', textAlign: 'center' }}>
                            <button
                              onClick={() => {
                                setSelectedOrderDate(day.date);
                                setActiveSubTab('orders');
                              }}
                              className="btn-secondary"
                              style={{
                                padding: '4px 10px',
                                fontSize: '0.72rem',
                                display: 'inline-flex',
                                alignItems: 'center',
                                gap: '4px',
                                border: '1px solid var(--accent-cyan)',
                                color: 'var(--accent-cyan)'
                              }}
                            >
                              <span>View Orders</span>
                              <ArrowRight size={12} />
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        );
      })()}

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
                const isSelected = isStrategySelected(strat.id, config.allowed_strategies);
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
                      transition: 'all 0.15s ease',
                      userSelect: 'none'
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                      <span style={{ fontWeight: '600', fontSize: '0.85rem', color: isSelected ? 'var(--accent-cyan)' : 'var(--text-primary)' }}>
                        {strat.name}
                      </span>
                      <input 
                        type="checkbox" 
                        checked={isSelected} 
                        readOnly
                        style={{ 
                          accentColor: 'var(--accent-cyan)', 
                          cursor: 'pointer', 
                          pointerEvents: 'none', 
                          width: '18px', 
                          height: '18px' 
                        }}
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
                  onChange={(e) => {
                    setIsConfigDirty(true);
                    setConfig({ ...config, nifty_lots: parseInt(e.target.value) || 1 });
                  }}
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
                  onChange={(e) => {
                    setIsConfigDirty(true);
                    setConfig({ ...config, sensex_lots: parseInt(e.target.value) || 1 });
                  }}
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
                  onChange={(e) => {
                    setIsConfigDirty(true);
                    setConfig({ ...config, max_capital_per_trade: parseFloat(e.target.value) || 25000 });
                  }}
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
                  onChange={(e) => {
                    setIsConfigDirty(true);
                    setConfig({ ...config, max_daily_loss: parseFloat(e.target.value) || 4000 });
                  }}
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

            <div style={{ display: 'flex', alignItems: 'center', gap: '1rem', marginTop: '1.25rem' }}>
              <button
                className="btn-primary"
                onClick={handleSaveConfig}
                disabled={configSaving}
                style={{
                  padding: '8px 20px',
                  fontSize: '0.85rem',
                  boxShadow: isConfigDirty ? '0 0 16px rgba(0, 240, 255, 0.4)' : undefined,
                  border: isConfigDirty ? '1px solid var(--accent-cyan)' : undefined
                }}
              >
                {configSaving ? 'Saving...' : 'Save Configuration'}
              </button>
              {isConfigDirty && (
                <span style={{ fontSize: '0.8rem', color: 'var(--accent-amber)', fontWeight: '600' }}>
                  ⚠️ Unsaved changes — click Save Configuration to apply
                </span>
              )}
            </div>
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
