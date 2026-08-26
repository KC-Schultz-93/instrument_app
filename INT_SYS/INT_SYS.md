# INT_SYS/CLAUDE.md

## What this folder is

Arduino C++ firmware for the instrument's embedded controller. This is a completely separate domain from the Python application layer. Changes here require uploading via the Arduino IDE using `INT_SYS.ino` as the entry point.

**Do not mix Python application logic into this folder, and do not call Python from here.**

---

## Upload

Open `INT_SYS.ino` in Arduino IDE and upload to the target board.

---

## Module ownership

| File | Owns |
|---|---|
| `StateMachine.cpp/.h` | Top-level state machine — controls overall instrument operating mode |
| `StateManager.cpp/.h` | Manages transitions between states; consult before adding new states |
| `MaintenanceMode.cpp/.h` | Behavior when instrument is in maintenance/diagnostic mode |
| `SerialInterface.cpp/.h` | All serial communication with the Python app — message framing and parsing |
| `Gauge.cpp/.h` | Pressure gauge hardware interaction |
| `Pump.h` | Pump control interface |
| `Relay.h` | Relay switching |
| `Filters.h` | Signal filtering utilities |
| `Config.h` | Hardware pin assignments, timing constants, compile-time config |
| `Types.h` | Shared enums and structs used across modules |

---

## Serial protocol

The Python side (`Services/SerialComms.py`) and this firmware communicate over serial. Both ends must agree on message format. Check `serial_debug.log` in `App/` for recent message traffic.

**If you change the serial protocol here, the Python `SerialComms.py` must be updated to match — and vice versa.**

---

## Key rules

- All state transitions go through `StateManager` — do not add ad-hoc state changes elsewhere.
- All serial message handling goes through `SerialInterface` — do not scatter `Serial.print` calls.
- `Config.h` is the single source of truth for pin numbers and timing constants.
- `Types.h` is the single source of truth for shared enums — add new types here, not inline.
- The Python app layer is **unaware of internal firmware states** beyond what is explicitly sent over serial.