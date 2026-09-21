
BAUD_RATE = 115200
# INT_SYS.ino prints one averaged CSV line every LOG_INTERVAL_MS (1000ms) x 3
# samples = 3000ms (see INT_SYS/Config.h, INT_SYS.ino). Keep these in step with
# that cadence, or most polls just time out doing nothing.
READ_PERIOD_MS = 3000           # polling cadence, matched to firmware print rate
SERIAL_READ_TIMEOUT_S = 3.5     # pyserial readline() timeout, with margin
CSV_BASENAME = "vacuum_log"    # final name gets timestamp suffix
