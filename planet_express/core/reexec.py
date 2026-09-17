"""D18: Restart=on-failure would leave core stopped after a clean exit.

Re-exec preserves the same PID, so systemd never sees an exit.
"""

import logging
import os
import sys
from typing import NoReturn


def reexec(argv: list[str] | None = None) -> NoReturn:
    sys.stdout.flush()
    sys.stderr.flush()
    # Include unattached handlers as well as handlers on root and named loggers.
    for ref in list(logging._handlerList):
        handler = ref()
        if handler is not None:
            handler.flush()
    os.execv(sys.executable, [sys.executable, *(argv or sys.argv)])
