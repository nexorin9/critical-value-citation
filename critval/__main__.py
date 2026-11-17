"""``python -m critval`` → 常驻守护进程。

值班机上装一个 Python 就能跑，不需要额外的入口脚本。
"""

from __future__ import annotations

import sys

from critval.daemon import main

if __name__ == "__main__":
    sys.exit(main())
