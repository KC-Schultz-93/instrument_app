#ifndef GAUGE_H
#define GAUGE_H

#include <Arduino.h>
#include "Filters.h"
#include "Types.h"

// Pressure-to-voltage conversion functions (gauge domain: 0–8 V).
// Hornet: P(Torr) = 10^(V - 10)
float hornetVoltsToTorr(float v_domain);
// Stinger: P(Torr) = 10^(V - 5)
float stingerVoltsToTorr(float v_domain);

// Encapsulates one analog pressure gauge: ADC pin, median + EMA filters,
// and a volt-to-pressure conversion function.
struct Gauge {
  uint8_t           pin;
  MedianFilter<5>   median;
  EMA               ema;
  VoltsToPressureFn toTorr;
  float             lastVolts;
  float             lastTorr;

  Gauge(uint8_t p, float alpha, VoltsToPressureFn fn)
    : pin(p), ema(alpha), toTorr(fn), lastVolts(0.0f), lastTorr(1e9f) {}

  void reset() { median.reset(); ema.reset(); }
  void update();

  float volts() const { return lastVolts; }
  float torr()  const { return lastTorr; }
};

#endif // GAUGE_H
