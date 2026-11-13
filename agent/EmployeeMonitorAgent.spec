# -*- mode: python ; coding: utf-8 -*-
# How the agent is packed into one .exe. build.bat uses this file:
#     python -m PyInstaller --noconfirm --clean EmployeeMonitorAgent.spec

from PyInstaller.utils.hooks import collect_submodules

# pynput loads its real backend lazily (pynput.mouse._win32, pynput.keyboard._win32),
# so collect every submodule - otherwise remote control drops out of the built .exe.
hidden = ['mss', 'PIL', 'PIL.ImageTk', 'PIL._tkinter_finder', 'psutil', 'tkinter', 'winsound', 'truststore']
hidden += collect_submodules('pynput')

# Parts that the libraries would bring along "just in case" and the agent never uses
# (each of them is only tried, inside a try/except, by the library that names it).
# Without them the .exe, and what it unpacks at every start, is about half the size.
unused = [
    'numpy',                                         # Pillow's optional array support (27 MB)
    'cryptography', 'OpenSSL', 'bcrypt', 'cffi', '_cffi_backend',   # requests' optional pyOpenSSL path
    'yaml', 'zstandard', 'brotli', 'brotlicffi',     # optional encodings
    'PIL._avif', 'PIL.AvifImagePlugin',              # AVIF pictures (8 MB); notices are JPG/PNG/GIF/WebP
]

a = Analysis(
    ['agent.py'],
    pathex=[],
    binaries=[],
    # Marks the folder the .exe unpacks into, so a later start can clear left-over ones.
    datas=[('em_agent.marker', '.')],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=unused,
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
    name='EmployeeMonitorAgent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,              # packed binaries are what antivirus programs distrust most
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
