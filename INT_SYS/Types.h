#ifndef TYPES_H
#define TYPES_H

#include <Arduino.h>

// ============================================================================
// STATE MACHINE
// ============================================================================

// Integer values are stored in EEPROM — do not reorder or renumber.
enum State : uint8_t {
  STATE_GAUGE_ANALOG_ON_WAIT = 0,
  STATE_IDLE,
  STATE_START_PUMPS,
  STATE_WAIT_PUMPS_OK,
  STATE_ENABLE_HORNET_WAIT,  // unused; transitions immediately to RUN
  STATE_RUN,
  STATE_FAULT
};

// ============================================================================
// SERIAL COMMANDS
// ============================================================================

enum Cmd : uint8_t {
  CMD_NONE = 0,
  CMD_START,
  CMD_STOP,
  CMD_STATUS,
  CMD_RESET,
  CMD_MAINT_TOGGLE,
  // 'H' in normal mode = clear Hornet fault; in MAINT mode = Hornet relay ON
  CMD_HORNET_ON,
  CMD_HORNET_OFF,
  // MAINT relay commands
  CMD_TG60_ON,  CMD_TG60_OFF,
  CMD_TG220_ON, CMD_TG220_OFF,
  CMD_TEST_ON,  CMD_TEST_OFF,
  CMD_ALL_OFF
};

// ============================================================================
// GAUGE CONVERSION FUNCTION TYPE
// ============================================================================

typedef float (*VoltsToPressureFn)(float);

#endif // TYPES_H
