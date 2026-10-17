# PyInstaller spec for the macOS app. Build with packaging/build_macos.sh.
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    (os.path.join(ROOT, "coopa_trada/web/templates"), "coopa_trada/web/templates"),
    (os.path.join(ROOT, "coopa_trada/web/static"), "coopa_trada/web/static"),
    (os.path.join(ROOT, "config/rules.example.yaml"), "config"),
    (os.path.join(ROOT, "config/asset_classes.yaml"), "config"),
    (os.path.join(ROOT, "data/signals.example.csv"), "data"),
    (os.path.join(ROOT, ".env.example"), "."),
]
# Apple Vision text reader, compiled by build_macos.sh so Cooper needs no Xcode tools.
binaries = [(os.path.join(ROOT, "build/ocr/coopa-recognize"), "ocr")]
hiddenimports = collect_submodules("coopa_trada") + collect_submodules("schwab")
for package in ("exchange_calendars",):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

a = Analysis(
    [os.path.join(ROOT, "packaging/launch.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "IPython", "matplotlib"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Coopa Trada",
    console=False,          # double-click app: no terminal window
    argv_emulation=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Coopa Trada")
app = BUNDLE(
    coll,
    name="Coopa Trada.app",
    bundle_identifier="local.coopa-trada.app",
    info_plist={
        "CFBundleShortVersionString": "0.1.0",
        # No Dock icon: the app has no window of its own. Quit from the web page.
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    },
)
