import React, { useState, useCallback } from 'react';
import { Layers, History, Shield, Zap, Filter, BellRing } from 'lucide-react';
import { useWebSocketStream } from './hooks/useWebSocketStream';
import { useSoundEffects } from './hooks/useSoundEffects';
import { HeaderTelemetry } from './components/HeaderTelemetry';
import { SignalCard } from './components/SignalCard';
import { SignalHistoryTable } from './components/SignalHistoryTable';
import { ReplayControlModal } from './components/ReplayControlModal';
import { RadarAlertBanner } from './components/RadarAlertBanner';

export default function App() {
  const [activeTab, setActiveTab] = useState('live'); // 'live' | 'journal'
  const [selectedInst, setSelectedInst] = useState('ALL');
  const [soundEnabled, setSoundEnabled] = useState(true);
  const [isReplayOpen, setIsReplayOpen] = useState(false);

  const { playNewSignalSound, playTargetSound, playStopSound, playChasePreventedSound, playRadarAlertSound } = useSoundEffects();

  const handleNewSignal = useCallback(
    (signal) => {
      if (soundEnabled) {
        playNewSignalSound();
      }
    },
    [soundEnabled, playNewSignalSound]
  );

  const handleSignalResolved = useCallback(
    (signal) => {
      if (!soundEnabled) return;
      if (signal.status === 'TARGET_HIT') playTargetSound();
      else if (signal.status === 'STOP_HIT') playStopSound();
      else if (signal.status === 'INVALID_CHASE_PREVENTED') playChasePreventedSound();
    },
    [soundEnabled, playTargetSound, playStopSound, playChasePreventedSound]
  );

  const handleRadarAlert = useCallback(
    (alert) => {
      if (soundEnabled) {
        playRadarAlertSound();
      }
    },
    [soundEnabled, playRadarAlertSound]
  );

  const { connected, activeSignals, radarAlerts, dismissRadarAlert, telemetry, spotData } = useWebSocketStream(
    handleNewSignal,
    handleSignalResolved,
    handleRadarAlert
  );

  const filteredSignals = activeSignals.filter((s) => {
    if (selectedInst === 'ALL') return true;
    return s.instrument === selectedInst;
  });

  return (
    <div className="app-container">
      {/* Top Telemetry Header */}
      <HeaderTelemetry
        telemetry={telemetry}
        spotData={spotData}
        connected={connected}
        soundEnabled={soundEnabled}
        onToggleSound={() => setSoundEnabled(!soundEnabled)}
        onOpenReplay={() => setIsReplayOpen(true)}
      />

      {/* Real-time Pre-Move Radar Feed */}
      <RadarAlertBanner alerts={radarAlerts} onDismiss={dismissRadarAlert} />

      {/* Advisory Notice Banner */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '0.5rem 1rem',
          borderRadius: 'var(--radius-sm)',
          background: 'rgba(255, 255, 255, 0.02)',
          border: '1px solid var(--border-subtle)',
          fontSize: '0.72rem',
          color: 'var(--text-muted)'
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
          <Shield size={13} color="var(--accent-cyan)" />
          <span>
            <strong>ADVISORY-ONLY SYSTEM:</strong> Human-in-the-loop signal tracker. No automated broker order execution. Runaway guardrails and terminal locks active.
          </span>
        </div>
        <span className="mono">RISK: 1.0% / TRADE | CIRCUIT BREAKER: -2.5%</span>
      </div>

      {/* Navigation & Filter Bar */}
      <div className="section-header">
        <div className="section-title-group">
          <div style={{ display: 'flex', gap: '0.5rem' }}>
            <button
              className={`btn-secondary ${activeTab === 'live' ? 'btn-primary' : ''}`}
              onClick={() => setActiveTab('live')}
            >

              <Zap size={14} />
              <span>Live Advisory Stream</span>
              {activeSignals.length > 0 && <span className="badge-count">{activeSignals.length}</span>}
            </button>

            <button
              className={`btn-secondary ${activeTab === 'journal' ? 'btn-primary' : ''}`}
              onClick={() => setActiveTab('journal')}
            >
              <History size={14} />
              <span>Signal Journal</span>
            </button>
          </div>
        </div>

        {/* Instrument Filter */}
        {activeTab === 'live' && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <Filter size={13} color="var(--text-muted)" />
            <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Index:</span>
            {['ALL', 'NIFTY', 'SENSEX'].map((inst) => (
              <button
                key={inst}
                className="btn-secondary"
                style={{
                  padding: '3px 10px',
                  fontSize: '0.72rem',
                  borderColor: selectedInst === inst ? 'var(--accent-cyan)' : 'var(--border-subtle)',
                  color: selectedInst === inst ? 'var(--accent-cyan)' : 'var(--text-secondary)'
                }}
                onClick={() => setSelectedInst(inst)}
              >
                {inst}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Main Content Area */}
      {activeTab === 'live' ? (
        <div>
          {filteredSignals.length === 0 ? (
            <div
              className="glass-panel"
              style={{
                padding: '4rem 2rem',
                textAlign: 'center',
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                gap: '1rem'
              }}
            >
              <div
                style={{
                  width: '54px',
                  height: '54px',
                  borderRadius: '50%',
                  background: 'rgba(0, 240, 255, 0.05)',
                  border: '1px solid rgba(0, 240, 255, 0.2)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center'
                }}
              >
                <Zap size={24} color="var(--accent-cyan)" className="pulse-indicator" />
              </div>
              <h3 style={{ fontSize: '1.1rem', fontWeight: '600' }}>Quantitative Sentinel Active & Scanning</h3>
              <p style={{ maxWidth: '480px', fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                Strategies (OI Squeeze Sentinel, Volume-backed ORB, VWAP/EMA Alignment, and Gamma Scalp) are continuously monitoring market ticks in the background. Valid momentum signals will appear here with dynamic 1:2 R:R targets and runaway guardrails.
              </p>
              <button className="btn-secondary" onClick={() => setIsReplayOpen(true)} style={{ marginTop: '0.5rem' }}>
                Simulate Morning Session (Lab Replay)
              </button>
            </div>
          ) : (
            <div className="cards-grid">
              {filteredSignals.map((signal) => (
                <SignalCard key={signal.signal_id} signal={signal} />
              ))}
            </div>
          )}
        </div>
      ) : (
        <SignalHistoryTable />
      )}

      {/* Market Replay Modal */}
      <ReplayControlModal isOpen={isReplayOpen} onClose={() => setIsReplayOpen(false)} />
    </div>
  );
}
