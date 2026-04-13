/* INT_SYS (Mega 2560) — interlocks + MAINT mode
   - CSV logging at 1 Hz
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
     TEST relay:   D36  (added)
     TG60 relay:   D38
     Hornet relay: D40
     TG220 relay:  D42

   CALIBRATION APPLIED:
   - Added ADC correction factor: 0.9782 (Arduino read 3.296V vs actual 3.224V)
*/

#include <Arduino.h>
#include <math.h>
#include <EEPROM.h>

// Forward declarations so Arduino's auto-prototypes don't break compilation
enum State : uint8_t;
enum Cmd   : uint8_t;

// ----------------------------- Pin Mapping -----------------------------
// Analog inputs (0–5 V into Arduino ADC)
static const uint8_t PIN_UHV      = A0; 
static const uint8_t PIN_FORELINE = A10;

// Pump "OK/Normal" digital inputs (active LOW due to INPUT_PULLUP)
static const uint8_t PIN_TG220F_OK = 26;
static const uint8_t PIN_TG60F_OK  = 34;

// Relay / SSR control outputs
static const uint8_t PIN_RELAY_TEST   = 36;
static const uint8_t PIN_RELAY_TG60F  = 38;
static const uint8_t PIN_RELAY_HORNET = 40;
static const uint8_t PIN_RELAY_TG220F = 42;

// ----------------------------- Timing -----------------------------
static const uint32_t GAUGE_WARMUP_MS        = 5000UL;
static const uint32_t LOG_INTERVAL_MS        = 1000UL;
static const uint32_t PUMP_OK_DEBOUNCE_MS    = 2000UL;
static const uint32_t FORELINE_STABLE_MS     = 2000UL;
static const uint32_t PUMP_OK_LOST_TIMEOUT_MS = 15UL * 1000UL;  // 15 seconds - pump OK lost triggers fault
static const uint32_t HORNET_GRACE_PERIOD_MS = 5000UL;  // 5 seconds grace period before overpressure can trip

// MAINT mode arming + timeout
static const uint32_t MAINT_ARM_WINDOW_MS    = 5000UL;
static const uint32_t MAINT_IDLE_TIMEOUT_MS  = 10UL * 60UL * 1000UL; // 10 minutes

// ----------------------------- State Persistence (EEPROM) -----------------------------
// EEPROM addresses for state persistence across resets
static const int EEPROM_ADDR_MAGIC      = 0;   // Magic number to verify valid data
static const int EEPROM_ADDR_STATE      = 1;   // Current state
static const int EEPROM_ADDR_RELAY_TG60 = 2;   // TG60 relay state
static const int EEPROM_ADDR_RELAY_TG220 = 3;  // TG220 relay state
static const int EEPROM_ADDR_RELAY_HORNET = 4; // Hornet relay state
static const int EEPROM_ADDR_MAINT_MODE = 5;   // Maintenance mode flag
static const int EEPROM_ADDR_FAULT_HORNET = 6; // Hornet fault flag
static const int EEPROM_ADDR_FAULT_SYSTEM = 7; // System fault flag

static const uint8_t EEPROM_MAGIC_VALUE = 0xA5; // Magic number for validation

// ----------------------------- Thresholds -----------------------------
// Foreline must be <= this to start turbos (Torr)
static const float FORELINE_SAFE_START_TORR  = 5.0f;

// Foreline overpressure trip (Torr) - system fault
static const float FORELINE_TRIP_TORR = 5.0f;

// Hornet (UHV) overpressure trip (Torr)
static const float HORN_TRIP_RISE_TORR = 7.0e-4f;
static const float HORN_TRIP_FALL_TORR = 5.0e-4f;

// ----------------------------- Gauge Conversion -----------------------------
static const float TO_GAUGE_8V = 8.0f / 5.0f;  // 1.6129 (was 1.6)

// CALIBRATION FIX 2: ADC voltage correction
// Arduino read 3.296V when multimeter measured 3.224V
static const float ADC_CORRECTION = 2.442f / 2.628f;  // 1

// Hornet: P(Torr) = 10^(V - 10)  in the 0–8 V domain
static float hornetVoltsToTorr(float v_domain) {
  return powf(10.0f, v_domain - 10.0f);
}

// Stinger: P(Torr) = 10^(V - 5)  in the 0–8 V domain
static float stingerVoltsToTorr(float v_domain) {
  return powf(10.0f, v_domain - 5.0f);
}

// ----------------------------- Median Filter -----------------------------
// Rejects outlier spikes by taking the median of the last N samples
template<uint8_t N>
struct MedianFilter {
  float buffer[N];
  uint8_t index;
  uint8_t count;

  MedianFilter() : index(0), count(0) {
    for (uint8_t i = 0; i < N; ++i) buffer[i] = 0.0f;
  }

  float step(float x) {
    buffer[index] = x;
    index = (index + 1) % N;
    if (count < N) count++;

    // Copy to temp array for sorting
    float temp[N];
    for (uint8_t i = 0; i < count; ++i) temp[i] = buffer[i];

    // Simple insertion sort (efficient for small N)
    for (uint8_t i = 1; i < count; ++i) {
      float key = temp[i];
      int8_t j = i - 1;
      while (j >= 0 && temp[j] > key) {
        temp[j + 1] = temp[j];
        j--;
      }
      temp[j + 1] = key;
    }

    // Return median
    return temp[count / 2];
  }

  void reset() {
    index = 0;
    count = 0;
    for (uint8_t i = 0; i < N; ++i) buffer[i] = 0.0f;
  }
};

// ----------------------------- EMA Filter -----------------------------
struct EMA {
  float alpha;
  bool  inited;
  float y;

  explicit EMA(float a = 0.1f) : alpha(a), inited(false), y(0.0f) {}

  float step(float x) {
    if (!inited) { y = x; inited = true; return y; }
    y = alpha * x + (1.0f - alpha) * y;
    return y;
  }

  void reset() { inited = false; y = 0.0f; }
};

// Median filters to reject outlier spikes (5 samples = robust against 2 consecutive bad readings)
MedianFilter<5> medianUHV;
MedianFilter<5> medianFore;

EMA emaUHV(0.10f);
EMA emaFore(0.10f);

// ----------------------------- State Machine -----------------------------
enum State : uint8_t {
  STATE_GAUGE_ANALOG_ON_WAIT = 0,
  STATE_IDLE,
  STATE_START_PUMPS,
  STATE_WAIT_PUMPS_OK,
  STATE_ENABLE_HORNET_WAIT,
  STATE_RUN,
  STATE_FAULT
};

enum Cmd : uint8_t {
  CMD_NONE = 0,
  CMD_START,
  CMD_STOP,
  CMD_STATUS,
  CMD_RESET,
  CMD_MAINT_TOGGLE,   // 'M' arming/double-tap
  // MAINT relay commands
  CMD_TG60_ON,
  CMD_TG60_OFF,
  CMD_TG220_ON,
  CMD_TG220_OFF,
  CMD_HORNET_ON,
  CMD_HORNET_OFF,
  CMD_TEST_ON,
  CMD_TEST_OFF,
  CMD_ALL_OFF
};

// ----------------------------- Globals -----------------------------
static State state = STATE_GAUGE_ANALOG_ON_WAIT;

static bool relayTG60   = false;
static bool relayTG220  = false;
static bool relayHornet = false;
static bool relayTest   = false;

// Fault latches (separate for Hornet and system faults)
static bool faultHornet = false;   // Hornet overpressure fault - only turns off Hornet
static bool faultSystem = false;   // Foreline/pump system fault - turns off everything

// Timers
static uint32_t tBoot           = 0;
static uint32_t tLastLog        = 0;

// Logging rolling average (3 samples, 1 Hz each)
static float    logSum_uhvV_adc  = 0.0f;
static float    logSum_foreV_adc = 0.0f;
static float    logSum_uhvTorr   = 0.0f;
static float    logSum_foreTorr  = 0.0f;
static uint8_t  logSampleCount   = 0;

static uint32_t tTG60_okSince   = 0;  // For debouncing OK signal going HIGH
static uint32_t tTG220_okSince  = 0;
static uint32_t tTG60_lostSince = 0;  // For timing OK signal being lost
static uint32_t tTG220_lostSince = 0;

static uint32_t tForeSafeSince  = 0;
static uint32_t tHornetOnSince  = 0;  // When Hornet was last turned on (for overpressure grace period)

// MAINT Mode
static bool maintMode = false;
static uint32_t tMaintArmFirst = 0;
static uint32_t tMaintLastCmd  = 0;

// ----------------------------- Helpers -----------------------------
static const __FlashStringHelper* stateToStr(State s) {
  switch (s) {
    case STATE_GAUGE_ANALOG_ON_WAIT: return F("WARMUP");
    case STATE_IDLE:                 return F("IDLE");
    case STATE_START_PUMPS:          return F("START_PUMPS");
    case STATE_WAIT_PUMPS_OK:        return F("WAIT_PUMPS_OK");
    case STATE_ENABLE_HORNET_WAIT:   return F("ENABLE_HORNET_WAIT");
    case STATE_RUN:                  return F("RUN");
    case STATE_FAULT:                return F("FAULT");
    default:                         return F("UNKNOWN");
  }
}

// ----------------------------- State Persistence Functions -----------------------------
static void saveStateToEEPROM() {
  EEPROM.update(EEPROM_ADDR_MAGIC, EEPROM_MAGIC_VALUE);
  EEPROM.update(EEPROM_ADDR_STATE, (uint8_t)state);
  EEPROM.update(EEPROM_ADDR_RELAY_TG60, relayTG60 ? 1 : 0);
  EEPROM.update(EEPROM_ADDR_RELAY_TG220, relayTG220 ? 1 : 0);
  EEPROM.update(EEPROM_ADDR_RELAY_HORNET, relayHornet ? 1 : 0);
  EEPROM.update(EEPROM_ADDR_MAINT_MODE, maintMode ? 1 : 0);
  EEPROM.update(EEPROM_ADDR_FAULT_HORNET, faultHornet ? 1 : 0);
  EEPROM.update(EEPROM_ADDR_FAULT_SYSTEM, faultSystem ? 1 : 0);
}

static bool restoreStateFromEEPROM() {
  // Check if EEPROM contains valid data
  if (EEPROM.read(EEPROM_ADDR_MAGIC) != EEPROM_MAGIC_VALUE) {
    return false; // No valid saved state
  }

  // Restore state variables
  uint8_t savedState = EEPROM.read(EEPROM_ADDR_STATE);
  relayTG60 = EEPROM.read(EEPROM_ADDR_RELAY_TG60) != 0;
  relayTG220 = EEPROM.read(EEPROM_ADDR_RELAY_TG220) != 0;
  relayHornet = EEPROM.read(EEPROM_ADDR_RELAY_HORNET) != 0;
  maintMode = EEPROM.read(EEPROM_ADDR_MAINT_MODE) != 0;
  faultHornet = EEPROM.read(EEPROM_ADDR_FAULT_HORNET) != 0;
  faultSystem = EEPROM.read(EEPROM_ADDR_FAULT_SYSTEM) != 0;

  // Only restore certain states (safe to resume)
  // Don't restore WARMUP, START_PUMPS, or WAIT_PUMPS_OK (transient states)
  if (savedState == STATE_RUN ||
      savedState == STATE_ENABLE_HORNET_WAIT ||
      savedState == STATE_IDLE) {
    state = (State)savedState;

    // Clear fault flags for non-fault states - they shouldn't have faults
    // (If there was a fault, state would have been STATE_FAULT)
    faultHornet = false;
    faultSystem = false;

    // Apply the restored relay states
    digitalWrite(PIN_RELAY_TG60F, relayTG60 ? LOW : HIGH);
    digitalWrite(PIN_RELAY_TG220F, relayTG220 ? LOW : HIGH);
    digitalWrite(PIN_RELAY_HORNET, relayHornet ? LOW : HIGH);

    return true;
  }

  // For FAULT state, keep the fault flags that were restored above
  if (savedState == STATE_FAULT) {
    state = STATE_FAULT;
    // Fault flags already restored above - keep them
    allOff();
    return true;
  }

  return false;
}

static void clearEEPROMState() {
  EEPROM.update(EEPROM_ADDR_MAGIC, 0x00); // Invalidate saved state
}

static void setTG60(bool on) {
  if (relayTG60 != on) {  // Only update and save if state changed
    relayTG60 = on;
    digitalWrite(PIN_RELAY_TG60F, on ? LOW : HIGH);
    saveStateToEEPROM();
  }
}

static void setTG220(bool on) {
  if (relayTG220 != on) {
    relayTG220 = on;
    digitalWrite(PIN_RELAY_TG220F, on ? LOW : HIGH);
    saveStateToEEPROM();
  }
}

static void setHornet(bool on) {
  if (relayHornet != on) {
    relayHornet = on;
    digitalWrite(PIN_RELAY_HORNET, on ? LOW : HIGH);
    if (on) {
      tHornetOnSince = millis();  // Start grace period timer
    }
    saveStateToEEPROM();
  }
}

static void setTestRelay(bool on) {
  relayTest = on;
  digitalWrite(PIN_RELAY_TEST, on ? LOW : HIGH);
}

static void allOff() {
  setTG60(false);
  setTG220(false);
  setHornet(false);
  setTestRelay(false);
}

// Active-low OK inputs with pullups: LOW means OK
static bool rawTG60_OK()  { return digitalRead(PIN_TG60F_OK)  == LOW; }
static bool rawTG220_OK() { return digitalRead(PIN_TG220F_OK) == LOW; }

static bool debounceTrue(bool raw, uint32_t holdMs, uint32_t &sinceMs) {
  const uint32_t now = millis();
  if (!raw) { sinceMs = 0; return false; }
  if (sinceMs == 0) sinceMs = now;
  return (now - sinceMs) >= holdMs;
}

static float readVoltsAvg(uint8_t pin, uint8_t samples = 32, uint16_t usDelay = 50) {
  (void)analogRead(pin); // throwaway for mux settle
  uint32_t acc = 0;
  for (uint8_t i = 0; i < samples; ++i) {
    acc += (uint16_t)analogRead(pin);
    delayMicroseconds(usDelay);
  }
  const float adc = (float)acc / (float)samples; // 0..1023
  const float volts = (adc * 5.0f) / 1023.0f;
  return volts * ADC_CORRECTION;  // Apply calibration correction
}

static Cmd readSerialCmd() {
  if (!Serial.available()) return CMD_NONE;
  const int c = Serial.read();
  if (c < 0) return CMD_NONE;

  switch ((char)c) {
    case 'S': case 's': return CMD_START;
    case 'X': case 'x': return CMD_STOP;
    case '?':           return CMD_STATUS;
    case 'R': case 'r': return CMD_RESET;
    case 'M': case 'm': return CMD_MAINT_TOGGLE;

    // MAINT relay controls
    case '6': return CMD_TG60_ON;
    case '7': return CMD_TG60_OFF;
    case '2': return CMD_TG220_ON;
    case '3': return CMD_TG220_OFF;
    case 'H': case 'h': return CMD_HORNET_ON;
    case 'J': case 'j': return CMD_HORNET_OFF;

    // TEST relay controls
    case 'O': case 'o': return CMD_TEST_ON;   // O = open (ON)
    case 'C': case 'c': return CMD_TEST_OFF;  // C = close (OFF)

    case '0': return CMD_ALL_OFF;

    default: return CMD_NONE;
  }
}

struct Process {
  float uhvV_adc;     // 0–5 V at Arduino pin (filtered)
  float foreV_adc;    // 0–5 V at Arduino pin (filtered)
  float uhvTorr;
  float foreTorr;
};

static Process P{0, 0, 1e9f, 1e9f};

// ----------------------------- MAINT UX -----------------------------
static void printHelpNormal() {
  Serial.println(F("Commands: S=start, X=stop, H=clear Hornet fault, R=reset all, M=MAINT, ?=status"));
}

static void printHelpMaint() {
  Serial.println(F("MAINT: 6=TG60 ON, 7=TG60 OFF, 2=TG220 ON, 3=TG220 OFF, H=Hornet ON, J=Hornet OFF, O=TEST ON, C=TEST OFF, 0=ALL OFF, M=exit MAINT, ?=help"));
}

static void enterMaint() {
  maintMode = true;
  tMaintLastCmd = millis();
  saveStateToEEPROM();
  Serial.println(F("Entered MAINT mode. Interlocks/sequencing bypassed."));
  printHelpMaint();
}

static void exitMaint() {
  maintMode = false;
  tMaintArmFirst = 0;

  // Determine appropriate state based on current relay positions
  if (relayTG60 && relayTG220) {
    // Pumps are on - go to RUN state
    state = STATE_RUN;
    // Reset the pump OK lost timers since we're entering RUN
    tTG60_lostSince = 0;
    tTG220_lostSince = 0;
    // Clear Hornet fault if Hornet is currently on
    if (relayHornet) {
      faultHornet = false;
    }
    Serial.println(F("Exited MAINT mode. Pumps running, entering RUN state."));
  } else {
    // Pumps are off - go to IDLE
    state = STATE_IDLE;
    Serial.println(F("Exited MAINT mode. State IDLE."));
  }

  saveStateToEEPROM();
}

static void touchMaintCmd() {
  tMaintLastCmd = millis();
}

// ----------------------------- Logging -----------------------------
// Helper function to print float in scientific notation
static void printScientific(float value) {
  if (value == 0.0f) {
    Serial.print(F("0.00e+00"));
    return;
  }

  // Handle negative values
  if (value < 0.0f) {
    Serial.print('-');
    value = -value;
  }

  // Calculate exponent
  int exponent = 0;
  if (value >= 10.0f) {
    while (value >= 10.0f) {
      value /= 10.0f;
      exponent++;
    }
  } else if (value < 1.0f) {
    while (value < 1.0f) {
      value *= 10.0f;
      exponent--;
    }
  }

  // Print mantissa (2 decimal places)
  Serial.print(value, 2);
  Serial.print('e');

  // Print exponent with sign and zero-padding
  if (exponent >= 0) {
    Serial.print('+');
  } else {
    Serial.print('-');
    exponent = -exponent;  // Make positive for printing
  }
  if (exponent < 10) {
    Serial.print('0');
  }
  Serial.print(exponent);
}

static void printCsvHeader() {
  Serial.println(F("ms,uhv_V,fore_V,uhv_Torr,fore_Torr,state,tg60_ok,tg220_ok,rel_tg60,rel_tg220,rel_hornet,rel_test,fault_hornet,fault_system,maint"));
}

static void printCsvLine() {
  Serial.print(millis());
  Serial.print(',');

  Serial.print(P.uhvV_adc, 4);
  Serial.print(',');
  Serial.print(P.foreV_adc, 4);
  Serial.print(',');

  printScientific(P.uhvTorr);
  Serial.print(',');
  printScientific(P.foreTorr);
  Serial.print(',');

  Serial.print(stateToStr(state));
  Serial.print(',');

  Serial.print(rawTG60_OK() ? F("OK") : F("NO"));
  Serial.print(',');
  Serial.print(rawTG220_OK() ? F("OK") : F("NO"));
  Serial.print(',');

  Serial.print(relayTG60 ? 1 : 0);
  Serial.print(',');
  Serial.print(relayTG220 ? 1 : 0);
  Serial.print(',');
  Serial.print(relayHornet ? 1 : 0);
  Serial.print(',');
  Serial.print(relayTest ? 1 : 0);
  Serial.print(',');

  Serial.print(faultHornet ? 1 : 0);
  Serial.print(',');
  Serial.print(faultSystem ? 1 : 0);
  Serial.print(',');

  Serial.println(maintMode ? 1 : 0);
}

static void printCsvLineAveraged(float uhvV_adc, float foreV_adc, float uhvTorr, float foreTorr) {
  Serial.print(millis());
  Serial.print(',');

  Serial.print(uhvV_adc, 4);
  Serial.print(',');
  Serial.print(foreV_adc, 4);
  Serial.print(',');

  printScientific(uhvTorr);
  Serial.print(',');
  printScientific(foreTorr);
  Serial.print(',');

  Serial.print(stateToStr(state));
  Serial.print(',');

  Serial.print(rawTG60_OK() ? F("OK") : F("NO"));
  Serial.print(',');
  Serial.print(rawTG220_OK() ? F("OK") : F("NO"));
  Serial.print(',');

  Serial.print(relayTG60 ? 1 : 0);
  Serial.print(',');
  Serial.print(relayTG220 ? 1 : 0);
  Serial.print(',');
  Serial.print(relayHornet ? 1 : 0);
  Serial.print(',');
  Serial.print(relayTest ? 1 : 0);
  Serial.print(',');

  Serial.print(faultHornet ? 1 : 0);
  Serial.print(',');
  Serial.print(faultSystem ? 1 : 0);
  Serial.print(',');

  Serial.println(maintMode ? 1 : 0);
}

// ----------------------------- Setup -----------------------------
void setup() {
  pinMode(PIN_RELAY_TG60F, OUTPUT);
  pinMode(PIN_RELAY_TG220F, OUTPUT);
  pinMode(PIN_RELAY_HORNET, OUTPUT);
  pinMode(PIN_RELAY_TEST, OUTPUT);

  pinMode(PIN_TG60F_OK, INPUT_PULLUP);
  pinMode(PIN_TG220F_OK, INPUT_PULLUP);

  allOff(); // Start with everything off (will be overridden by restore if needed)

  Serial.begin(115200);

  // Brief delay to allow serial connection to stabilize after reset
  delay(100);

  tBoot = millis();
  tLastLog = 0;

  medianUHV.reset();
  medianFore.reset();
  emaUHV.reset();
  emaFore.reset();

  // Attempt to restore previous state
  bool stateRestored = restoreStateFromEEPROM();

  printCsvHeader();

  if (stateRestored) {
    Serial.println(F("=== STATE RESTORED FROM PREVIOUS SESSION ==="));
    Serial.print(F("Restored State: "));
    Serial.println(stateToStr(state));
    Serial.print(F("TG60: "));
    Serial.print(relayTG60 ? F("ON") : F("OFF"));
    Serial.print(F(", TG220: "));
    Serial.print(relayTG220 ? F("ON") : F("OFF"));
    Serial.print(F(", Hornet: "));
    Serial.println(relayHornet ? F("ON") : F("OFF"));
    if (maintMode) {
      Serial.println(F("Maintenance mode was active."));
      printHelpMaint();
    } else {
      printHelpNormal();
    }
    Serial.println(F("============================================"));
  } else {
    Serial.println(F("=== FRESH START (No previous state) ==="));
    printHelpNormal();
  }

  // Print calibration info
  Serial.println(F("=== CALIBRATION INFO ==="));
  Serial.print(F("Voltage divider ratio: 8V -> 5V, scale factor = "));
  Serial.println(TO_GAUGE_8V, 4);
  Serial.print(F("ADC correction factor: "));
  Serial.println(ADC_CORRECTION, 4);
  Serial.println(F("========================"));
}

// ----------------------------- Loop -----------------------------
void loop() {
  const uint32_t now = millis();

  // --------- Read sensors (always) ---------
  const float uhvV_raw  = readVoltsAvg(PIN_UHV);
  const float foreV_raw = readVoltsAvg(PIN_FORELINE);

  // Apply median filter first to reject outlier spikes, then EMA for smoothing
  const float uhvV_median  = medianUHV.step(uhvV_raw);
  const float foreV_median = medianFore.step(foreV_raw);

  P.uhvV_adc  = emaUHV.step(uhvV_median);
  P.foreV_adc = emaFore.step(foreV_median);

  const float uhvV_domain  = P.uhvV_adc  * TO_GAUGE_8V;
  const float foreV_domain = P.foreV_adc * TO_GAUGE_8V;

  P.uhvTorr  = hornetVoltsToTorr(uhvV_domain);
  P.foreTorr = stingerVoltsToTorr(foreV_domain);

  // --------- Periodic logging ---------
  if ((now - tLastLog) >= LOG_INTERVAL_MS) {
    tLastLog = now;

    // Accumulate 1-second samples to compute a 3-second average
    logSum_uhvV_adc  += P.uhvV_adc;
    logSum_foreV_adc += P.foreV_adc;
    logSum_uhvTorr   += P.uhvTorr;
    logSum_foreTorr  += P.foreTorr;

    if (++logSampleCount >= 3) {
      // Print averaged values every 3 seconds
      printCsvLineAveraged(logSum_uhvV_adc / 3.0f,
                           logSum_foreV_adc / 3.0f,
                           logSum_uhvTorr / 3.0f,
                           logSum_foreTorr / 3.0f);

      logSum_uhvV_adc  = 0.0f;
      logSum_foreV_adc = 0.0f;
      logSum_uhvTorr   = 0.0f;
      logSum_foreTorr  = 0.0f;
      logSampleCount   = 0;
    }

    // Save state periodically (every log interval = 1 second)
    // This ensures state is captured even if reset happens unexpectedly
    saveStateToEEPROM();
  }

  // --------- Read command ---------
  Cmd cmd = readSerialCmd();

  // --------- MAINT arm/toggle ---------
  if (cmd == CMD_MAINT_TOGGLE) {
    if (!maintMode) {
      if (tMaintArmFirst == 0) {
        tMaintArmFirst = now;
        Serial.println(F("MAINT arm requested. Send 'M' again within 5 seconds to enter MAINT."));
      } else if ((now - tMaintArmFirst) <= MAINT_ARM_WINDOW_MS) {
        tMaintArmFirst = 0;
        enterMaint();
      } else {
        tMaintArmFirst = now;
        Serial.println(F("MAINT arm requested. Send 'M' again within 5 seconds to enter MAINT."));
      }
    } else {
      exitMaint();
    }
    return;
  }

  if (!maintMode && tMaintArmFirst != 0 && (now - tMaintArmFirst) > MAINT_ARM_WINDOW_MS) {
    tMaintArmFirst = 0;
  }

  // --------- MAINT behavior ---------
  if (maintMode) {
    if ((now - tMaintLastCmd) > MAINT_IDLE_TIMEOUT_MS) {
      Serial.println(F("MAINT timeout. Outputs forced OFF; exiting MAINT."));
      exitMaint();
      return;
    }

    switch (cmd) {
      case CMD_TG60_ON:     setTG60(true);        touchMaintCmd(); break;
      case CMD_TG60_OFF:    setTG60(false);       touchMaintCmd(); break;
      case CMD_TG220_ON:    setTG220(true);       touchMaintCmd(); break;
      case CMD_TG220_OFF:   setTG220(false);      touchMaintCmd(); break;
      case CMD_HORNET_ON:   setHornet(true);      touchMaintCmd(); break;
      case CMD_HORNET_OFF:  setHornet(false);     touchMaintCmd(); break;
      case CMD_TEST_ON:     setTestRelay(true);   touchMaintCmd(); break;
      case CMD_TEST_OFF:    setTestRelay(false);  touchMaintCmd(); break;
      case CMD_ALL_OFF:     allOff();             touchMaintCmd(); break;

      case CMD_STATUS:
        printHelpMaint();
        touchMaintCmd();
        break;

      case CMD_STOP:
        allOff();
        touchMaintCmd();
        Serial.println(F("MAINT: ALL OFF"));
        break;

      default:
        break;
    }

    // Do not run interlocks/state machine in MAINT
    return;
  }

  // --------- Normal mode: global safety trips ---------

  // Foreline overpressure - system fault, turns off everything
  if (!faultSystem && P.foreTorr > FORELINE_TRIP_TORR) {
    faultSystem = true;
    state = STATE_FAULT;
    allOff();
    saveStateToEEPROM();
    Serial.println(F("FAULT: Foreline overpressure (>5 Torr). All outputs OFF."));
  }

  // Hornet overpressure - only turns off Hornet, pumps continue
  // Only check when Hornet is energized, in RUN state, AND grace period has passed
  // Grace period allows gauge to settle after being turned on
  const bool hornetGracePassed = (tHornetOnSince != 0) && ((now - tHornetOnSince) > HORNET_GRACE_PERIOD_MS);
  if (!faultHornet && state == STATE_RUN && relayHornet && hornetGracePassed && P.uhvTorr > HORN_TRIP_RISE_TORR) {
    faultHornet = true;
    setHornet(false);
    saveStateToEEPROM();
    Serial.println(F("FAULT: Hornet overpressure (>5e-4 Torr). Hornet OFF, pumps continue."));
  }

  // --------- Normal mode: command handling ---------
  if (cmd == CMD_STATUS) {
    printHelpNormal();
    Serial.print(F("State: "));
    Serial.println(stateToStr(state));
    Serial.print(F("Relays: TG60="));
    Serial.print(relayTG60 ? F("ON") : F("OFF"));
    Serial.print(F(", TG220="));
    Serial.print(relayTG220 ? F("ON") : F("OFF"));
    Serial.print(F(", Hornet="));
    Serial.println(relayHornet ? F("ON") : F("OFF"));
    Serial.print(F("Faults: Hornet="));
    Serial.print(faultHornet ? F("YES") : F("NO"));
    Serial.print(F(", System="));
    Serial.println(faultSystem ? F("YES") : F("NO"));
    Serial.print(F("Pressures: Foreline="));
    Serial.print(P.foreTorr, 2);
    Serial.print(F(" Torr, UHV="));
    printScientific(P.uhvTorr);
    Serial.println(F(" Torr"));
  }

  if (cmd == CMD_STOP) {
    allOff();
    state = STATE_IDLE;
    saveStateToEEPROM();
    Serial.println(F("STOP: Outputs OFF, state IDLE."));
  }

  // H command = Clear Hornet fault only (Hornet will auto-enable on next loop if in RUN)
  if (cmd == CMD_HORNET_ON) {
    if (faultHornet) {
      faultHornet = false;
      saveStateToEEPROM();
      Serial.println(F("Hornet fault cleared. Hornet will auto-enable if in RUN."));
    } else {
      Serial.println(F("No Hornet fault to clear."));
    }
  }

  // R command = Reset ALL faults, return to IDLE
  if (cmd == CMD_RESET) {
    if (faultHornet || faultSystem) {
      if (faultSystem && P.foreTorr > FORELINE_SAFE_START_TORR) {
        Serial.println(F("RESET denied: Foreline pressure still too high."));
      } else {
        faultHornet = false;
        faultSystem = false;
        state = STATE_IDLE;
        saveStateToEEPROM();
        Serial.println(F("All faults cleared. State IDLE."));
      }
    } else {
      Serial.println(F("No faults to clear."));
    }
  }

  if (cmd == CMD_START) {
    if (faultSystem) {
      Serial.println(F("START denied: System fault active. Use R to reset."));
    } else if (state != STATE_IDLE && state != STATE_GAUGE_ANALOG_ON_WAIT) {
      Serial.println(F("START denied: Not in IDLE state."));
    } else {
      state = STATE_START_PUMPS;
      tForeSafeSince = 0;
      saveStateToEEPROM();
      Serial.println(F("START requested."));
    }
  }

  // --------- State machine ---------
  switch (state) {
    case STATE_GAUGE_ANALOG_ON_WAIT: {
      allOff();
      if ((now - tBoot) >= GAUGE_WARMUP_MS) {
        state = STATE_IDLE;
        saveStateToEEPROM();
        Serial.println(F("Warmup complete. State IDLE."));
      }
      break;
    }

    case STATE_IDLE: {
      allOff();
      break;
    }

    case STATE_START_PUMPS: {
      // Check foreline is safe before starting pumps
      const bool foreSafe = (P.foreTorr <= FORELINE_SAFE_START_TORR);
      const bool foreSafeStable = debounceTrue(foreSafe, FORELINE_STABLE_MS, tForeSafeSince);

      if (foreSafeStable) {
        setTG60(true);
        setTG220(true);
        tTG60_okSince = 0;
        tTG220_okSince = 0;
        state = STATE_WAIT_PUMPS_OK;
        saveStateToEEPROM();
        Serial.println(F("Pumps commanded ON; waiting for OK signals."));
      } else {
        setTG60(false);
        setTG220(false);
      }
      setHornet(false);
      setTestRelay(false);
      break;
    }

    case STATE_WAIT_PUMPS_OK: {
      setHornet(false);
      setTestRelay(false);

      // Wait for both pump OK signals to be stable
      const bool tg60OkStable  = debounceTrue(rawTG60_OK(),  PUMP_OK_DEBOUNCE_MS, tTG60_okSince);
      const bool tg220OkStable = debounceTrue(rawTG220_OK(), PUMP_OK_DEBOUNCE_MS, tTG220_okSince);

      if (tg60OkStable && tg220OkStable) {
        // Reset the lost timers since pumps are now OK
        tTG60_lostSince = 0;
        tTG220_lostSince = 0;
        state = STATE_RUN;
        saveStateToEEPROM();
        Serial.println(F("Both pumps OK. Entering RUN state."));
      }
      break;
    }

    case STATE_ENABLE_HORNET_WAIT: {
      // This state is no longer used - transition directly to RUN
      state = STATE_RUN;
      saveStateToEEPROM();
      break;
    }

    case STATE_RUN: {
      setTestRelay(false);
      setTG60(true);
      setTG220(true);

      // Monitor pump OK signals - fault if lost for 15 seconds
      const bool tg60Ok = rawTG60_OK();
      const bool tg220Ok = rawTG220_OK();

      // Track how long each pump OK has been lost
      if (tg60Ok) {
        tTG60_lostSince = 0;
      } else if (tTG60_lostSince == 0) {
        tTG60_lostSince = now;
      }

      if (tg220Ok) {
        tTG220_lostSince = 0;
      } else if (tTG220_lostSince == 0) {
        tTG220_lostSince = now;
      }

      // Check if either pump OK has been lost for too long
      const bool tg60Lost = (tTG60_lostSince != 0) && ((now - tTG60_lostSince) > PUMP_OK_LOST_TIMEOUT_MS);
      const bool tg220Lost = (tTG220_lostSince != 0) && ((now - tTG220_lostSince) > PUMP_OK_LOST_TIMEOUT_MS);

      if (tg60Lost || tg220Lost) {
        faultSystem = true;
        state = STATE_FAULT;
        allOff();
        saveStateToEEPROM();
        if (tg60Lost) {
          Serial.println(F("FAULT: TG60 OK lost for 15s. All outputs OFF."));
        } else {
          Serial.println(F("FAULT: TG220 OK lost for 15s. All outputs OFF."));
        }
        break;
      }

      // Auto-enable Hornet if no Hornet fault
      if (!faultHornet) {
        setHornet(true);
      } else {
        setHornet(false);
      }
      break;
    }

    case STATE_FAULT: {
      allOff();
      break;
    }

    default: {
      faultSystem = true;
      state = STATE_FAULT;
      allOff();
      Serial.println(F("FAULT: Unknown state. Outputs OFF."));
      break;
    }
  }
}