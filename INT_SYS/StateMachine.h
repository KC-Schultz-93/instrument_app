#ifndef STATE_MACHINE_H
#define STATE_MACHINE_H

#include <Arduino.h>
#include "Types.h"
#include "Relay.h"
#include "Pump.h"
#include "Gauge.h"
#include "StateManager.h"

// Owns the vacuum system state machine and all associated safety interlocks.
// Holds references to hardware objects; does not own them.
class StateMachine {
public:
  StateMachine(Relay& relTG60, Relay& relTG220, Relay& relHornet, Relay& relTest,
               Pump&  pumpTG60, Pump& pumpTG220,
               Gauge& gaugeUHV, Gauge& gaugeForeline);

  // Call once from setup() to record boot time.
  void begin(uint32_t now);

  // Run safety trips (foreline + hornet overpressure). Call before update().
  void checkSafetyTrips(uint32_t now);

  // Process one normal-mode command.
  void handleCommand(Cmd cmd);

  // Advance the state machine one step.
  void update(uint32_t now);

  // Turn all relay outputs off.
  void allOff();

  // Apply state and relay outputs restored from EEPROM.
  void applyRestoredState(const StateManager::SavedState& s);

  // Used by the main loop after MAINT exit to re-enter RUN or IDLE.
  void setStateAfterMaintExit(bool pumpsWereRunning);

  // Build a SavedState snapshot for EEPROM persistence.
  StateManager::SavedState snapshot(bool maintMode) const;

  // ---- Accessors ----
  State getState()     const { return _state; }
  bool  faultHornet()  const { return _faultHornet; }
  bool  faultSystem()  const { return _faultSystem; }
  bool  pumpsRunning() const { return _relTG60.get() && _relTG220.get(); }

private:
  Relay& _relTG60;
  Relay& _relTG220;
  Relay& _relHornet;
  Relay& _relTest;   // TEST relay: MAINT-only, never persisted to EEPROM

  Pump&  _pumpTG60;
  Pump&  _pumpTG220;

  Gauge& _gaugeUHV;
  Gauge& _gaugeForeline;

  State    _state;
  bool     _faultHornet;
  bool     _faultSystem;
  uint32_t _tBoot;
  uint32_t _tForeSafeSince;
  uint32_t _tHornetOnSince;

  // Relay wrappers: change detection + EEPROM save on actual change.
  void setTG60(bool on);
  void setTG220(bool on);
  void setHornet(bool on);  // also updates _tHornetOnSince on rising edge

  void saveState(bool maintMode = false) const;

  // Generic rising-edge debounce helper.
  static bool debounceTrue(bool raw, uint32_t holdMs, uint32_t& sinceMs);
};

#endif // STATE_MACHINE_H
