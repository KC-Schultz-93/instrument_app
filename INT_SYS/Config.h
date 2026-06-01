#ifndef CONFIG_H
#define CONFIG_H

#include <Arduino.h>

// ============================================================================
// PIN ASSIGNMENTS
// ============================================================================

namespace Pins {
  constexpr uint8_t UHV          = A0;
  constexpr uint8_t FORELINE     = A10;

  // Pump OK/Normal digital inputs (active LOW, INPUT_PULLUP)
  constexpr uint8_t TG220F_OK    = 26;
  constexpr uint8_t TG60F_OK     = 34;

  // Relay outputs (active LOW)
  constexpr uint8_t RELAY_TEST   = 36;
  constexpr uint8_t RELAY_TG60   = 38;
  constexpr uint8_t RELAY_HORNET = 40;
  constexpr uint8_t RELAY_TG220  = 42;
}

// ============================================================================
// TIMING CONSTANTS (milliseconds)
// ============================================================================

namespace Timing {
  constexpr uint32_t GAUGE_WARMUP_MS         = 5000UL;
  constexpr uint32_t LOG_INTERVAL_MS         = 1000UL;
  constexpr uint32_t PUMP_OK_DEBOUNCE_MS     = 2000UL;
  constexpr uint32_t FORELINE_STABLE_MS      = 2000UL;
  constexpr uint32_t PUMP_OK_LOST_TIMEOUT_MS = 15UL * 1000UL;
  constexpr uint32_t HORNET_GRACE_PERIOD_MS  = 5000UL;
  constexpr uint32_t MAINT_ARM_WINDOW_MS     = 5000UL;
  constexpr uint32_t MAINT_IDLE_TIMEOUT_MS   = 10UL * 60UL * 1000UL;
}

// ============================================================================
// PRESSURE THRESHOLDS (Torr)
// ============================================================================

namespace Thresholds {
  constexpr float FORELINE_SAFE_START_TORR = 5.0f;
  constexpr float FORELINE_TRIP_TORR       = 5.0f;
  constexpr float HORN_TRIP_RISE_TORR      = 7.0e-4f;
  constexpr float HORN_TRIP_FALL_TORR      = 5.0e-4f;
}

// ============================================================================
// GAUGE CALIBRATION
// ============================================================================

namespace Calibration {
  constexpr float TO_GAUGE_8V    = 8.0f / 5.0f;
  constexpr float ADC_CORRECTION = 2.442f / 2.628f;
}

// ============================================================================
// EEPROM ADDRESSES
// ============================================================================

namespace EEPROM_Addr {
  constexpr int MAGIC        = 0;
  constexpr int STATE        = 1;
  constexpr int RELAY_TG60   = 2;
  constexpr int RELAY_TG220  = 3;
  constexpr int RELAY_HORNET = 4;
  constexpr int MAINT_MODE   = 5;
  constexpr int FAULT_HORNET = 6;
  constexpr int FAULT_SYSTEM = 7;

  constexpr uint8_t MAGIC_VALUE = 0xA5;
}

#endif // CONFIG_H
