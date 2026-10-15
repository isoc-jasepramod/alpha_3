import React, { useState, useEffect } from 'react';
import { History, RefreshCw, CheckCircle2, XCircle, AlertTriangle, Timer } from 'lucide-react';

export function SignalHistoryTable() {
  const [history, setHistory] = useState([]);
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(false);
  const [dateFilter, setDateFilter] = useState('today'); // 'today' | 'all'

  const fetchSummary = async () => {
    try {
      const res = await fetch('/api/signals/today-summary');
      if (res.ok) {
        const data = await res.json();
        setSummary(data);
      }
    } catch (e) {
      console.error('Failed to fetch summary:', e);
    }
  };

  const fetchHistory = async () => {
    try {
      setLoading(true);
      fetchSummary();
      const url = dateFilter === 'today'
        ? '/api/signals/history?limit=100&date_filter=today'
        : '/api/signals/history?limit=100';
      const res = await fetch(url);
      if (res.ok) {
        const data = await res.json();
        setHistory(data);
      }
    } catch (e) {
      console.error('Failed to fetch history:', e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchHistory();
    const interval = setInterval(fetchHistory, 4000);
    return () => clearInterval(interval);
  }, [dateFilter]);

  const todayPnl = summary?.theoretical_pnl !== undefined
    ? summary.theoretical_pnl
    : history.reduce((acc, h) => acc + (h.theoretical_pnl || 0), 0);
  const isPos = todayPnl >= 0;

  return (
    <div className="glass-panel" style={{ padding: '1.25rem' }}>
      {/* Header & Controls */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem', flexWrap: 'wrap', gap: '0.75rem' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
          <History size={18} color="var(--accent-cyan)" />
          <h3 style={{ fontSize: '1.05rem', fontWeight: '700' }}>Advisory Signal Journal (PostgreSQL)</h3>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
          <div style={{ display: 'flex', background: 'rgba(0,0,0,0.3)', borderRadius: 'var(--radius-sm)', padding: '2px', border: '1px solid var(--border-subtle)' }}>
            <button
              className="btn-secondary"
              style={{
                padding: '3px 10px',
                fontSize: '0.72rem',
                border: 'none',
                background: dateFilter === 'today' ? 'rgba(0, 240, 255, 0.15)' : 'transparent',
                color: dateFilter === 'today' ? 'var(--accent-cyan)' : 'var(--text-muted)'
              }}
              onClick={() => setDateFilter('today')}
            >
              Today ({summary?.total_signals || history.length})
            </button>
            <button
              className="btn-secondary"
              style={{
                padding: '3px 10px',
                fontSize: '0.72rem',
                border: 'none',
                background: dateFilter === 'all' ? 'rgba(0, 240, 255, 0.15)' : 'transparent',
                color: dateFilter === 'all' ? 'var(--accent-cyan)' : 'var(--text-muted)'
              }}
              onClick={() => setDateFilter('all')}
            >
              All Records
            </button>
          </div>
          <button className="btn-secondary" onClick={fetchHistory} disabled={loading} style={{ padding: '4px 10px' }}>
            <RefreshCw size={13} className={loading ? 'pulse-indicator' : ''} />
            <span style={{ fontSize: '0.75rem' }}>Refresh</span>
          </button>
        </div>
      </div>

      {/* Executive Summary Metrics Card */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
        gap: '0.75rem',
        marginBottom: '1.25rem',
        padding: '0.9rem',
        background: 'rgba(255,255,255,0.02)',
        borderRadius: 'var(--radius-md)',
        border: '1px solid var(--border-subtle)'
      }}>
        <div>
          <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
            TODAY'S THEORETICAL PNL
          </div>
          <div className="mono" style={{ fontSize: '1.35rem', fontWeight: '800', color: isPos ? 'var(--ce-green)' : 'var(--pe-red)', marginTop: '2px' }}>
            {isPos ? '+' : ''}₹{todayPnl.toFixed(2)}
          </div>
        </div>

        <div>
          <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
            TRADING DAY (IST)
          </div>
          <div style={{ fontSize: '0.95rem', fontWeight: '600', color: 'var(--text-primary)', marginTop: '4px' }}>
            {summary?.date || new Date().toISOString().split('T')[0]} (Today)
          </div>
        </div>

        <div>
          <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
            OUTCOMES (W / L)
          </div>
          <div style={{ fontSize: '0.82rem', fontWeight: '600', display: 'flex', alignItems: 'center', gap: '6px', marginTop: '4px' }}>
            <span style={{ color: 'var(--ce-green)' }}>{summary?.target_hits ?? 0} Hits</span>
            <span style={{ color: 'var(--text-muted)' }}>/</span>
            <span style={{ color: 'var(--pe-red)' }}>{summary?.stop_hits ?? 0} Stops</span>
            <span style={{ color: 'var(--text-muted)', fontSize: '0.72rem' }}>
              ({summary?.win_rate ?? 0}% Win Rate)
            </span>
          </div>
        </div>

        <div>
          <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
            CHASE GUARDED
          </div>
          <div style={{ fontSize: '0.85rem', fontWeight: '600', color: 'var(--accent-amber)', marginTop: '4px' }}>
            {summary?.chase_prevented ?? 0} Saved Exits
          </div>
        </div>
      </div>

      <div className="table-container">
        {history.length === 0 ? (
          <p style={{ padding: '1.5rem', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.85rem' }}>
            No journal entries recorded for {dateFilter === 'today' ? 'today' : 'the selected filter'} yet. Signals will appear here as they are generated and resolved.
          </p>
        ) : (
          <table className="journal-table">
            <thead>
              <tr>
                <th>Date & Time (IST)</th>
                <th>Signal ID</th>
                <th>Instrument</th>
                <th>Strategy</th>
                <th>Strike & Type</th>
                <th>Entry</th>
                <th>Stop Loss</th>
                <th>Target</th>
                <th>Exit</th>
                <th>Lots (Qty)</th>
                <th>Conf</th>
                <th>Status / Outcome</th>
                <th>Theoretical PnL</th>
              </tr>
            </thead>
            <tbody className="mono">
              {history.map((item) => {
                const isWin = item.status === 'TARGET_HIT';
                const isLoss = item.status === 'STOP_HIT';
                const isChase = item.status === 'INVALID_CHASE_PREVENTED';
                const pnl = item.theoretical_pnl || 0.0;
                const conf = item.details?.confidence || item.confidence || '-';
                const t1 = item.details?.target_1r;
                const tgtDisplay = t1 ? `₹${t1.toFixed(1)} / ₹${item.target.toFixed(1)}` : `₹${item.target.toFixed(2)}`;

                // Format entry date & time in IST
                const createdDate = item.created_at ? new Date(item.created_at) : null;
                const dateStr = createdDate && !isNaN(createdDate.getTime())
                  ? createdDate.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric', timeZone: 'Asia/Kolkata' })
                  : '-';
                const timeStr = createdDate && !isNaN(createdDate.getTime())
                  ? createdDate.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false, timeZone: 'Asia/Kolkata' })
                  : '-';

                // Format exit / resolved time in IST if available
                const resolvedDate = item.resolved_at ? new Date(item.resolved_at) : null;
                const exitTimeStr = resolvedDate && !isNaN(resolvedDate.getTime())
                  ? resolvedDate.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false, timeZone: 'Asia/Kolkata' })
                  : null;

                return (
                  <tr key={item.signal_id}>
                    <td style={{ color: 'var(--text-secondary)', whiteSpace: 'nowrap' }}>
                      <div style={{ color: 'var(--text-primary)', fontWeight: '600', fontSize: '0.78rem' }}>{dateStr}</div>
                      <div style={{ color: 'var(--text-muted)', fontSize: '0.72rem' }}>{timeStr}</div>
                    </td>
                    <td style={{ color: 'var(--text-muted)', fontSize: '0.75rem' }}>{item.signal_id}</td>
                    <td>
                      <span className={`dir-badge ${item.direction === 'CE' ? 'ce' : 'pe'}`} style={{ padding: '2px 5px', fontSize: '0.65rem' }}>
                        {item.instrument} {item.direction}
                      </span>
                    </td>
                    <td><span className="strategy-tag" style={{ fontSize: '0.68rem' }}>{item.strategy.replace('_', ' ')}</span></td>
                    <td><strong>{item.strike} {item.option_type}</strong></td>
                    <td style={{ color: 'var(--text-primary)' }}>₹{item.entry_price.toFixed(2)}</td>
                    <td style={{ color: 'var(--pe-red)' }}>₹{item.stop_loss.toFixed(2)}</td>
                    <td style={{ color: 'var(--ce-green)' }}>{tgtDisplay}</td>
                    <td style={{ color: item.exit_price ? (item.exit_price > item.entry_price ? 'var(--ce-green)' : 'var(--pe-red)') : 'var(--text-muted)' }}>
                      <div>{item.exit_price ? `₹${item.exit_price.toFixed(2)}` : '-'}</div>
                      {exitTimeStr && (
                        <div style={{ fontSize: '0.68rem', color: 'var(--text-muted)' }} title={`Exit time: ${exitTimeStr}`}>
                          @{exitTimeStr}
                        </div>
                      )}
                    </td>
                    <td style={{ fontSize: '0.75rem' }}>
                      {item.quantity} ({item.quantity / (item.lot_size || 1)}L)
                    </td>
                    <td style={{ color: 'var(--accent-cyan)' }}>{conf !== '-' ? `${conf}%` : '-'}</td>
                    <td>
                      {isWin && <span style={{ color: 'var(--ce-green)', display: 'flex', alignItems: 'center', gap: '4px' }}><CheckCircle2 size={12} /> Target Hit</span>}
                      {isLoss && <span style={{ color: 'var(--pe-red)', display: 'flex', alignItems: 'center', gap: '4px' }}><XCircle size={12} /> Stop Hit</span>}
                      {isChase && <span style={{ color: 'var(--accent-amber)', display: 'flex', alignItems: 'center', gap: '4px' }}><AlertTriangle size={12} /> Chase Prevented</span>}
                      {item.status === 'ACTIVE' && (
                        <span style={{ color: 'var(--accent-cyan)', display: 'flex', alignItems: 'center', gap: '4px', fontWeight: '600' }}>
                          <Timer size={12} className="pulse-indicator" /> Active In Trade
                        </span>
                      )}
                      {!isWin && !isLoss && !isChase && item.status !== 'ACTIVE' && (
                        <span style={{ color: 'var(--text-muted)' }}>{item.status}</span>
                      )}
                    </td>
                    <td style={{ fontWeight: '700', color: pnl > 0 ? 'var(--ce-green)' : pnl < 0 ? 'var(--pe-red)' : 'var(--text-muted)' }}>
                      {pnl > 0 ? '+' : ''}₹{pnl.toFixed(2)}
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
}
