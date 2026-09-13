"""AMD GPU Tuner GUI launcher.

Run from source:  py -3.12 src/voltshift_gui.py
Frozen build:     AMD-GPU-Tuner.exe

Kept as a thin top-level module so the PyInstaller spec has a stable entry
point regardless of package layout.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from voltshift.gui import run

if __name__ == "__main__":
    run()
