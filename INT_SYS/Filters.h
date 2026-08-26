#ifndef FILTERS_H
#define FILTERS_H

#include <Arduino.h>

// ============================================================================
// MEDIAN FILTER (insertion-sort, ring buffer, header-only template)
// ============================================================================

template<uint8_t N>
struct MedianFilter {
  float   buffer[N];
  uint8_t index;
  uint8_t count;

  MedianFilter() : index(0), count(0) {
    for (uint8_t i = 0; i < N; ++i) buffer[i] = 0.0f;
  }

  float step(float x) {
    buffer[index] = x;
    index = (index + 1) % N;
    if (count < N) count++;

    float temp[N];
    for (uint8_t i = 0; i < count; ++i) temp[i] = buffer[i];

    for (uint8_t i = 1; i < count; ++i) {
      float  key = temp[i];
      int8_t j   = i - 1;
      while (j >= 0 && temp[j] > key) { temp[j + 1] = temp[j]; j--; }
      temp[j + 1] = key;
    }

    return temp[count / 2];
  }

  void reset() {
    index = 0;
    count = 0;
    for (uint8_t i = 0; i < N; ++i) buffer[i] = 0.0f;
  }
};

// ============================================================================
// EXPONENTIAL MOVING AVERAGE FILTER
// ============================================================================

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

#endif // FILTERS_H
