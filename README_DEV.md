## instrument_app developer notes

## Running the app

From the repository root that contains the `instrument_app/` package:

```powershell
Set-Location path\to\Codes
py -3.12 -m instrument_app
```

After installing in editable mode, the app can be launched from any
directory:

```powershell
py -3.12 -m pip install -e instrument_app
py -3.12 -m instrument_app
```

Windows users can also run `run.ps1` or `run.bat`; these scripts print a
helpful hint when the package cannot be imported.

## Theming contract

Widgets that need to respond to theme changes should inherit from
`ui.mixins.ThemedMixin` and implement `apply_theme(theme: Theme)`.  The mixin
subscribes to `theme_mgr.themeChanged` and calls `apply_theme` whenever the
user switches themes.

## Adding a new card or panel

1. Implement the widget in `instrument_app/ui/`.
2. Subclass `ThemedMixin` and expose any public Qt signals or properties that
   callers need.
3. Add it to `ui/__init__.py` so pages can import it easily.
4. Compose the widget inside a page (e.g., `pages/pressure_page.py`) and wire
   its signals to the appropriate service.

Cards are small visual blocks such as `PressureCard` or `PumpCard`.  Panels are
slightly larger groups of controls such as `AODOPanel` or `AcquisitionPanel`.
