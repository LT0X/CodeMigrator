"""Runtime composition-root skeleton; implementation belongs to CM-RUNTIME-001."""

import signal


def main() -> None:
    """Keep the baseline app process alive until the runtime task replaces this stub."""

    signal.pause()
