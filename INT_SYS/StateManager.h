#ifndef STATE_MANAGER_H
#define STATE_MANAGER_H

#include <Arduino.h>
#include "Types.h"

// Handles saving and restoring system state to/from EEPROM.
class StateManager {
public:
  struct SavedState {
    State state;
    bool  relayTG60;
    bool  relayTG220;
    bool  relayHornet;
    bool  maintMode;
    bool  faultHornet;
    bool  faultSystem;
  };

  // Writes all fields via EEPROM.update (only writes on actual byte change).
  static void save(const SavedState& s);

  // Reads EEPROM; populates s and returns true only for safe/restorable states.
  static bool restore(SavedState& s);

  // Invalidates the magic byte so next boot is a fresh start.
  static void clear();
};

#endif // STATE_MANAGER_H
