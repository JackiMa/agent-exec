import os
import sys
from pathlib import Path


def main(argv):
    os.execv(sys.executable, [sys.executable, str(Path(__file__).with_name("_legacy.py")), *argv])
