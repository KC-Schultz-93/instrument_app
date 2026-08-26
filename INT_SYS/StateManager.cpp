#include "StateManager.h"
#include "Config.h"
#include <EEPROM.h>

void StateManager::save(const SavedState& s) {
  EEPROM.update(EEPROM_Addr::MAGIC,        EEPROM_Addr::MAGIC_VALUE);
  EEPROM.update(EEPROM_Addr::STATE,        (uint8_t)s.state);
  EEPROM.update(EEPROM_Addr::RELAY_TG60,   s.relayTG60   ? 1 : 0);
  EEPROM.update(EEPROM_Addr::RELAY_TG220,  s.relayTG220  ? 1 : 0);
  EEPROM.update(EEPROM_Addr::RELAY_HORNET, s.relayHornet ? 1 : 0);
  EEPROM.update(EEPROM_Addr::MAINT_MODE,   s.maintMode   ? 1 : 0);
  EEPROM.update(EEPROM_Addr::FAULT_HORNET, s.faultHornet ? 1 : 0);
  EEPROM.update(EEPROM_Addr::FAULT_SYSTEM, s.faultSystem ? 1 : 0);
}

bool StateManager::restore(SavedState& s) {
  if (EEPROM.read(EEPROM_Addr::MAGIC) != EEPROM_Addr::MAGIC_VALUE) return false;

  uint8_t savedState = EEPROM.read(EEPROM_Addr::STATE);

  s.relayTG60   = EEPROM.read(EEPROM_Addr::RELAY_TG60)   != 0;
  s.relayTG220  = EEPROM.read(EEPROM_Addr::RELAY_TG220)  != 0;
  s.relayHornet = EEPROM.read(EEPROM_Addr::RELAY_HORNET) != 0;
  s.maintMode   = EEPROM.read(EEPROM_Addr::MAINT_MODE)   != 0;
  s.faultHornet = EEPROM.read(EEPROM_Addr::FAULT_HORNET) != 0;
  s.faultSystem = EEPROM.read(EEPROM_Addr::FAULT_SYSTEM) != 0;

  // Only restore safe states; clear fault flags for non-fault states.
  if (savedState == STATE_RUN ||
      savedState == STATE_ENABLE_HORNET_WAIT ||
      savedState == STATE_IDLE) {
    s.state       = (State)savedState;
    s.faultHornet = false;
    s.faultSystem = false;
    return true;
  }

  if (savedState == STATE_FAULT) {
    s.state = STATE_FAULT;
    return true;
  }

  return false;
}

void StateManager::clear() {
  EEPROM.update(EEPROM_Addr::MAGIC, 0x00);
}
