#ifndef RELAY_H
#define RELAY_H

#include <Arduino.h>

// Encapsulates one active-LOW relay output pin.
struct Relay {
  uint8_t pin;
  bool    state;

  explicit Relay(uint8_t p) : pin(p), state(false) {}

  void begin() { pinMode(pin, OUTPUT); write(false); }

  // Unconditionally drives the pin.  Change detection + EEPROM saving is
  // handled by the setXxx() wrappers in StateMachine so frequent calls
  // (e.g. STATE_RUN every loop) don't hammer EEPROM.
  void write(bool on) {
    state = on;
    digitalWrite(pin, on ? LOW : HIGH);
  }

  bool get() const { return state; }
};

#endif // RELAY_H
