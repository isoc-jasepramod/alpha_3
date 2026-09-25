import { useRef, useCallback } from 'react';

/**
 * Web Audio API synthesizer for instant zero-dependency audio alerts.
 */
export function useSoundEffects() {
  const audioCtxRef = useRef(null);

  const getAudioContext = useCallback(() => {
    if (!audioCtxRef.current) {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (AudioCtx) {
        audioCtxRef.current = new AudioCtx();
      }
    }
    if (audioCtxRef.current && audioCtxRef.current.state === 'suspended') {
      audioCtxRef.current.resume();
    }
    return audioCtxRef.current;
  }, []);

  const playTone = useCallback((frequency, duration, type = 'sine', delay = 0) => {
    try {
      const ctx = getAudioContext();
      if (!ctx) return;

      setTimeout(() => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();

        osc.type = type;
        osc.frequency.setValueAtTime(frequency, ctx.currentTime);

        gain.gain.setValueAtTime(0.15, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + duration);

        osc.connect(gain);
        gain.connect(ctx.destination);

        osc.start();
        osc.stop(ctx.currentTime + duration);
      }, delay * 1000);
    } catch (e) {
      // Audio context might be blocked prior to user interaction
    }
  }, [getAudioContext]);

  const playNewSignalSound = useCallback(() => {
    playTone(880, 0.12, 'triangle', 0);
    playTone(1174.66, 0.25, 'sine', 0.12);
  }, [playTone]);

  const playTargetSound = useCallback(() => {
    playTone(523.25, 0.1, 'sine', 0);
    playTone(659.25, 0.1, 'sine', 0.1);
    playTone(783.99, 0.3, 'triangle', 0.2);
  }, [playTone]);

  const playStopSound = useCallback(() => {
    playTone(440, 0.15, 'sawtooth', 0);
    playTone(311.13, 0.25, 'sawtooth', 0.15);
  }, [playTone]);

  const playChasePreventedSound = useCallback(() => {
    playTone(700, 0.08, 'square', 0);
    playTone(700, 0.08, 'square', 0.12);
  }, [playTone]);

  const playRadarAlertSound = useCallback(() => {
    playTone(1046.5, 0.08, 'sine', 0);
    playTone(1318.51, 0.18, 'sine', 0.08);
  }, [playTone]);

  return {
    playNewSignalSound,
    playTargetSound,
    playStopSound,
    playChasePreventedSound,
    playRadarAlertSound
  };
}

