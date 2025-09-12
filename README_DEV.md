## instrument_app developer notes

## Running the app

Recommended: use Python 3.12 and a virtual environment.

```powershell
# Create venv (first time)
py -3.12 -m venv .venv

# Activate
. .venv\Scripts\Activate.ps1   # PowerShell

# Install deps
python -m pip install -U pip
python -m pip install -r requirements.txt
```

Run the app during development (no install required):

```powershell
# Option A: from anywhere
./run.ps1

# Option B: from the repo parent folder
Set-Location ..    # go to the folder that contains 'instrument_app'
py -3.12 -m instrument_app
```

Install in editable mode (creates an `instrument-app` command and adds deps):

```powershell
python -m pip install -e .
instrument-app
```

Windows users can also run `run.ps1` or `run.bat` from any directory; the
scripts resolve the repository root automatically.

## Theming contract

Widgets that need to respond to theme changes should inherit from
`ui.mixins.ThemedMixin` and implement `apply_theme(theme: Theme)`.  The mixin
subscribes to `theme_mgr.themeChanged` and calls `apply_theme` whenever the
user switches themes.

Note: The legacy package `instrument_app.widgets` is deprecated. Use
`instrument_app.ui` for all reusable UI components. A temporary shim remains to
ease migration, but it may be removed in a future release.

## Adding a new card or panel

1. Implement the widget in `instrument_app/ui/`.
2. Subclass `ThemedMixin` and expose any public Qt signals or properties that
   callers need.
3. Add it to `ui/__init__.py` so pages can import it easily.
4. Compose the widget inside a page (e.g., `pages/pressure_page.py`) and wire
   its signals to the appropriate service.

Cards are small visual blocks such as `PressureCard` or `PumpCard`.  Panels are
slightly larger groups of controls such as `AODOPanel` or `AcquisitionPanel`.
