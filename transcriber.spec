# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs


root = Path(SPECPATH)
analysis = Analysis(
    [str(root / "app.py")],
    binaries=collect_dynamic_libs("sherpa_onnx") + collect_dynamic_libs("ctranslate2"),
    datas=collect_data_files("faster_whisper") + collect_data_files("tkinterdnd2"),
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="Транскриптор",
    icon=str(root / "assets" / "transcriber.ico"),
    console=False,
    upx=False,
)
bundle = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    name="Транскриптор",
    upx=False,
)
