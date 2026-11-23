#include "Gauge.h"
#include "Config.h"
#include <math.h>

// Single throwaway read lets the ADC mux settle before averaging.
static float readVoltsAvg(uint8_t pin, uint8_t samples = 32, uint16_t usDelay = 50) {
  (void)analogRead(pin);
  uint32_t acc = 0;
  for (uint8_t i = 0; i < samples; ++i) {
    acc += (uint16_t)analogRead(pin);
    delayMicroseconds(usDelay);
  }
  const float adc   = (float)acc / (float)samples;
  const float volts = (adc * 5.0f) / 1023.0f;
  return volts * Calibration::ADC_CORRECTION;
}

float hornetVoltsToTorr(float v_domain) {
  return powf(10.0f, v_domain - 10.0f);
}

float stingerVoltsToTorr(float v_domain) {
  return powf(10.0f, v_domain - 5.0f);
}

void Gauge::prime(uint16_t settleMs, uint8_t fillSamples) {
  const uint32_t t0 = millis();
  while ((millis() - t0) < settleMs) {
    (void)readVoltsAvg(pin);  // throwaway reads while the ADC/reference settles
    delay(20);
  }
  reset();
  for (uint8_t i = 0; i < fillSamples; ++i) update();
}

void Gauge::update() {
  float raw = readVoltsAvg(pin);
  float med = median.step(raw);
  lastVolts = ema.step(med);
  lastTorr  = toTorr(lastVolts * Calibration::TO_GAUGE_8V);
}
