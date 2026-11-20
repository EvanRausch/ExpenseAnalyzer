# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for BudgetTracker (macOS onedir bundle)
#
# Build with:
#   pyinstaller BudgetTracker.spec
#
# Output: dist/BudgetTracker.app
# Keep dist/BudgetTracker.app and your project's data/ folder in the same
# parent directory so the app can find enriched_transactions.csv.

from pathlib import Path

project_root = Path(SPECPATH)

a = Analysis(
    [str(project_root / "src" / "budget_ui.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[],           # data/ folder stays external (written to by watchdog)
    hiddenimports=[
        "PySide6.QtCharts",
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "PySide6.QtOpenGL",
        "PySide6.QtOpenGLWidgets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BudgetTracker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # no terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="BudgetTracker",
)

app = BUNDLE(
    coll,
    name="BudgetTracker.app",
    icon=None,              # set to "path/to/icon.icns" if you have one
    bundle_identifier="com.personal.budgettracker",
    info_plist={
        "NSHighResolutionCapable": True,
        "CFBundleShortVersionString": "1.0.0",
    },
)
