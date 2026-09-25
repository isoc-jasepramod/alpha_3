import React, { useState } from 'react';
import { X, Play, Square, FastForward, Database } from 'lucide-react';

export function ReplayControlModal({ isOpen, onClose }) {
  const [speed, setSpeed] = useState(2.0);
  const [loading, setLoading] = useState(false);
  const [statusMessage, setStatusMessage] = useState('');

  if (!isOpen) return null;

  const startReplay = async () => {
    setLoading(true);
    setStatusMessage('Starting simulation stream...');
    try {
      const res = await fetch(`/api/lab/replay/start?speed=${speed}`, { method: 'POST' });
      if (res.ok) {
        setStatusMessage(`Simulation replay running at ${speed}x speed on Redis channel!`);
      }
    } catch (e) {
      setStatusMessage(`Error: ${e.message}`);
    } finally {
      setLoading(false);
    }
  };

  const stopReplay = async () => {
    setLoading(true);
    try {
      const res = await fetch('/api/lab/replay/stop', { method: 'POST' });
      if (res.ok) {
        setStatusMessage('Replay stopped.');
      }
    } catch (e) {
      setStatusMessage(`Error: ${e.message}`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        backgroundColor: 'rgba(0, 0, 0, 0.75)',
        backdropFilter: 'blur(8px)',
        display: 'flex',
        justifyContent: 'center',
        alignItems: 'center',
        zIndex: 100
      }}
    >
      <div className="glass-panel" style={{ width: '440px', padding: '1.5rem', position: 'relative' }}>
        <button
          onClick={onClose}
          style={{
            position: 'absolute',
            top: '1rem',
            right: '1rem',
            background: 'none',
            border: 'none',
            color: 'var(--text-muted)',
            cursor: 'pointer'
          }}
        >
          <X size={18} />
        </button>

        <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem', marginBottom: '1rem' }}>
          <Database size={20} color="var(--accent-cyan)" />
          <h2 style={{ fontSize: '1.15rem', fontWeight: '700' }}>Lab Services: Market Replay</h2>
        </div>

        <p style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', marginBottom: '1.25rem' }}>
          Query local Parquet data lake with DuckDB and inject ticks into Redis at configurable simulation speeds.
        </p>

        <div style={{ marginBottom: '1.25rem' }}>
          <label style={{ fontSize: '0.75rem', fontWeight: '600', color: 'var(--text-muted)', display: 'block', marginBottom: '0.5rem' }}>
            REPLAY SPEED MULTIPLIER ({speed}x)
          </label>
          <div style={{ display: 'flex', gap: '0.5rem' }}>
            {[1.0, 2.0, 5.0, 10.0].map((s) => (
              <button
                key={s}
                className={`btn-secondary ${speed === s ? 'btn-primary' : ''}`}
                style={{ flex: 1, justifyContent: 'center' }}
                onClick={() => setSpeed(s)}
              >
                {s}x
              </button>
            ))}
          </div>
        </div>

        {statusMessage && (
          <div
            className="mono"
            style={{
              padding: '0.6rem',
              borderRadius: 'var(--radius-sm)',
              background: 'rgba(0, 240, 255, 0.08)',
              border: '1px solid rgba(0, 240, 255, 0.2)',
              color: 'var(--accent-cyan)',
              fontSize: '0.75rem',
              marginBottom: '1.25rem'
            }}
          >
            {statusMessage}
          </div>
        )}

        <div style={{ display: 'flex', gap: '0.75rem' }}>
          <button className="btn-primary" style={{ flex: 2, justifyContent: 'center', padding: '10px' }} onClick={startReplay} disabled={loading}>
            <Play size={15} />
            <span>Start Replay</span>
          </button>
          <button className="btn-secondary" style={{ flex: 1, justifyContent: 'center', padding: '10px' }} onClick={stopReplay} disabled={loading}>
            <Square size={15} />
            <span>Stop</span>
          </button>
        </div>
      </div>
    </div>
  );
}
