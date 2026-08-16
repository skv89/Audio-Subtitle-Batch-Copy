from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    source = root / "assets" / "app_icon.svg"
    target = root / "assets" / "app_icon.ico"
    preview = root / "assets" / "app_icon.png"
    application = QApplication(sys.argv[:1])
    renderer = QSvgRenderer(str(source))
    if not renderer.isValid():
        raise RuntimeError(f"Could not read {source}")
    image = QImage(256, 256, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter, QRectF(0, 0, 256, 256))
    painter.end()
    if not image.save(str(target), "ICO"):
        raise RuntimeError("Qt could not write the Windows icon format.")
    if not image.save(str(preview), "PNG"):
        raise RuntimeError("Qt could not write the icon preview.")
    application.quit()
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
