# Fund Valuation Desktop Client Design

## Decision

Build a fully local, single-machine desktop client for Windows and macOS using `pywebview` as the native window shell and the existing FastAPI application as the embedded local service.

The first release preserves the current browser-based workflow and business behavior. It does not add accounts, cloud sync, remote storage, automatic updates, tray persistence, or a redesigned interface.

## Goals

- Keep all existing fund valuation features, APIs, refresh behavior, snapshots, reconciliation, and SQLite-backed data flow.
- Add a desktop entry point that launches by double-click instead of requiring a terminal and browser.
- Support Windows and macOS packaging from the same source layout.
- Keep the current browser entry point usable for development and local debugging.
- Store all user data locally on the same machine.

## Non-Goals

- No multi-device data sharing.
- No cloud database or hosted backend.
- No login, licensing, or user account system.
- No automatic update channel in the first desktop version.
- No tray/background resident mode in the first desktop version.
- No UI redesign beyond desktop-specific startup and error handling.

## Architecture

The desktop app has three layers:

1. Desktop launcher
   - New module under `desktop/`.
   - Finds an available `127.0.0.1` port.
   - Starts the existing FastAPI app through Uvicorn in the current process or a managed background thread.
   - Opens a pywebview window pointed at the local service URL.
   - Shuts down the managed service when the desktop window exits.

2. Existing FastAPI backend
   - Reuses `app.create_app()`.
   - Keeps existing routes and scheduler behavior.
   - Uses the same valuation service, providers, snapshots, reconciliation, and cache rules.

3. Existing static frontend
   - Reuses `web/index.html`, `web/app.js`, and `web/styles.css`.
   - Continues calling `/api/...` on the same local origin.
   - Requires no cross-origin changes because the desktop window loads the embedded local server.

## Data Storage

The first desktop version keeps the current local SQLite model:

- Default data file: `data/funds.db`.
- Existing watchlist, snapshot, and reconciliation records stay compatible.
- No migration is required for the initial desktop shell.

If packaged-app working directories differ from source checkouts, the desktop launcher should resolve the data directory explicitly so data is not accidentally written into a temporary bundle extraction directory.

## Startup Flow

1. User opens the desktop executable or app bundle.
2. Launcher chooses an available loopback port.
3. Launcher starts the FastAPI app with scheduler enabled.
4. Launcher polls `/api/health` until the service is ready or a timeout is reached.
5. Launcher opens the desktop window at `http://127.0.0.1:<port>/`.
6. Window close triggers service shutdown.

## Error Handling

The desktop launcher should provide clear local failure feedback:

- If no local port can be allocated, show a startup error dialog.
- If the service fails to become healthy, show a startup error dialog with the log path.
- If imports or packaged dependencies are missing, show a startup error dialog rather than failing silently.
- Write desktop startup logs under `logs/desktop.log`.

The existing application-level API errors and page-level messages remain unchanged.

## Packaging

Use PyInstaller for first-version packaging:

- Windows output: a double-clickable executable or app folder.
- macOS output: an `.app` bundle.
- Include `web/`, required Python modules, and runtime dependencies.
- Keep build scripts separate from existing local-service scripts.

Expected files:

- `desktop/main.py`
- `desktop/__init__.py`
- `desktop/build-windows.ps1`
- `desktop/build-macos.sh`
- Optional PyInstaller spec file if dependency discovery needs explicit hidden imports or bundled data files.

## Testing

Verification should cover both unchanged behavior and the new desktop shell:

- Run the existing unit test suite: `python -m unittest discover -s tests -v`.
- Compile-check Python modules touched by the desktop work.
- Keep `node --check web/app.js` for the existing frontend.
- Add focused tests for port selection and URL construction if the launcher logic is separated into testable helpers.
- Run a local desktop smoke test on Windows: launcher starts, `/api/health` succeeds, window opens, app exits cleanly.
- For macOS, verify packaging command structure in source and perform a real smoke test on a macOS machine before claiming macOS release readiness.

## Rollout Plan

1. Add desktop runtime dependencies.
2. Add a small desktop launcher that wraps the existing FastAPI app.
3. Add startup logging and health polling.
4. Add Windows and macOS build scripts.
5. Update README with desktop run and package instructions.
6. Run tests and Windows smoke verification.

## Open Constraints

- macOS packaging cannot be fully verified from the current Windows machine.
- First-version PyInstaller hidden imports may need adjustment after an actual package build.
- Code signing and notarization are outside the first release scope.
