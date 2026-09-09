"""Console entry point for the portable distribution."""

import sys
from pathlib import Path

from g64conv.cli import main

if __name__ == "__main__":
    if sys.platform == "win32":
        import ctypes

        # Load bundled codec dependencies before restoring the normal DLL search
        # path. Otherwise external FFmpeg inherits PyInstaller's private DLL path.
        # https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html
        import av  # noqa: F401

        if not ctypes.windll.kernel32.SetDllDirectoryW(None):
            raise ctypes.WinError()
        if len(sys.argv) == 1:
            # Explorer starts an executable without CLI arguments. Use a writable,
            # stable output directory and an available port for that launch mode.
            raise SystemExit(main(["gui", "--port", "0", "-o", str(Path.home() / "g64conv-output")]))
    raise SystemExit(main())
