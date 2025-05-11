"""Stream to pass strings to the queue"""

from io import TextIOBase
from multiprocessing import Queue
from typing import Iterable


class QueueStream(TextIOBase):
    def __init__(self, queue: Queue) -> None:
        super().__init__()

        self._queue: Queue = queue
        self._cache: str = ''
    
    def write(self, s: str) -> int:
        for c in s:
            if c == '\r':
                self._cache = ''
            elif c == '\n':
                # self._cache += c
                self._queue.put_nowait(self._cache)
                self._cache = ''
            else:
                self._cache += c
        return len(s)

    def writelines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self._queue.put_nowait(line)

    def flush(self) -> None:
        pass
