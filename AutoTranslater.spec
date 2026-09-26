# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


project_root = Path(SPECPATH)


def include_google_runtime(module_name):
    excluded = (
        "google.genai.tests",
        "google.genai._test_api_client",
        "google.genai.local_tokenizer",
        "google.genai._local_tokenizer_loader",
    )
    return not any(
        module_name == prefix or module_name.startswith(prefix + ".")
        for prefix in excluded
    )


google_datas = collect_data_files(
    "google.genai",
    excludes=["tests/**"],
)
google_hiddenimports = collect_submodules(
    "google.genai",
    filter=include_google_runtime,
)

analysis = Analysis(
    [str(project_root / "gui_main.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        (str(project_root / "resources"), "resources"),
        *google_datas,
        *collect_data_files("tzdata"),
    ],
    hiddenimports=[
        *google_hiddenimports,
        "bs4.builder._lxml",
        "lxml.etree",
        # Playwright's upstream hook includes its driver (not a full browser).
        "playwright.sync_api",
        "keyring.backends.Windows",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "mypy", "ruff", "developer"],
    noarchive=False,
    optimize=0,
)

python_archive = PYZ(analysis.pure)

executable = EXE(
    python_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="AutoTranslater",
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
    contents_directory="_internal",
)

distribution = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="AutoTranslater",
)
