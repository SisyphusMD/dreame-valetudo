"""PyInstaller entry point for the standalone `dreame-valetudo` bundle.

The release builds this into a single self-contained binary per OS/arch, so
every channel (.pkg, .deb, brew) ships an identical, Python-free artifact. The fastboot libusb
client + form-signature baseline ride along as bundled data (resolved via _MEIPASS); the separate
`dreame-fastboot` client binary + `sunxi-fel` are bundled beside it by the packaging.
"""

import sys

from dreame_valetudo.cli import main

if __name__ == "__main__":
    sys.exit(main())
