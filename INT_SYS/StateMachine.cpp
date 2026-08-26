#include "StateMachine.h"
#include "Config.h"
#include "SerialInterface.h"

StateMachine::StateMachine(Relay& relTG60, Relay& relTG220, Relay& relHornet, Relay& relTest,
                           Pump&  pumpTG60, Pump& pumpTG220,
                           Gauge& gaugeUHV, Gauge& gaugeForeline)
  : _relTG60(relTG60), _relTG220(relTG220), _relHornet(relHornet), _relTest(relTest),
    _pumpTG60(pumpTG60), _pumpTG220(pumpTG220),
    _gaugeUHV(gaugeUHV), _gaugeForeline(gaugeForeline),
    _state(STATE_GAUGE_ANALOG_ON_WAIT),
    _faultHornet(false), _faultSystem(false),
    _tBoot(0), _tForeSafeSince(0), _tHornetOnSince(0)
{}

void StateMachine::begin(uint32_t now) {
  _tBoot = now;
}

// ---- Private helpers --------------------------------------------------------

bool StateMachine::debounceTrue(bool raw, uint32_t holdMs, uint32_t& sinceMs) {
  const uint32_t now = millis();
  if (!raw) { sinceMs = 0; return false; }
  if (sinceMs == 0) sinceMs = now;
  return (now - sinceMs) >= holdMs;
}

void StateMachine::saveState(bool maintMode) const {
  StateManager::save(snapshot(maintMode));
}

void StateMachine::setTG60(bool on) {
  if (_relTG60.get() != on) { _relTG60.write(on); saveState(); }
}

void StateMachine::setTG220(bool on) {
  if (_relTG220.get() != on) { _relTG220.write(on); saveState(); }
}

void StateMachine::setHornet(bool on) {
  if (_relHornet.get() != on) {
    _relHornet.write(on);
    if (on) _tHornetOnSince = millis();
    saveState();
  }
}

void StateMachine::allOff() {
  setTG60(false);
  setTG220(false);
  setHornet(false);
  _relTest.write(false);
}

// ---- Public interface -------------------------------------------------------

void StateMachine::applyRestoredState(const StateManager::SavedState& s) {
  _state       = s.state;
  _faultHornet = s.faultHornet;
  _faultSystem = s.faultSystem;

  if (s.state == STATE_FAULT) {
    allOff();
    return;
  }

  _relTG60.write(s.relayTG60);
  _relTG220.write(s.relayTG220);
  _relHornet.write(s.relayHornet);
}

void StateMachine::checkSafetyTrips(uint32_t now) {
  // Foreline overpressure — system fault, turns off everything.
  if (!_faultSystem && _gaugeForeline.torr() > Thresholds::FORELINE_TRIP_TORR) {
    _faultSystem = true;
    _state = STATE_FAULT;
    allOff();
    saveState();
    Serial.println(F("FAULT: Foreline overpressure (>5 Torr). All outputs OFF."));
  }

  // Hornet overpressure — turns off Hornet only, pumps continue.
  // Only trips in RUN state after the grace period has elapsed.
  const bool gracePassed = (_tHornetOnSince != 0) &&
                           ((now - _tHornetOnSince) > Timing::HORNET_GRACE_PERIOD_MS);
  if (!_faultHornet && _state == STATE_RUN && _relHornet.get() &&
      gracePassed && _gaugeUHV.torr() > Thresholds::HORN_TRIP_RISE_TORR) {
    _faultHornet = true;
    setHornet(false);
    saveState();
    Serial.println(F("FAULT: Hornet overpressure (>5e-4 Torr). Hornet OFF, pumps continue."));
  }
}

void StateMachine::handleCommand(Cmd cmd) {
  switch (cmd) {
    case CMD_STATUS:
      SerialInterface::printStatus(_state,
                                   _relTG60.get(), _relTG220.get(), _relHornet.get(),
                                   _faultHornet, _faultSystem,
                                   _gaugeForeline.torr(), _gaugeUHV.torr());
      break;

    case CMD_STOP:
      allOff();
      _state = STATE_IDLE;
      saveState();
      Serial.println(F("STOP: Outputs OFF, state IDLE."));
      break;

    case CMD_HORNET_ON:  // 'H' in normal mode = clear Hornet fault only
      if (_faultHornet) {
        _faultHornet = false;
        saveState();
        Serial.println(F("Hornet fault cleared. Hornet will auto-enable if in RUN."));
      } else {
        Serial.println(F("No Hornet fault to clear."));
      }
      break;

    case CMD_RESET:
      if (_faultHornet || _faultSystem) {
        if (_faultSystem && _gaugeForeline.torr() > Thresholds::FORELINE_SAFE_START_TORR) {
          Serial.println(F("RESET denied: Foreline pressure still too high."));
        } else {
          _faultHornet = false;
          _faultSystem = false;
          _state = STATE_IDLE;
          saveState();
          Serial.println(F("All faults cleared. State IDLE."));
        }
      } else {
        Serial.println(F("No faults to clear."));
      }
      break;

    case CMD_START:
      if (_faultSystem) {
        Serial.println(F("START denied: System fault active. Use R to reset."));
      } else if (_state != STATE_IDLE && _state != STATE_GAUGE_ANALOG_ON_WAIT) {
        Serial.println(F("START denied: Not in IDLE state."));
      } else {
        _state = STATE_START_PUMPS;
        _tForeSafeSince = 0;
        saveState();
        Serial.println(F("START requested."));
      }
      break;

    default:
      break;
  }
}

void StateMachine::update(uint32_t now) {
  switch (_state) {
    case STATE_GAUGE_ANALOG_ON_WAIT: {
      allOff();
      if ((now - _tBoot) >= Timing::GAUGE_WARMUP_MS) {
        _state = STATE_IDLE;
        saveState();
        Serial.println(F("Warmup complete. State IDLE."));
      }
      break;
    }

    case STATE_IDLE: {
      allOff();
      break;
    }

    case STATE_START_PUMPS: {
      const bool foreSafe       = (_gaugeForeline.torr() <= Thresholds::FORELINE_SAFE_START_TORR);
      const bool foreSafeStable = debounceTrue(foreSafe, Timing::FORELINE_STABLE_MS, _tForeSafeSince);

      if (foreSafeStable) {
        setTG60(true);
        setTG220(true);
        _pumpTG60.resetTimers();
        _pumpTG220.resetTimers();
        _state = STATE_WAIT_PUMPS_OK;
        saveState();
        Serial.println(F("Pumps commanded ON; waiting for OK signals."));
      } else {
        setTG60(false);
        setTG220(false);
      }
      setHornet(false);
      _relTest.write(false);
      break;
    }

    case STATE_WAIT_PUMPS_OK: {
      setHornet(false);
      _relTest.write(false);

      const bool tg60ok  = _pumpTG60.okStable(Timing::PUMP_OK_DEBOUNCE_MS, now);
      const bool tg220ok = _pumpTG220.okStable(Timing::PUMP_OK_DEBOUNCE_MS, now);

      if (tg60ok && tg220ok) {
        _pumpTG60.resetTimers();
        _pumpTG220.resetTimers();
        _state = STATE_RUN;
        saveState();
        Serial.println(F("Both pumps OK. Entering RUN state."));
      }
      break;
    }

    case STATE_ENABLE_HORNET_WAIT: {
      // Unused transition state — fall straight through to RUN.
      _state = STATE_RUN;
      saveState();
      break;
    }

    case STATE_RUN: {
      _relTest.write(false);
      setTG60(true);
      setTG220(true);

      const bool tg60Lost  = _pumpTG60.lostTimeout(Timing::PUMP_OK_LOST_TIMEOUT_MS, now);
      const bool tg220Lost = _pumpTG220.lostTimeout(Timing::PUMP_OK_LOST_TIMEOUT_MS, now);

      if (tg60Lost || tg220Lost) {
        _faultSystem = true;
        _state = STATE_FAULT;
        allOff();
        saveState();
        if (tg60Lost) {
          Serial.println(F("FAULT: TG60 OK lost for 15s. All outputs OFF."));
        } else {
          Serial.println(F("FAULT: TG220 OK lost for 15s. All outputs OFF."));
        }
        break;
      }

      setHornet(!_faultHornet);
      break;
    }

    case STATE_FAULT: {
      allOff();
      break;
    }

    default: {
      _faultSystem = true;
      _state = STATE_FAULT;
      allOff();
      Serial.println(F("FAULT: Unknown state. Outputs OFF."));
      break;
    }
  }
}

void StateMachine::setStateAfterMaintExit(bool pumpsWereRunning) {
  if (pumpsWereRunning) {
    _state = STATE_RUN;
    _pumpTG60.resetTimers();
    _pumpTG220.resetTimers();
    if (_relHornet.get()) _faultHornet = false;
  } else {
    _state = STATE_IDLE;
  }
}

StateManager::SavedState StateMachine::snapshot(bool maintMode) const {
  StateManager::SavedState s;
  s.state       = _state;
  s.relayTG60   = _relTG60.get();
  s.relayTG220  = _relTG220.get();
  s.relayHornet = _relHornet.get();
  s.maintMode   = maintMode;
  s.faultHornet = _faultHornet;
  s.faultSystem = _faultSystem;
  return s;
}
