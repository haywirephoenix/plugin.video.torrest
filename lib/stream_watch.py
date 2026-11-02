"""
Pausing playback while the download is behind it.

Kodi ends a play when a read from the stream fails (a piece that doesn't arrive in time), so a
torrent that downloads slower than its video plays used to stop the player. While a video plays,
StreamWatch reads the serve URL ahead of the play position, which makes the daemon fetch those
pieces first (as the pre-buffer does) and tells how much of the file is ready past the position.
The position's byte offset is estimated from the playing time and the file's average byte rate.
When less than PAUSE_AHEAD seconds of video are ready and the reader has had nothing for STALL
seconds (what's already downloaded reads at once, so only a wait for peers counts) the player pauses (published as
torrest.download.waiting, for the skin's OSD); it resumes once resume_ahead seconds are ready, or
the rest of the file is. The user's own play / pause wins: resumed by hand, it doesn't pause again
until the download has caught up once.
"""

import logging
import threading
import time

import requests
import xbmcgui

from lib.kodi import WINDOW_HOME

WAITING_PROPERTY = "torrest.download.waiting"
PAUSE_AHEAD = 5  # seconds of video ready past the position, below which playback pauses
STALL = 2  # ...once the reader has waited this long for data
READ_AHEAD = 2  # the reader keeps this many times resume_ahead ready past the position
CHUNK_SIZE = 64 * 1024
READ_TIMEOUT = 15  # seconds without data before a read is retried
CHECK_COMPLETE = 5  # seconds between checks whether the file has finished downloading


class StreamWatch(object):
    def __init__(self, url, length, resume_ahead, complete_handler=None):
        self._url = url
        self._length = length
        self._resume_ahead = resume_ahead
        self._complete_handler = complete_handler
        self._complete = False
        self._rate = 0  # bytes per second of video
        self._offset = 0  # the play position's estimated byte offset
        self._ready = 0  # everything from the position's offset up to here has been read
        self._restart = threading.Event()
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._auto_paused = False
        self._user_resumed = False
        self._since_check = CHECK_COMPLETE
        self._waiting_since = None  # when the reader started waiting for data it hasn't had yet

    def start(self):
        self._thread = threading.Thread(target=self._reader)
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        self._stopped.set()
        self._restart.set()
        xbmcgui.Window(WINDOW_HOME).clearProperty(WAITING_PROPERTY)

    def ahead(self):
        """Seconds of video ready past the play position (None when it's all there or unknown)."""
        with self._lock:
            if self._complete or not self._rate or self._ready >= self._length:
                return None
            return (self._ready - self._offset) / float(self._rate)

    def _update(self, position, duration):
        if duration <= 0 or self._length <= 0:
            return
        with self._lock:
            self._rate = self._length / float(duration)
            offset = min(int(position * self._rate), self._length)
            # Moved back, or past what's ready (a seek): read on from the new position
            if offset < self._offset or offset > self._ready:
                self._ready = offset
                self._restart.set()
            self._offset = offset

    def check(self, player, period):
        """Called about every period seconds while the video plays or is paused."""
        self._since_check += period
        if self._complete_handler and self._since_check >= CHECK_COMPLETE:
            self._since_check = 0
            try:
                self._complete = self._complete_handler()
            except Exception as e:
                logging.debug("Failed to get the file status: %s", e)
        try:
            self._update(player.get_time(), player.get_total_time())
        except RuntimeError:
            return
        ahead = self.ahead()
        caught_up = ahead is None or ahead >= self._resume_ahead
        with self._lock:
            waiting = self._waiting_since
        stalled = waiting is not None and time.time() - waiting >= STALL
        paused = player.is_paused()
        if self._auto_paused:
            if not paused:
                logging.info("Resumed by hand while the download is behind")
                self._auto_paused, self._user_resumed = False, True
            elif caught_up:
                logging.info("Download caught up: resuming")
                player.toggle_pause()
                self._auto_paused = False
        elif self._user_resumed:
            self._user_resumed = not caught_up
        elif not paused and stalled and ahead is not None and ahead < PAUSE_AHEAD:
            logging.info("Download behind playback (%.1f s ready): pausing until %d s are",
                         ahead, self._resume_ahead)
            player.toggle_pause()
            self._auto_paused = True
        home = xbmcgui.Window(WINDOW_HOME)
        if self._auto_paused:
            home.setProperty(WAITING_PROPERTY, "1")
        else:
            home.clearProperty(WAITING_PROPERTY)

    def _target(self):
        with self._lock:
            ahead = int(self._resume_ahead * READ_AHEAD * self._rate)
            return self._ready, min(self._offset + max(ahead, CHUNK_SIZE), self._length)

    def _add(self, start, n):
        with self._lock:
            if self._ready == start:  # not moved by a seek meanwhile
                self._ready += n
                self._waiting_since = time.time()  # waiting for the next chunk now
                return True
            return False

    def _waiting(self, since):
        with self._lock:
            self._waiting_since = since

    def _reader(self):
        session = requests.Session()
        try:
            while not self._stopped.is_set():
                self._restart.clear()
                start, end = self._target()
                if self._complete or not self._rate or start >= end:
                    self._waiting(None)  # nothing wanted
                    self._restart.wait(0.5)
                    continue
                self._waiting(time.time())
                try:
                    r = session.get(self._url, headers={"Range": "bytes={}-{}".format(start, end - 1)},
                                    stream=True, timeout=(5, READ_TIMEOUT))
                    try:
                        r.raise_for_status()
                        for chunk in r.iter_content(CHUNK_SIZE):
                            if self._restart.is_set() or not self._add(start, len(chunk)):
                                break
                            start += len(chunk)
                    finally:
                        r.close()
                except requests.exceptions.RequestException as e:
                    logging.debug("Read ahead at %d failed, retrying: %s", start, e)
                    self._restart.wait(1)
        finally:
            session.close()
