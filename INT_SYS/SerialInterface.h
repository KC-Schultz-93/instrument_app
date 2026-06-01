#ifndef SERIAL_INTERFACE_H
#define SERIAL_INTERFACE_H

#include <Arduino.h>
#include "Types.h"

class SerialInterface {
public:
  static Cmd  readSerialCmd();
  static void printScientific(float value);

  static void printCsvHeader();
  static void printCsvLineAveraged(uint32_t ms,
                                   float uhvV,   float foreV,
                                   float uhvTorr, float foreTorr,
                                   State state,
                                   bool tg60Ok, bool tg220Ok,
                                   bool relTG60, bool relTG220,
                                   bool relHornet, bool relTest,
                                   bool faultHornet, bool faultSystem,
                                   bool maintMode);

  static void printHelpNormal();
  static void printHelpMaint();

  static void printStatus(State state,
                          bool relTG60, bool relTG220, bool relHornet,
                          bool faultHornet, bool faultSystem,
                          float foreTorr, float uhvTorr);

  static void printStartupBanner(bool restored, State state,
                                 bool relTG60, bool relTG220, bool relHornet,
                                 bool maintMode,
                                 float toGauge8V, float adcCorrection);
};

#endif // SERIAL_INTERFACE_H
