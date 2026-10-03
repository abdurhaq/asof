import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
