# -*- mode: python ; coding: utf-8 -*-

import os
import subprocess

block_cipher = None

# Attempt to find Homebrew's portaudio dylib to bundle it
binaries = []
try:
    prefix = subprocess.check_output(["brew", "--prefix", "portaudio"], text=True).strip()
    dylib_path = os.path.join(prefix, "lib", "libportaudio.2.dylib")
    if os.path.exists(dylib_path):
        binaries.append((dylib_path, '.'))
except Exception:
    pass

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=[
        # Add any extra non-code files here if needed
    ],
    hiddenimports=[
        'webrtcvad',
        'customtkinter',
        'faster_whisper',
        'sounddevice',
        'pynput.keyboard._darwin',
        'pynput.mouse._darwin',
    ],
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
    name='WhisperPro',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch='arm64', # or universal2
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='WhisperPro',
)

app = BUNDLE(
    coll,
    name='WhisperPro.app',
    icon=None,
    bundle_identifier='com.whisper.pro.mac',
    info_plist={
        'NSMicrophoneUsageDescription': 'This application needs access to the microphone to transcribe your speech into text.',
        'NSAccessibilityUsageDescription': 'This application requires accessibility permissions to detect global hotkeys (Command+Shift+Space) even when running in the background.',
        'LSUIElement': False,
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleVersion': '1.0.0',
        'CFBundleName': 'Whisper Pro',
    },
)