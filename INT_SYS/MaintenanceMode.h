#ifndef MAINTENANCE_MODE_H
#define MAINTENANCE_MODE_H

#include <Arduino.h>

// Result of handleToggle() so the caller knows what just happened.
enum MaintToggleResult : uint8_t {
  MAINT_ARM_PENDING,  // first tap received; waiting for second
  MAINT_ENTERED,      // second tap confirmed; MAINT is now active
  MAINT_EXITED,       // was active; now exited
  MAINT_NO_CHANGE     // should not occur in normal use
};

// Owns maintenance-mode state and the two-tap arm/disarm UX.
// Does NOT hold references to relays or the state machine — the caller
// drives hardware and state transitions based on the returned result.
class MaintenanceMode {
public:
  MaintenanceMode();

  bool isActive() const { return _active; }

  // Call when CMD_MAINT_TOGGLE is received.
  // pumpsRunning is only used when exiting to decide which message to print.
  MaintToggleResult handleToggle(uint32_t now, bool pumpsRunning);

  // Resets the idle timer — call after any MAINT command is processed.
  void touch(uint32_t now);

  // Returns true if the idle timeout has expired.
  bool timedOut(uint32_t now) const;

  // Unconditionally deactivates MAINT (used by timeout handler).
  void forceExit();

  // Clears a stale first-tap arm if the arm window has expired.
  void clearArmIfExpired(uint32_t now);

private:
  bool     _active;
  uint32_t _tArmFirst;
  uint32_t _tLastCmd;
};

#endif // MAINTENANCE_MODE_H
