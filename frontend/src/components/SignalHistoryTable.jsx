import React, { useState, useEffect } from 'react';
import { History, RefreshCw, CheckCircle2, XCircle, AlertTriangle } from 'lucide-react';

export function SignalHistoryTable() {
  const [history, setHistory] = useState([]);
  const [loading, setLoading] = useState(false);

  const fetchHistory = async () => {
    setLoading(true);
    try {
      const res = await fetch('/api/signals/history?limit=30');
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
  }, []);

  return (
    <div className="glass-panel" style={{ padding: '1.25rem' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.75rem' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
          <History size={16} color="var(--accent-cyan)" />
          <h3 style={{ fontSize: '1rem', fontWeight: '700' }}>Advisory Signal Journal (PostgreSQL)</h3>
        </div>
        <button className="btn-secondary" onClick={fetchHistory} disabled={loading}>
          <RefreshCw size={13} className={loading ? 'pulse-indicator' : ''} />
          <span>Refresh</span>
        </button>
      </div>

      <div className="table-container">
        {history.length === 0 ? (
          <p style={{ padding: '1.5rem', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.85rem' }}>
            No journal entries recorded yet today. Signals will appear here as they are generated and resolved.
          </p>
        ) : (
          <table className="journal-table">
            <thead>
              <tr>
                <th>Time (IST)</th>
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

                return (
                  <tr key={item.signal_id}>
                    <td style={{ color: 'var(--text-secondary)', whiteSpace: 'nowrap' }}>
                      {new Date(item.created_at).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata' })}
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
                      {item.exit_price ? `₹${item.exit_price.toFixed(2)}` : '-'}
                    </td>
                    <td style={{ fontSize: '0.75rem' }}>
                      {item.quantity} ({item.quantity / (item.lot_size || 1)}L)
                    </td>
                    <td style={{ color: 'var(--accent-cyan)' }}>{conf !== '-' ? `${conf}%` : '-'}</td>
                    <td>
                      {isWin && <span style={{ color: 'var(--ce-green)', display: 'flex', alignItems: 'center', gap: '4px' }}><CheckCircle2 size={12} /> Target Hit</span>}
                      {isLoss && <span style={{ color: 'var(--pe-red)', display: 'flex', alignItems: 'center', gap: '4px' }}><XCircle size={12} /> Stop Hit</span>}
                      {isChase && <span style={{ color: 'var(--accent-amber)', display: 'flex', alignItems: 'center', gap: '4px' }}><AlertTriangle size={12} /> Chase Prevented</span>}
                      {!isWin && !isLoss && !isChase && <span style={{ color: 'var(--accent-cyan)' }}>{item.status}</span>}
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
