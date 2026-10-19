"""Older Office and OpenDocument files (.doc, .xls, .ppt, .rtf, .odt, .ods, .odp), converted by LibreOffice first."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from . import ExtractionError, Segment

TARGETS = {".doc": ".docx", ".rtf": ".docx", ".odt": ".docx", ".xls": ".xlsx", ".ods": ".xlsx", ".ppt": ".pptx", ".odp": ".pptx"}


def read_legacy(path: Path, config) -> list[Segment]:
    from . import documents

    program = shutil.which("soffice") or shutil.which("libreoffice")
    if not program:
        raise ExtractionError("install LibreOffice to read .doc, .xls and .ppt files (sudo apt install libreoffice-core)")
    target = TARGETS[path.suffix.lower()]
    with tempfile.TemporaryDirectory(prefix="rigveda-office-") as folder:
        # A profile of its own, so this works while LibreOffice is open on the desktop.
        subprocess.run([program, f"-env:UserInstallation=file://{folder}/profile", "--headless", "--norestore",
                        "--convert-to", target[1:], "--outdir", folder, str(path)], capture_output=True, timeout=300)
        converted = Path(folder) / (path.stem + target)
        if not converted.exists():
            raise ValueError("LibreOffice could not convert the file")
        reader = {".docx": documents.read_docx, ".xlsx": documents.read_xlsx, ".pptx": documents.read_pptx}[target]
        return reader(converted, config)


EXTRACTORS = [(suffix, "document", read_legacy) for suffix in TARGETS]
