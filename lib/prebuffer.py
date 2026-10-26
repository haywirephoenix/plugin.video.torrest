"""
Pre-buffering through the daemon's serve URL.

The daemon's own buffering (download?buffer=true) uses a fixed 10 MB at the end of the file and
max(size / 200, buffer_size) at the start. Reading byte ranges from the serve URL instead makes
the daemon prioritize exactly those pieces (with deadlines), so the start and end amounts are ours
to choose. Each range is split into segments read in parallel, so many pieces are requested at once.
"""

import logging
import threading

import requests

SEGMENT_SIZE = 4 * 1024 * 1024
WORKERS = 6
CHUNK_SIZE = 64 * 1024
# Seconds without data before a read is retried (the daemon waits up to piece_wait_timeout per piece)
READ_TIMEOUT = 15


def split(start, end, size=SEGMENT_SIZE):
    return [(offset, min(offset + size, end)) for offset in range(start, end, size)]


class PreBuffer(object):
    def __init__(self, url, file_size, start_size, end_size):
        self._url = url
        if file_size <= start_size + end_size:
            ranges = split(0, file_size)
        else:
            ranges = split(0, start_size) + split(file_size - end_size, file_size)
        self._pending = list(reversed(ranges))
        self.total = sum(e - s for s, e in ranges)
        self.done = 0
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._threads = []

    def start(self):
        for _ in range(min(WORKERS, len(self._pending))):
            thread = threading.Thread(target=self._worker)
            thread.daemon = True
            thread.start()
            self._threads.append(thread)

    def stop(self):
        self._stopped.set()

    @property
    def completed(self):
        return self.done >= self.total

    def _next(self):
        with self._lock:
            return self._pending.pop() if self._pending else None

    def _add(self, n):
        with self._lock:
            self.done += n

    def _worker(self):
        session = requests.Session()
        try:
            while not self._stopped.is_set():
                segment = self._next()
                if segment is None:
                    return
                self._read(session, *segment)
        finally:
            session.close()

    def _read(self, session, offset, end):
        while offset < end and not self._stopped.is_set():
            try:
                r = session.get(self._url, headers={"Range": "bytes={}-{}".format(offset, end - 1)},
                                stream=True, timeout=(5, READ_TIMEOUT))
                try:
                    r.raise_for_status()
                    for chunk in r.iter_content(CHUNK_SIZE):
                        if self._stopped.is_set():
                            return
                        n = min(len(chunk), end - offset)
                        offset += n
                        self._add(n)
                        if offset >= end:
                            return
                finally:
                    r.close()
            except requests.exceptions.RequestException as e:
                logging.debug("Pre-buffer read at %d failed, retrying: %s", offset, e)
                self._stopped.wait(1)
