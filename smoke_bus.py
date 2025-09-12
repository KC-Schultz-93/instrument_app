from __future__ import annotations

import sys
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

from instrument_app.core.bus import bus
from instrument_app.core.app_context import ctx


def main():
    app = QApplication(sys.argv)
    ions_seen = {"n": 0}

    def on_ions(lst):
        try:
            ions_seen["n"] += len(list(lst))
        except Exception:
            pass

    bus.ions_batch.connect(on_ions)

    # Start synthetic for a quick smoke
    ctx.sources.start_synthetic(fs_hz=200_000.0, n_samples=16_384, period_ms=100)

    def stop():
        ctx.sources.stop()
        print(f"IONS_SEEN={ions_seen['n']}")
        print("SMOKE_BUS_OK" if ions_seen["n"] > 0 else "SMOKE_BUS_NO_IONS")
        app.quit()

    QTimer.singleShot(1500, stop)
    app.exec_()


if __name__ == "__main__":
    main()

