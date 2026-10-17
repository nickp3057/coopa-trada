# What a Windows build would need

Not built. This lists what is already portable, what must change, and what has to
be tested on a real Windows machine before Cooper could use it.

## Already portable

- **The app itself.** Flask UI, engine, SQLite store, and schwab-py are pure Python
  or have Windows wheels. The launcher (`coopa_trada/desktop.py`) treats the folder
  that contains the executable as the home folder when it is not inside a `.app`.
- **CSV upload** works everywhere. Picture upload already turns itself off
  outside macOS, with a plain message ("only works on a Mac … upload the CSV file
  instead"), and the file picker is limited to `.csv` (`imports.image_reader_status`).
- **Run lock.** `coopa_trada/runlock.py` has a Windows branch using `msvcrt.locking`
  on byte 0, with the owner record after it so it stays readable. It is **untested
  on Windows**.

## Must change

1. **Build on Windows.** PyInstaller does not cross-compile. Use a Windows 10/11
   machine or CI runner with the same Python version and `requirements.txt`.
2. **A Windows spec file** (or a platform branch in `packaging/coopa_trada.spec`):
   no `BUNDLE` step, no `swiftc` helper, `console=False`, an `.ico` icon, and
   **one-folder** output (not one-file). One-file builds unpack to a temp folder on
   every launch, are slower, and trip antivirus more often.
3. **Time zones.** Windows has no system time-zone database. Add the `tzdata` package
   to `requirements.txt` so `zoneinfo` (used for New York market times) works, then
   test a run against the real XNYS close.
4. **Picture reading (optional).** Apple Vision does not exist on Windows. Options:
   Windows' built-in OCR (`Windows.Media.Ocr` via the `winrt` Python packages; no
   install for Cooper) or Tesseract (a separate install, usually less accurate on
   screenshots). Either needs its own accuracy check against real pick screenshots.
   Without it, Windows is CSV-only.
5. **`packaging/verify_app.py`** uses `ditto` and the `.app` layout. Add a Windows
   branch that copies the build folder and runs `Coopa Trada.exe`.
6. **Token file privacy.** `os.chmod(0o600)` does nothing useful on Windows. The token
   should live in the user's own folder (the default when the app sits in Documents);
   consider encrypting it with Windows DPAPI.

## Must test on Windows

- The full `pytest` suite. The lock tests use real subprocesses and `os._exit`,
  which work on Windows, but the `msvcrt` locking path has never run.
- The launcher: first-run files, `.env` next to the exe, browser opening, double-click
  while already running, and Quit.
- A dry run end to end, then the live gates with `verify_app.py`.

## Cooper-facing differences

- **SmartScreen** shows "Windows protected your PC" for an unsigned app (More info →
  Run anyway). A code-signing certificate removes this; it costs money and takes setup.
- **`.env` in Notepad**: Notepad saves as `.env.txt` unless "Save as type" is set to
  "All files". Have the app accept `env.txt` as well, or provide a settings page for keys.
- **Sleep**: Windows sleep stops monitoring, as on a Mac. Either have the app keep
  the PC awake during a run (`SetThreadExecutionState`), or tell Cooper to change
  the power settings.
