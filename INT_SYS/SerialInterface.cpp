#include "SerialInterface.h"

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

Cmd SerialInterface::readSerialCmd() {
  if (!Serial.available()) return CMD_NONE;
  const int c = Serial.read();
  if (c < 0) return CMD_NONE;

  switch ((char)c) {
    case 'S': case 's': return CMD_START;
    case 'X': case 'x': return CMD_STOP;
    case '?':           return CMD_STATUS;
    case 'R': case 'r': return CMD_RESET;
    case 'M': case 'm': return CMD_MAINT_TOGGLE;
    // 'H' = clear Hornet fault (normal mode) or Hornet relay ON (MAINT mode)
    case 'H': case 'h': return CMD_HORNET_ON;
    case 'J': case 'j': return CMD_HORNET_OFF;
    // MAINT relay controls
    case '6':           return CMD_TG60_ON;
    case '7':           return CMD_TG60_OFF;
    case '2':           return CMD_TG220_ON;
    case '3':           return CMD_TG220_OFF;
    case 'O': case 'o': return CMD_TEST_ON;
    case 'C': case 'c': return CMD_TEST_OFF;
    case '0':           return CMD_ALL_OFF;
    default:            return CMD_NONE;
  }
}

void SerialInterface::printScientific(float value) {
  if (value == 0.0f) { Serial.print(F("0.00e+00")); return; }

  if (value < 0.0f) { Serial.print('-'); value = -value; }

  int exponent = 0;
  if (value >= 10.0f) {
    while (value >= 10.0f) { value /= 10.0f; exponent++; }
  } else if (value < 1.0f) {
    while (value < 1.0f)   { value *= 10.0f; exponent--; }
  }

  Serial.print(value, 2);
  Serial.print('e');
  if (exponent >= 0) { Serial.print('+'); } else { Serial.print('-'); exponent = -exponent; }
  if (exponent < 10) Serial.print('0');
  Serial.print(exponent);
}

void SerialInterface::printCsvHeader() {
  Serial.println(F("ms,uhv_V,fore_V,uhv_Torr,fore_Torr,state,"
                   "tg60_ok,tg220_ok,"
                   "rel_tg60,rel_tg220,rel_hornet,rel_test,"
                   "fault_hornet,fault_system,maint"));
}

void SerialInterface::printCsvLineAveraged(uint32_t ms,
                                           float uhvV,   float foreV,
                                           float uhvTorr, float foreTorr,
                                           State state,
                                           bool tg60Ok, bool tg220Ok,
                                           bool relTG60, bool relTG220,
                                           bool relHornet, bool relTest,
                                           bool faultHornet, bool faultSystem,
                                           bool maintMode) {
  Serial.print(ms);
  Serial.print(',');
  Serial.print(uhvV, 4);
  Serial.print(',');
  Serial.print(foreV, 4);
  Serial.print(',');
  printScientific(uhvTorr);
  Serial.print(',');
  printScientific(foreTorr);
  Serial.print(',');
  Serial.print(stateToStr(state));
  Serial.print(',');
  Serial.print(tg60Ok  ? F("OK") : F("NO"));
  Serial.print(',');
  Serial.print(tg220Ok ? F("OK") : F("NO"));
  Serial.print(',');
  Serial.print(relTG60   ? 1 : 0);
  Serial.print(',');
  Serial.print(relTG220  ? 1 : 0);
  Serial.print(',');
  Serial.print(relHornet ? 1 : 0);
  Serial.print(',');
  Serial.print(relTest   ? 1 : 0);
  Serial.print(',');
  Serial.print(faultHornet ? 1 : 0);
  Serial.print(',');
  Serial.print(faultSystem ? 1 : 0);
  Serial.print(',');
  Serial.println(maintMode ? 1 : 0);
}

void SerialInterface::printHelpNormal() {
  Serial.println(F("Commands: S=start, X=stop, H=clear Hornet fault, R=reset all, M=MAINT, ?=status"));
}

void SerialInterface::printHelpMaint() {
  Serial.println(F("MAINT: 6=TG60 ON, 7=TG60 OFF, 2=TG220 ON, 3=TG220 OFF, "
                   "H=Hornet ON, J=Hornet OFF, O=TEST ON, C=TEST OFF, "
                   "0=ALL OFF, M=exit MAINT, ?=help"));
}

void SerialInterface::printStatus(State state,
                                  bool relTG60, bool relTG220, bool relHornet,
                                  bool faultHornet, bool faultSystem,
                                  float foreTorr, float uhvTorr) {
  printHelpNormal();
  Serial.print(F("State: ")); Serial.println(stateToStr(state));
  Serial.print(F("Relays: TG60="));
  Serial.print(relTG60   ? F("ON") : F("OFF"));
  Serial.print(F(", TG220="));
  Serial.print(relTG220  ? F("ON") : F("OFF"));
  Serial.print(F(", Hornet="));
  Serial.println(relHornet ? F("ON") : F("OFF"));
  Serial.print(F("Faults: Hornet="));
  Serial.print(faultHornet ? F("YES") : F("NO"));
  Serial.print(F(", System="));
  Serial.println(faultSystem ? F("YES") : F("NO"));
  Serial.print(F("Pressures: Foreline="));
  Serial.print(foreTorr, 2);
  Serial.print(F(" Torr, UHV="));
  printScientific(uhvTorr);
  Serial.println(F(" Torr"));
}

void SerialInterface::printStartupBanner(bool restored, State state,
                                         bool relTG60, bool relTG220, bool relHornet,
                                         bool maintMode,
                                         float toGauge8V, float adcCorrection) {
  if (restored) {
    Serial.println(F("=== STATE RESTORED FROM PREVIOUS SESSION ==="));
    Serial.print(F("Restored State: ")); Serial.println(stateToStr(state));
    Serial.print(F("TG60: "));
    Serial.print(relTG60  ? F("ON") : F("OFF"));
    Serial.print(F(", TG220: "));
    Serial.print(relTG220 ? F("ON") : F("OFF"));
    Serial.print(F(", Hornet: "));
    Serial.println(relHornet ? F("ON") : F("OFF"));
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

  Serial.println(F("=== CALIBRATION INFO ==="));
  Serial.print(F("Voltage divider ratio: 8V -> 5V, scale factor = "));
  Serial.println(toGauge8V, 4);
  Serial.print(F("ADC correction factor: "));
  Serial.println(adcCorrection, 4);
  Serial.println(F("========================"));
}
