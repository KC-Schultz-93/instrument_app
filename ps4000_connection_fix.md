# Fix: PicoScope 4262 connection failure in `services/picoscope_service.py`

## Context

The PicoScope 4262 was not connecting. This was investigated outside this
repo session (via GitHub-synced source read access) and the root cause has
been identified with high confidence. This doc hands off the fix.

**Do not** re-investigate whether to switch Python packages (`pyPicoSDK` vs
`picosdk-python-wrappers`) or rewrite the driver layer in C — both were
already ruled out. The package choice (`picosdk-python-wrappers`, `ps4000`
driver) is correct for the 4262. The bug is narrower than that.

## Root cause

`services/picoscope_service.py` calls the `picosdk.ps4000.ps4000` module
using snake_case, unprefixed method names (e.g. `ps.open_unit(...)`,
`ps.run_block(...)`). That module does **not** define any such names — it
only exposes the raw C driver functions with the `ps4000` prefix intact
(e.g. `ps.ps4000OpenUnit(...)`, `ps.ps4000RunBlock(...)`), confirmed against
the module source and the official `ps4000Examples/ps4000BlockExample.py` in
[picotech/picosdk-python-wrappers](https://github.com/picotech/picosdk-python-wrappers).

Every SDK call in `connect()`, `disconnect()`, `configure_channel()`,
`set_trigger()`, and `run_block()` uses a name that does not exist on the
module. The very first call — `connect()` → `ps.open_unit(...)` — raises
`AttributeError: module 'picosdk.ps4000.ps4000' has no attribute 'open_unit'`
before ever reaching the USB device. This is why the scope never appeared to
connect: it's not a driver/hardware negotiation failure, it fails before it
gets that far.

Likely origin: pyPicoSDK (a separate, newer Pico Technology package) does use
a clean class-based snake_case API, and that calling convention appears to
have been used when writing these calls, while the import line correctly
pulls in `picosdk-python-wrappers` (which never had that convention).

## Required fix

In `services/picoscope_service.py`, rename these calls to their `ps4000`-prefixed
equivalents. Argument order and types were checked against the module source
and are already correct in every case below — this is a rename-only fix, not
a signature change:

| Current call | Correct call |
|---|---|
| `ps.open_unit(...)` | `ps.ps4000OpenUnit(...)` |
| `ps.close_unit(...)` | `ps.ps4000CloseUnit(...)` |
| `ps.set_channel(...)` | `ps.ps4000SetChannel(...)` |
| `ps.set_simple_trigger(...)` | `ps.ps4000SetSimpleTrigger(...)` |
| `ps.run_block(...)` | `ps.ps4000RunBlock(...)` |
| `ps.is_ready(...)` | `ps.ps4000IsReady(...)` |
| `ps.set_data_buffer(...)` | `ps.ps4000SetDataBuffer(...)` |
| `ps.get_values(...)` | `ps.ps4000GetValues(...)` |

Note on `set_data_buffer`: both `ps4000SetDataBuffer` (singular, 4 args:
handle, channel, buffer, bufferLth) and `ps4000SetDataBuffers` (plural, 5
args, separate max/min buffers) exist in the driver. The existing call in
this file already passes exactly 4 arguments matching the singular form, so
use `ps4000SetDataBuffer` (singular) — no other signature change needed.

## Steps

1. Open `services/picoscope_service.py` and apply the 8 renames above.
2. Grep the repo for any other direct calls into the `picosdk` module
   (`rg "ps\.(open_unit|close_unit|set_channel|set_simple_trigger|run_block|is_ready|set_data_buffer|get_values)\("`
   or similar) to confirm `picoscope_service.py` really is the only place
   this pattern occurs, per the module-ownership rule in `CLAUDE.md`
   ("only place that touches the hardware driver"). Fix any other instance
   found using the same table above.
3. Do not modify `services/serial_manager.py`, `pages/pressure_page.py`, or
   `services/data_recorder.py` — out of scope per `CLAUDE.md`.
4. Verify with real hardware: run `tests/test_pico_connection.py` with the
   PicoScope 4262 connected via USB and PicoSDK/PicoScope 6 or 7 drivers
   installed (provides `ps4000.dll` / `libps4000.so`). Confirm
   `test_connect_disconnect`, `test_timebase_resolution`,
   `test_single_block_acquisition`, and `test_multiple_blocks` all pass.
5. If connection still fails after this fix, capture the actual exception/
   traceback from step 4 — that's the next real signal, rather than
   re-guessing at the package or API layer again.

## Separately: UI issues

There were also some UI issues reported alongside the connection problem
(cause/scope not yet diagnosed in this pass). Once the connection fix above
is verified, revisit those separately — they are very likely unrelated to
this bug, since this bug prevents the app from ever reaching a connected
state at all.