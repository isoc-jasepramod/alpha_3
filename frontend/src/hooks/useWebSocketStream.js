import { useState, useEffect, useRef, useCallback } from 'react';

export function useWebSocketStream(onNewSignal, onSignalResolved, onRadarAlert) {
  const [connected, setConnected] = useState(false);
  const [activeSignals, setActiveSignals] = useState([]);
  const [radarAlerts, setRadarAlerts] = useState([]);
  const [spotData, setSpotData] = useState({
    NIFTY: { ltp: 23346.4, change: 0, change_pct: 0, atm: 23350, open: 23346.4, high: 23346.4, low: 23346.4 },
    SENSEX: { ltp: 74294.96, change: 0, change_pct: 0, atm: 74300, open: 74294.96, high: 74294.96, low: 74294.96 }
  });
  const [telemetry, setTelemetry] = useState({
    total_equity: 100000.0,
    realized_pnl: 0.0,
    realized_pnl_pct: 0.0,
    circuit_breaker_tripped: false,
    current_atm: {}
  });

  const wsRef = useRef(null);
  const reconnectTimeoutRef = useRef(null);

  const dismissRadarAlert = useCallback((id) => {
    setRadarAlerts((prev) => prev.filter((a) => a.id !== id));
  }, []);

  const connect = useCallback(() => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host;
    const wsUrl = `${protocol}//${host}/ws/stream`;

    try {
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);

          if (data.type === 'INITIAL_STATE') {
            if (data.active_signals) {
              setActiveSignals(data.active_signals);
            }
            if (data.telemetry) {
              setTelemetry(data.telemetry);
              if (data.telemetry.spot_data) {
                setSpotData(data.telemetry.spot_data);
              }
            }
          } else if (data.type === 'TICK_BATCH') {
            // 250ms batch update from backend
            if (data.active_signals) {
              setActiveSignals(data.active_signals);
            }
            if (data.spot_data) {
              setSpotData(data.spot_data);
            }
          } else if (data.event === 'NEW_SIGNAL') {
            const sig = data.signal;
            setActiveSignals((prev) => {
              const exists = prev.some((s) => s.signal_id === sig.signal_id);
              return exists ? prev : [sig, ...prev];
            });
            // Auto-clear pre-move alerts when trade triggers
            setRadarAlerts((prev) =>
              prev.filter((a) => !(a.instrument === sig.instrument && a.direction === sig.direction))
            );
            if (onNewSignal) onNewSignal(sig);
          } else if (data.event === 'SIGNAL_RESOLVED') {
            const resolved = data.signal;
            // Update the card to terminal state
            setActiveSignals((prev) =>
              prev.map((s) => (s.signal_id === resolved.signal_id ? resolved : s))
            );
            if (onSignalResolved) onSignalResolved(resolved);
          } else if (data.event === 'RADAR_PRE_ALERT') {
            const alert = data.alert;
            setRadarAlerts((prev) => {
              // Replace older alert for same instrument and alert type
              const filtered = prev.filter(
                (a) => !(a.instrument === alert.instrument && a.alert_type === alert.alert_type)
              );
              return [alert, ...filtered.slice(0, 2)]; // Keep at most 3 active alerts
            });
            if (onRadarAlert) onRadarAlert(alert);
          }
        } catch (err) {
          console.error('Error parsing WS message:', err);
        }
      };

      ws.onclose = () => {
        setConnected(false);
        reconnectTimeoutRef.current = setTimeout(connect, 2000);
      };

      ws.onerror = () => {
        ws.close();
      };
    } catch (e) {
      reconnectTimeoutRef.current = setTimeout(connect, 2000);
    }
  }, [onNewSignal, onSignalResolved, onRadarAlert]);

  useEffect(() => {
    connect();

    // Auto-expire radar alerts after their TTL (60s)
    const alertExpiryInterval = setInterval(() => {
      const now = Date.now() / 1000;
      setRadarAlerts((prev) =>
        prev.filter((a) => {
          const ttl = a.expires_in_sec || 60;
          return now - a.timestamp < ttl;
        })
      );
    }, 1000);

    // Telemetry polling fallback
    const interval = setInterval(async () => {
      try {
        const res = await fetch('/api/telemetry');
        if (res.ok) {
          const t = await res.json();
          setTelemetry(t);
          if (t.spot_data) {
            setSpotData(t.spot_data);
          }
        }
      } catch (e) {}
    }, 2000);

    return () => {
      clearInterval(alertExpiryInterval);
      clearInterval(interval);
      if (wsRef.current) wsRef.current.close();
      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current);
    };
  }, [connect]);

  return { connected, activeSignals, radarAlerts, dismissRadarAlert, telemetry, spotData };
}


