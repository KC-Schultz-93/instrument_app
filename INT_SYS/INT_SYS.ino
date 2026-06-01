/* INT_SYS (Mega 2560) — interlocks + MAINT mode
   - CSV logging at 1 Hz (3-sample rolling average)
   - Safety interlocks: thresholds, hysteresis, debounce, timeouts
   - Start/Stop/Reset commands over Serial
   - DUAL FAULT SYSTEM:
     * Hornet fault (UHV > 5e-4 Torr): Only turns off Hornet, pumps continue
     * System fault (Foreline > 5 Torr, pump OK lost/timeout): Turns off everything
     * H = reset Hornet fault only, R = reset all faults
   - STATE PERSISTENCE: System state saved to EEPROM and restored after reset/reconnection
     * Allows safe serial reconnection without stopping pumps
     * State saved every second and on every state/relay change
     * Only safe states (IDLE, RUN, ENABLE_HORNET_WAIT, FAULT) are restored
   - MAINTENANCE MODE: allows manual relay control (TG60/TG220/Hornet/TEST) via Serial
     * Enter/exit MAINT: send 'M' twice within 5 seconds
     * While in MAINT: interlocks and sequencing are bypassed; relays respond only to commands
     * Auto-timeout: if no MAINT commands for 10 minutes, outputs forced OFF and exit MAINT
     * MAINT mode state is also persisted across resets

   Relay wiring:
     TEST relay:   D36  (MAINT-only)
     TG60 relay:   D38
     Hornet relay: D40
     TG220 relay:  D42

   CALIBRATION APPLIED:
   - Added ADC correction factor: 0.9782 (Arduino read 3.296V vs actual 3.224V)
*/

#include <Arduino.h>
#include "Config.h"
#include "Types.h"
#include "Relay.h"
#include "Pump.h"
#include "Gauge.h"
#include "StateManager.h"
#include "SerialInterface.h"
#include "MaintenanceMode.h"
#include "StateMachine.h"

// ============================================================================
// HARDWARE COMPONENTS
// ============================================================================

static Relay relTG60  (Pins::RELAY_TG60);
static Relay relTG220 (Pins::RELAY_TG220);
static Relay relHornet(Pins::RELAY_HORNET);
static Relay relTest  (Pins::RELAY_TEST);

static Pump pumpTG60 (Pins::TG60F_OK);
static Pump pumpTG220(Pins::TG220F_OK);

static Gauge gaugeUHV     (Pins::UHV,      0.10f, hornetVoltsToTorr);
static Gauge gaugeForeline(Pins::FORELINE, 0.10f, stingerVoltsToTorr);

// ============================================================================
// CONTROLLERS
// ============================================================================

static StateMachine stateMachine(
  relTG60, relTG220, relHornet, relTest,
  pumpTG60, pumpTG220,
  gaugeUHV, gaugeForeline
);
static MaintenanceMode maint;

// ============================================================================
// LOOP-LOCAL LOGGING STATE
// ============================================================================

static uint32_t tLastLog         = 0;
static float    logSum_uhvV      = 0.0f;
static float    logSum_foreV     = 0.0f;
static float    logSum_uhvTorr   = 0.0f;
static float    logSum_foreTorr  = 0.0f;
static uint8_t  logSampleCount   = 0;

// ============================================================================
// SETUP
// ============================================================================

void setup() {
  relTG60.begin();
  relTG220.begin();
  relHornet.begin();
  relTest.begin();

  pumpTG60.begin();
  pumpTG220.begin();

  gaugeUHV.reset();
  gaugeForeline.reset();

  Serial.begin(115200);
  delay(100);

  stateMachine.begin(millis());

  StateManager::SavedState saved;
  const bool restored = StateManager::restore(saved);
  if (restored) {
    stateMachine.applyRestoredState(saved);
  }

  SerialInterface::printCsvHeader();
  SerialInterface::printStartupBanner(restored,
                                      stateMachine.getState(),
                                      relTG60.get(), relTG220.get(), relHornet.get(),
                                      restored ? saved.maintMode : false,
                                      Calibration::TO_GAUGE_8V,
                                      Calibration::ADC_CORRECTION);

  if (restored && saved.maintMode) {
    maint.handleToggle(millis(), false);  // arm
    maint.handleToggle(millis(), false);  // enter
  }
}

// ============================================================================
// LOOP
// ============================================================================

void loop() {
  const uint32_t now = millis();

  // --------- Read sensors ---------
  gaugeUHV.update();
  gaugeForeline.update();

  // --------- Periodic logging (3-sample rolling average) ---------
  if ((now - tLastLog) >= Timing::LOG_INTERVAL_MS) {
    tLastLog = now;

    logSum_uhvV    += gaugeUHV.volts();
    logSum_foreV   += gaugeForeline.volts();
    logSum_uhvTorr += gaugeUHV.torr();
    logSum_foreTorr+= gaugeForeline.torr();

    if (++logSampleCount >= 3) {
      SerialInterface::printCsvLineAveraged(
        now,
        logSum_uhvV    / 3.0f, logSum_foreV    / 3.0f,
        logSum_uhvTorr / 3.0f, logSum_foreTorr / 3.0f,
        stateMachine.getState(),
        pumpTG60.rawOk(), pumpTG220.rawOk(),
        relTG60.get(),    relTG220.get(),
        relHornet.get(),  relTest.get(),
        stateMachine.faultHornet(), stateMachine.faultSystem(),
        maint.isActive()
      );
      logSum_uhvV     = 0.0f;
      logSum_foreV    = 0.0f;
      logSum_uhvTorr  = 0.0f;
      logSum_foreTorr = 0.0f;
      logSampleCount  = 0;
    }

    // Periodic EEPROM save (on every log tick, not just relay changes).
    StateManager::save(stateMachine.snapshot(maint.isActive()));
  }

  // --------- Read command ---------
  Cmd cmd = SerialInterface::readSerialCmd();

  // --------- MAINT toggle handling ---------
  if (cmd == CMD_MAINT_TOGGLE) {
    const bool pumpsOn = stateMachine.pumpsRunning();
    MaintToggleResult result = maint.handleToggle(now, pumpsOn);

    if (result == MAINT_ENTERED || result == MAINT_EXITED) {
      if (result == MAINT_EXITED) {
        stateMachine.setStateAfterMaintExit(pumpsOn);
      }
      StateManager::save(stateMachine.snapshot(maint.isActive()));
    }
    return;
  }

  // Clear stale MAINT arm attempt.
  maint.clearArmIfExpired(now);

  // --------- MAINT active behavior ---------
  if (maint.isActive()) {
    if (maint.timedOut(now)) {
      Serial.println(F("MAINT timeout. Outputs forced OFF; exiting MAINT."));
      stateMachine.allOff();
      maint.forceExit();
      stateMachine.setStateAfterMaintExit(false);
      StateManager::save(stateMachine.snapshot(false));
      return;
    }

    switch (cmd) {
      case CMD_TG60_ON:    relTG60.write(true);    maint.touch(now); break;
      case CMD_TG60_OFF:   relTG60.write(false);   maint.touch(now); break;
      case CMD_TG220_ON:   relTG220.write(true);   maint.touch(now); break;
      case CMD_TG220_OFF:  relTG220.write(false);  maint.touch(now); break;
      case CMD_HORNET_ON:  relHornet.write(true);  maint.touch(now); break;
      case CMD_HORNET_OFF: relHornet.write(false); maint.touch(now); break;
      case CMD_TEST_ON:    relTest.write(true);     maint.touch(now); break;
      case CMD_TEST_OFF:   relTest.write(false);    maint.touch(now); break;
      case CMD_ALL_OFF:
        relTG60.write(false); relTG220.write(false);
        relHornet.write(false); relTest.write(false);
        maint.touch(now);
        Serial.println(F("MAINT: ALL OFF"));
        break;
      case CMD_STATUS:
        SerialInterface::printHelpMaint();
        maint.touch(now);
        break;
      case CMD_STOP:
        relTG60.write(false); relTG220.write(false);
        relHornet.write(false); relTest.write(false);
        maint.touch(now);
        Serial.println(F("MAINT: ALL OFF"));
        break;
      default:
        break;
    }

    return; // do not run interlocks/state machine in MAINT
  }

  // --------- Normal mode: safety trips then state machine ---------
  stateMachine.checkSafetyTrips(now);
  stateMachine.handleCommand(cmd);
  stateMachine.update(now);
}
