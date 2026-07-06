# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all


block_cipher = None


def merge_collected(*packages: str):
    datas = []
    binaries = []
    hiddenimports = []
    for package in packages:
        package_datas, package_binaries, package_hiddenimports = collect_all(package)
        datas += package_datas
        binaries += package_binaries
        hiddenimports += package_hiddenimports
    return datas, binaries, hiddenimports


datas, binaries, hiddenimports = merge_collected(
    "PySide6",
    "playwright",
    "openpyxl",
)


a = Analysis(
    ["src/link_glancer/main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LinkGlancer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LinkGlancer",
)

app = BUNDLE(
    coll,
    name="LinkGlancer.app",
    icon=None,
    bundle_identifier="com.linkglancer.app",
)
