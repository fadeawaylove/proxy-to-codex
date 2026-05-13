# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules, collect_data_files


hiddenimports = []
for package in (
    'anyio',
    'customtkinter',
    'fastapi',
    'h11',
    'httpcore',
    'httpx',
    'pydantic',
    'pydantic_core',
    'starlette',
    'uvicorn',
    'websockets',
):
    hiddenimports += collect_submodules(package)

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=collect_data_files('customtkinter'),
    hiddenimports=hiddenimports,
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
    name='proxy-to-codex',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[
        'python*.dll',
        'vcruntime*.dll',
        'ucrtbase.dll',
    ],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico',
)
