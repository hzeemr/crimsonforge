# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for CrimsonForge — single standalone .exe"""

import sys
import os

block_cipher = None
ROOT = SPECPATH
USER_HOME = os.path.expanduser('~')


def collect_project_data(relative_dir: str) -> list[tuple[str, str]]:
    """Recursively bundle the whole project data tree."""
    results = []
    base_dir = os.path.join(ROOT, relative_dir)
    for current_root, _, filenames in os.walk(base_dir):
        dest_dir = os.path.relpath(current_root, ROOT)
        for filename in filenames:
            results.append((os.path.join(current_root, filename), dest_dir))
    return results


def collect_optional_tree(source_dir: str, target_dir: str) -> list[tuple[str, str]]:
    results = []
    if not os.path.isdir(source_dir):
        return results
    for current_root, _, filenames in os.walk(source_dir):
        relative_root = os.path.relpath(current_root, source_dir)
        dest_dir = target_dir if relative_root == '.' else os.path.join(target_dir, relative_root)
        for filename in filenames:
            results.append((os.path.join(current_root, filename), dest_dir))
    return results


DATA_FILES = collect_project_data("data")
DATA_FILES += collect_optional_tree(os.path.join(USER_HOME, '.crimsonforge', 'tools', 'ffmpeg'), os.path.join('tools', 'ffmpeg'))
DATA_FILES += collect_optional_tree(os.path.join(USER_HOME, '.crimsonforge', 'tools', 'vgmstream'), os.path.join('tools', 'vgmstream'))

a = Analysis(
    [os.path.join(ROOT, 'main.py')],
    pathex=[ROOT],
    # The PaChecksum native code now ships as a Python C extension
    # (core/_pa_checksum.cp*-win_amd64.pyd) compiled via MSVC. It is
    # auto-discovered by PyInstaller through the hiddenimport below,
    # so no explicit binaries entry is needed.
    #
    # The previous setup shipped a raw ctypes DLL (pa_checksum.dll)
    # compiled with MinGW gcc. That triggered false-positive virus
    # flags on several users' machines because:
    #   1. The DLL was unsigned.
    #   2. MinGW-compiled binaries share byte-level patterns with
    #      malware loaders, which AV heuristics lock onto.
    #   3. ctypes-loaded standalone DLLs are treated by AV as
    #      unknown low-reputation code, while .pyd files load via
    #      Python's own import machinery and get AV trust paths.
    # Switching to a .pyd built with MSVC fixed the false positives.
    binaries=[],
    datas=DATA_FILES,
    hiddenimports=[
        'core._pa_checksum',
        # AI providers — every provider module referenced by
        # ai.provider_registry so PyInstaller's analyser can't
        # drop any of them. Missing one here causes the app to
        # show "SDK not installed" at runtime even though the
        # SDK is installed in the venv.
        'ai.provider_openai',
        'ai.provider_openai_compat',
        'ai.provider_anthropic',
        'ai.provider_gemini',
        'ai.provider_deepseek',
        'ai.provider_ollama',
        'ai.provider_vllm',
        'ai.provider_mistral',
        'ai.provider_cohere',
        'ai.provider_custom',
        'ai.provider_deepl',
        'ai.translation_engine',
        'ai.pricing_registry',
        # Core
        'core.paloc_parser',
        'core.pamt_parser',
        'core.papgt_manager',
        'core.paz_reader',
        'core.vfs_manager',
        'core.crypto_engine',
        'core.compression_engine',
        'core.checksum_engine',
        'core.font_builder',
        'core.repack_engine',
        'core.script_ranges',
        # Translation
        'translation.translation_state',
        'translation.translation_project',
        'translation.translation_batch',
        'translation.localization_usage_index',
        # UI
        'ui.main_window',
        'ui.tab_translate',
        'ui.tab_explorer',
        'ui.tab_font',
        'ui.tab_settings',
        'ui.tab_about',
        'ui.themes.dark',
        'ui.themes.light',
        'ui.widgets.translation_table',
        # Utils
        'utils.config',
        'utils.logger',
        # Dependencies that PyInstaller sometimes misses
        'lz4.block',
        'lz4.frame',
        'cryptography.hazmat.primitives.ciphers',
        'cryptography.hazmat.primitives.ciphers.algorithms',
        'cryptography.hazmat.backends',
        'fontTools',
        'fontTools.ttLib',
        'fontTools.ttLib.tables',
        'PIL',
        'PIL.Image',
        'chardet',
        'numpy',
        'openai',
        'anthropic',
        'google.genai',
        'cohere',
        'PySide6.QtMultimedia',
        'PySide6.QtMultimediaWidgets',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Unused Python packages
        'tkinter',
        'matplotlib',
        'scipy',
        'pandas',
        'IPython',
        'jupyter',
        'notebook',
        'pytest',
        'unittest',
        'pygments',
        'setuptools',
        'pip',
        'hf_xet',
        'huggingface_hub',
        'tokenizers',
        # Unused Qt modules (saves ~400MB)
        'PySide6.Qt3DAnimation',
        'PySide6.Qt3DCore',
        'PySide6.Qt3DExtras',
        'PySide6.Qt3DInput',
        'PySide6.Qt3DLogic',
        'PySide6.Qt3DRender',
        'PySide6.QtBluetooth',
        'PySide6.QtCharts',
        'PySide6.QtDataVisualization',
        'PySide6.QtDesigner',
        'PySide6.QtGraphs',
        'PySide6.QtGraphsWidgets',
        'PySide6.QtHttpServer',
        'PySide6.QtLocation',
        'PySide6.QtNfc',
        'PySide6.QtPdf',
        'PySide6.QtPdfWidgets',
        'PySide6.QtPositioning',
        'PySide6.QtQuick',
        'PySide6.QtQuick3D',
        'PySide6.QtQuickWidgets',
        'PySide6.QtRemoteObjects',
        'PySide6.QtScxml',
        'PySide6.QtSensors',
        'PySide6.QtSerialBus',
        'PySide6.QtSerialPort',
        'PySide6.QtSpatialAudio',
        'PySide6.QtStateMachine',
        'PySide6.QtSvg',
        'PySide6.QtSvgWidgets',
        'PySide6.QtTest',
        'PySide6.QtTextToSpeech',
        'PySide6.QtUiTools',
        'PySide6.QtVirtualKeyboard',
        'PySide6.QtWebChannel',
        'PySide6.QtWebEngine',
        'PySide6.QtWebEngineCore',
        'PySide6.QtWebEngineWidgets',
        'PySide6.QtWebSockets',
        'PySide6.QtXml',
        'PySide6.QtQml',
        'PySide6.QtOpenGL',
        'PySide6.QtOpenGLWidgets',
        'PySide6.QtDBus',
        'PySide6.QtHelp',
        'PySide6.QtConcurrent',
        'PySide6.QtAsyncio',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

_version_str = "v" + __import__('importlib').import_module('version').APP_VERSION

splash = Splash(
    os.path.join(ROOT, 'splash.png'),
    binaries=a.binaries,
    datas=a.datas,
    text_pos=(240, 258),
    text_size=12,
    text_color='#a6adc8',
    text_default=_version_str,
    minify_script=True,
    always_on_top=True,
)

exe = EXE(
    pyz,
    a.scripts,
    splash,
    splash.binaries,
    a.binaries,
    a.datas,
    [],
    name='CrimsonForge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
