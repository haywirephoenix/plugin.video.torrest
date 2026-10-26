"""
Play status for skins, through Home window (10000) properties.

A skin that draws its own buffering UI sets "ultra.torrent.ui" to "1"; while it is set Torrest
shows none of its own buffering/progress dialogs. Either way, while a play starts and buffers
Torrest keeps the "torrest.*" properties below up to date, and a skin can cancel the play by
setting "torrest.cancel" to "1".
"""

import threading
import time
import uuid

import xbmc
import xbmcgui

from lib.kodi import WINDOW_HOME

SKIN_UI_PROPERTY = "ultra.torrent.ui"
CANCEL_PROPERTY = "torrest.cancel"
# Which play owns the properties, so a play that ends late doesn't clear a newer play's status
OWNER_PROPERTY = "torrest.owner"
PROPERTIES = ("state", "progress", "label", "speed", "peers", "seeds", "name", "error")

STATE_STARTING = "starting"
STATE_BUFFERING = "buffering"
STATE_PLAYING = "playing"
STATE_FAILED = "failed"

PLAYING_CLEAR_DELAY = 3
FAILED_CLEAR_DELAY = 5


def skin_ui_enabled():
    return xbmcgui.Window(WINDOW_HOME).getProperty(SKIN_UI_PROPERTY) == "1"


def rate_fmt(num):
    for unit in ("B", "kB", "MB", "GB"):
        if abs(num) < 1000.0:
            return "{:.1f} {}/s".format(num, unit)
        num /= 1000.0
    return "{:.1f} TB/s".format(num)


class PlayStatus(object):
    def __init__(self):
        self._home = xbmcgui.Window(WINDOW_HOME)
        self._token = uuid.uuid4().hex
        self._skin_ui = False
        self._failed_time = None
        self.state = None

    @property
    def skin_ui(self):
        # Sticky: once the skin asked for it, this play leaves Torrest's dialogs out
        if not self._skin_ui:
            self._skin_ui = skin_ui_enabled()
        return self._skin_ui

    def _owns(self):
        return self._home.getProperty(OWNER_PROPERTY) == self._token

    def _set(self, **values):
        for key, value in values.items():
            self._home.setProperty("torrest." + key, "" if value is None else str(value))

    def start(self):
        self._home.clearProperty(CANCEL_PROPERTY)
        self._clear_properties()
        self._home.setProperty(OWNER_PROPERTY, self._token)
        self.state = STATE_STARTING
        self._set(state=STATE_STARTING, progress=0)

    def update(self, state, label, progress=0, torrent_status=None, name=None):
        if not self._owns():
            return
        self.state = state
        values = dict(state=state, label=label, progress=max(0, min(100, int(progress))))
        if torrent_status is not None:
            values.update(speed=rate_fmt(torrent_status.download_rate), peers=torrent_status.peers,
                          seeds=torrent_status.seeders)
        if name is not None:
            values["name"] = name
        self._set(**values)

    def cancel_requested(self):
        return self._home.getProperty(CANCEL_PROPERTY) == "1"

    def playing(self, label):
        if not self._owns():
            return
        self.state = STATE_PLAYING
        self._set(state=STATE_PLAYING, progress=100, label=label)
        if self.skin_ui and not xbmc.getCondVisibility("Window.IsActive(fullscreenvideo)"):
            # Kodi doesn't go fullscreen when playback starts under a modal dialog (e.g. a video
            # info page left open after the skin's loading screen closed): the video would play
            # unseen behind it.
            xbmc.executebuiltin("Dialog.Close(all,true)")
            xbmc.executebuiltin("ActivateWindow(fullscreenvideo)")
        timer = threading.Timer(PLAYING_CLEAR_DELAY, self.clear)
        timer.daemon = True
        timer.start()

    def failed(self, error):
        """Shows the error (the skin clears it on its own after a moment), or clears the status
        right away when there's no error to show (canceled, aborted)."""
        if error is None:
            self.clear()
        elif self._owns():
            self.state = STATE_FAILED
            self._failed_time = time.time()
            self._set(state=STATE_FAILED, error=error, label=error)

    def linger(self, monitor=None):
        """After failed(error): keeps the error up for FAILED_CLEAR_DELAY seconds, then clears it."""
        if self._failed_time is None:
            return
        remaining = FAILED_CLEAR_DELAY - (time.time() - self._failed_time)
        if remaining > 0:
            (monitor or xbmc.Monitor()).waitForAbort(remaining)
        self.clear()

    def _clear_properties(self):
        for key in PROPERTIES:
            self._home.clearProperty("torrest." + key)

    def clear(self):
        if self._owns():
            self._clear_properties()
            self._home.clearProperty(CANCEL_PROPERTY)
            self._home.clearProperty(OWNER_PROPERTY)
