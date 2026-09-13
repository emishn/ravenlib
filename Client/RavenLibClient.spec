# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


CLIENT_DIR = Path(SPECPATH).resolve()


a = Analysis(
    [str(CLIENT_DIR / "web_client_window.py")],
    pathex=[str(CLIENT_DIR)],
    binaries=[],
    datas=[(str(CLIENT_DIR / "webui"), "webui")],
    hiddenimports=collect_submodules("webview"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="RavenLibClient",
    icon=str(CLIENT_DIR / "ravenfall_code.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
