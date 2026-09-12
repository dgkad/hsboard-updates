# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['pc\\board.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['paho.mqtt', 'PIL.Image', 'PIL.ImageDraw', 'PIL.ImageFilter', 'PIL.ImageGrab', 'PIL.ImageTk', 'hashlib', 'json', 'argparse'],
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
    [],
    exclude_binaries=True,
    name='班级留言板',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
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
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='班级留言板',
)
