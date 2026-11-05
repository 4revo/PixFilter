# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = ['tkinter', 'PIL._tkinter_finder', 'PIL.ImageTk', 'PIL.Image', 'numpy', 'scipy.ndimage', 'scipy.fft', 'scipy.fftpack', 'scipy.signal', 'imagehash', 'pywt']
hiddenimports += collect_submodules('scipy')
hiddenimports += collect_submodules('imagehash')
tmp_ret = collect_all('pywt')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['C:/Users/zhaof/Desktop/PixFilter/main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'pandas', 'PyQt5', 'PyQt6', 'IPython', 'notebook', 'pytest'],
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
    name='PixFilter_v1.0',
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
