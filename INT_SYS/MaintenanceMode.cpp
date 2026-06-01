#include "MaintenanceMode.h"
#include "Config.h"
#include "SerialInterface.h"

MaintenanceMode::MaintenanceMode()
  : _active(false), _tArmFirst(0), _tLastCmd(0) {}

MaintToggleResult MaintenanceMode::handleToggle(uint32_t now, bool pumpsRunning) {
  if (!_active) {
    // First tap or re-tap: start / check arm window.
    if (_tArmFirst == 0) {
      _tArmFirst = now;
      Serial.println(F("MAINT arm requested. Send 'M' again within 5 seconds to enter MAINT."));
      return MAINT_ARM_PENDING;
    }
    if ((now - _tArmFirst) <= Timing::MAINT_ARM_WINDOW_MS) {
      // Second tap within window — enter MAINT.
      _tArmFirst = 0;
      _active    = true;
      _tLastCmd  = now;
      Serial.println(F("Entered MAINT mode. Interlocks/sequencing bypassed."));
      SerialInterface::printHelpMaint();
      return MAINT_ENTERED;
    }
    // Arm window expired — treat as a fresh first tap.
    _tArmFirst = now;
    Serial.println(F("MAINT arm requested. Send 'M' again within 5 seconds to enter MAINT."));
    return MAINT_ARM_PENDING;
  }

  // Currently active — exit.
  _active    = false;
  _tArmFirst = 0;

  if (pumpsRunning) {
    Serial.println(F("Exited MAINT mode. Pumps running, entering RUN state."));
  } else {
    Serial.println(F("Exited MAINT mode. State IDLE."));
  }

  return MAINT_EXITED;
}

void MaintenanceMode::touch(uint32_t now) {
  _tLastCmd = now;
}

bool MaintenanceMode::timedOut(uint32_t now) const {
  if (!_active) return false;
  return (now - _tLastCmd) > Timing::MAINT_IDLE_TIMEOUT_MS;
}

void MaintenanceMode::forceExit() {
  _active    = false;
  _tArmFirst = 0;
}

void MaintenanceMode::clearArmIfExpired(uint32_t now) {
  if (_tArmFirst != 0 && (now - _tArmFirst) > Timing::MAINT_ARM_WINDOW_MS) {
    _tArmFirst = 0;
  }
}
