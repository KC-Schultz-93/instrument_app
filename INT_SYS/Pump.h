#ifndef PUMP_H
#define PUMP_H

#include <Arduino.h>

// Encapsulates one pump-OK feedback input (active-LOW, INPUT_PULLUP).
// Carries its own debounce and lost-signal timers so no extra globals
// are needed when pumps are added or removed.
struct Pump {
  uint8_t  okPin;
  uint32_t okSince;    // time OK signal first went active (for debounce)
  uint32_t lostSince;  // time OK signal first went inactive (for lost-timeout)

  explicit Pump(uint8_t p) : okPin(p), okSince(0), lostSince(0) {}

  void begin() { pinMode(okPin, INPUT_PULLUP); }

  bool rawOk() const { return digitalRead(okPin) == LOW; }

  void resetTimers() { okSince = 0; lostSince = 0; }

  // Returns true once OK has been continuously stable for >= ms.
  bool okStable(uint32_t ms, uint32_t now) {
    if (!rawOk()) { okSince = 0; return false; }
    if (!okSince)   okSince = now;
    return (now - okSince) >= ms;
  }

  // Updates lostSince tracker and returns true if OK has been absent for >= ms.
  bool lostTimeout(uint32_t ms, uint32_t now) {
    if (rawOk()) { lostSince = 0; return false; }
    if (!lostSince) lostSince = now;
    return (now - lostSince) >= ms;
  }
};

#endif // PUMP_H
