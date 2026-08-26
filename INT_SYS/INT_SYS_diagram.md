---
config:
  theme: redux
---
flowchart TD
    subgraph ino ["INT_SYS.ino  —  orchestration (~130 lines)"]
        direction LR
        ino_note["setup() / loop()\nInstantiates all objects\nDispatches to class methods"]
    end

    subgraph hw ["Hardware Layer"]
        direction TB
        Relay["Relay.h\n──────────────\nactive-LOW digital out\nwrite(bool) / get()"]
        Pump["Pump.h\n──────────────\nokStable() debounce 2 s\nlostTimeout() 15 s"]
        Gauge["Gauge.h / Gauge.cpp\n──────────────\n32-sample ADC avg + ADC_CORRECTION\nMedianFilter‹5› → EMA (α=0.10)\nvolts → Torr conversion"]
    end

    subgraph ctrl ["Control Layer"]
        direction TB
        SM["StateMachine.h / .cpp\n──────────────\ncheckSafetyTrips()\nhandleCommand()\nupdate()\nallOff() / snapshot()"]
        MM["MaintenanceMode.h / .cpp\n──────────────\ntwo-tap arm (5 s window)\nidle timeout (10 min)\nforceExit()"]
    end

    subgraph svc ["Static Services"]
        direction TB
        StateManager["StateManager.h / .cpp\n──────────────\nSavedState struct\nsave() / restore() / clear()\nEEPROM addrs 0–7"]
        SerialIf["SerialInterface.h / .cpp\n──────────────\nreadSerialCmd()\nprintCsvLineAveraged()\nprintStartupBanner()"]
    end

    subgraph fnd ["Foundation  (included everywhere)"]
        direction LR
        Config["Config.h\nPins · Timing · Thresholds\nCalibration · EEPROM_Addr"]
        Types["Types.h\nState enum · Cmd enum\nVoltsToPressureFn typedef"]
        Filters["Filters.h  (header-only)\nMedianFilter‹N› template\nEMA struct"]
    end

    ino -->|"owns: 4× Relay\n2× Pump · 2× Gauge"| hw
    ino -->|"owns: StateMachine\nMaintenanceMode"| ctrl
    ino -->|"calls static"| svc

    SM -->|"holds references to"| hw
    SM -->|"calls static"| StateManager

    Gauge -->|uses| Filters

    hw --> fnd
    ctrl --> fnd
    svc --> fnd