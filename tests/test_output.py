"""The one output path writes whole lines even when several threads write at once (issue #15)."""

from __future__ import annotations

import threading
import time

from ticketctl import output


class _SlowStream:
    """Counts the writes in progress: more than one at a time would interleave the lines."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.now = 0
        self.peak = 0
        self._guard = threading.Lock()

    def write(self, text: str) -> None:
        with self._guard:
            self.now += 1
            self.peak = max(self.peak, self.now)
        time.sleep(0.002)
        self.lines.append(text)
        with self._guard:
            self.now -= 1

    def flush(self) -> None:
        return None


def test_concurrent_writes_never_overlap() -> None:
    stream = _SlowStream()
    threads = [
        threading.Thread(target=lambda n=n: [output.write(stream, f"line {n}") for _ in range(5)])
        for n in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert stream.peak == 1 and len(stream.lines) == 40
