import logging
import os
import re
import time

try:
    from urllib.parse import quote, unquote
except ImportError:
    from urllib import quote, unquote

import requests
import routing
from xbmc import Monitor, executebuiltin, getInfoLabel, getCondVisibility, sleep
from xbmcgui import ListItem, DialogProgress, Dialog
from xbmcplugin import addDirectoryItem, endOfDirectory, setResolvedUrl

from lib import settings, trackers
from lib.dialog import DialogInsert
from lib.kodi import ADDON_PATH, ADDON_NAME, translate, notification, set_logger, refresh, show_picture, \
    busy_dialog, close_busy_dialog
from lib.kodi_formats import is_music, is_picture, is_video, is_text
from lib.play_status import PlayStatus, STATE_STARTING, STATE_BUFFERING, STATE_PLAYING, skin_osd_enabled
from lib.player import TorrestPlayer
from lib.prebuffer import PreBuffer
from lib.torrest.api import Torrest, TorrestError, STATUS_SEEDING, STATUS_PAUSED, STATUS_QUEUED
from lib.utils import sizeof_fmt, ThreadLocal

set_logger()
plugin = routing.Plugin()
api = Torrest(settings.get_service_address(), settings.get_port(), settings.ssl_enabled())
# Status of the play in progress, for skins (see lib/play_status.py)
play_status = None
# The play's magnet (with trackers), when it came from one, and its stuck queue watch
play_magnet_uri = None
queue_watch = None
# The play's torrent, and whether this play added it (rather than playing one already there)
play_torrent = None
play_torrent_added = False

# Seconds a play's torrent may sit in libtorrent's queue before its magnet is re-added
QUEUED_RECOVERY_DELAY = 8


class Types:
    @staticmethod
    def boolean(s):
        return s.lower() in ("true", "1", "yes", "on")

    @staticmethod
    def integer(s):
        return int(s, 10)

    @staticmethod
    def string(s):
        return s


class PlayError(Exception):
    def __init__(self, e, reason=None):
        super(PlayError, self).__init__(e)
        # What to tell the user, or None when there's nothing to tell (canceled, aborted)
        self.reason = reason

    def handle(self):
        # The play ended before playback: clean up as for a stop right at the start
        if play_torrent is not None:
            handle_player_stop(play_torrent, added=play_torrent_added)


def play_failed(message_id, log_message):
    reason = translate(message_id)
    if not play_status.skin_ui:
        notification(reason)
    return PlayError(log_message, reason=reason)


class CanceledError(PlayError):
    def __init__(self, e, info_hash):
        super(CanceledError, self).__init__(e)


class SupersededError(PlayError):
    """A newer play started: give up without resolving, and leave the torrent to that play."""

    def handle(self):
        pass


def check_superseded():
    if play_status.superseded():
        raise SupersededError("A newer play started")


def check_playable(func, _ctx=ThreadLocal(True)):
    def wrapper(*args, **kwargs):
        if _ctx.get():
            _ctx.set(False)
            global play_status, play_magnet_uri, queue_watch, play_torrent, play_torrent_added
            status = play_status = PlayStatus()
            play_magnet_uri = None
            queue_watch = QueueWatch()
            play_torrent = None
            play_torrent_added = False
            status.start()

            try:
                return func(*args, **kwargs)
            except PlayError as e:
                setResolvedUrl(plugin.handle, False, ListItem())
                logging.debug(e)
                status.failed(e.reason)
                e.handle()
                return None
            except Exception as e:
                setResolvedUrl(plugin.handle, False, ListItem())
                status.failed(str(e))
                raise e
            finally:
                _ctx.set(True)
                status.linger()
        else:
            return func(*args, **kwargs)

    return wrapper


def check_directory(func):
    def wrapper(*args, **kwargs):
        succeeded = False

        try:
            ret = func(*args, **kwargs)
            succeeded = True
            return ret
        finally:
            endOfDirectory(plugin.handle, succeeded=succeeded)

    return wrapper


def li(tid, icon):
    return list_item(translate(tid), icon)


def list_item(label, icon):
    item = ListItem(label)
    item.setArt({"icon": os.path.join(ADDON_PATH, "resources", "images", icon)})
    return item


def action(func, *args, **kwargs):
    return "RunPlugin({})".format(plugin.url_for(func, *args, **kwargs))


def media(func, *args, **kwargs):
    return "PlayMedia({})".format(plugin.url_for(func, *args, **kwargs))


def query_arg(name, required=True, arg_type=Types.string):
    def decorator(func):
        def wrapper(*args, **kwargs):
            if name not in kwargs:
                query_list = plugin.args.get(name)
                if query_list:
                    kwargs[name] = arg_type(query_list[0])
                elif required:
                    raise AttributeError("Missing {} required query argument".format(name))
            return func(*args, **kwargs)

        return wrapper

    return decorator


def get_state_string(state):
    if 0 <= state <= 9:
        return translate(30220 + state)
    return translate(30230)


def get_status_labels(info_hash):
    status = api.torrent_status(info_hash)
    return (
        "{:s} ({:.2f}%)".format(get_state_string(status.state), status.progress),
        "D:{:s}/s U:{:s}/s S:{:d}/{:d} P:{:d}/{:d}".format(
            sizeof_fmt(status.download_rate), sizeof_fmt(status.upload_rate), status.seeders,
            status.seeders_total, status.peers, status.peers_total))


def handle_player_stop(info_hash, name=None, watched=None, added=False, initial_delay=0.5, listing_timeout=10):
    """
    After a play ends. watched: how much was played (0-100), or None when it ended before
    playback started; then the torrent counts as unstarted only if this play added it.
    """
    try:
        info = api.torrent_info(info_hash)
    except TorrestError:
        return
    if name is None:
        name = info.name

    # Only the whole torrent can be deleted: never a pack (a season, say) for one of its videos
    if is_pack(info_hash):
        logging.info("Keeping %s: more than one video in it", name)
        unstarted = watched_to_end = False
    else:
        unstarted = added if watched is None else watched < settings.get_unstarted_percent()
        watched_to_end = watched is not None and watched >= settings.get_watched_percent()

    if settings.delete_unstarted() and unstarted:
        logging.info("Deleting %s: stopped near the start", name)
        remove_torrent_files(info_hash)
        return
    if settings.delete_watched() and watched_to_end:
        logging.info("Deleting %s: watched to the end", name)
        remove_torrent_files(info_hash)
        return

    stop_action = settings.get_on_playback_stop_action()
    if stop_action != settings.PlaybackStopAction.IGNORE:
        sleep(int(initial_delay * 1000))
        start_time = time.time()
        while getCondVisibility("Window.IsActive(busydialog)") and not 0 < listing_timeout < time.time() - start_time:
            sleep(100)

        if (stop_action == settings.PlaybackStopAction.DELETE
                or Dialog().yesno(ADDON_NAME, name + "\n" + translate(30241))):
            remove_torrent_files(info_hash)
            return

    if settings.stop_on_exit():
        # Kept for later, but no downloading or seeding in the background
        try:
            api.pause_torrent(info_hash)
        except TorrestError as e:
            logging.debug("Failed to pause %s: %s", info_hash, e)


def is_pack(info_hash):
    min_size = settings.get_min_candidate_size() * 1024 * 1024
    return sum(1 for f in api.files(info_hash, status=False) if is_video(f.path) and f.length >= min_size) > 1


def remove_torrent_files(info_hash):
    try:
        api.remove_torrent(info_hash, delete=True)
    except TorrestError as e:
        # Already removed, e.g. by another play's stop handler for the same torrent
        logging.debug("Failed to remove %s: %s", info_hash, e)
        return
    current_folder = getInfoLabel("Container.FolderPath")
    if current_folder == plugin.url_for(torrent_files, info_hash):
        executebuiltin("Action(Back)")
    elif current_folder == plugin.url_for(torrents):
        refresh()


@plugin.route("/")
@check_directory
def index():
    addDirectoryItem(plugin.handle, plugin.url_for(torrents), li(30206, "torrents.png"), isFolder=True)
    addDirectoryItem(plugin.handle, plugin.url_for(dialog_insert), li(30207, "add.png"), isFolder=False)


@plugin.route("/torrents")
@check_directory
def torrents():
    for torrent in api.torrents(status=True):
        context_menu_items = [
            (translate(30235), media(play_info_hash, info_hash=torrent.info_hash))
        ]

        if torrent.status.state not in (STATUS_SEEDING, STATUS_PAUSED):
            context_menu_items.append(
                (translate(30208), action(torrent_action, torrent.info_hash, "stop"))
                if torrent.status.total == torrent.status.total_wanted else
                (translate(30209), action(torrent_action, torrent.info_hash, "download"))
            )

        context_menu_items.extend([
            (translate(30210), action(torrent_action, torrent.info_hash, "resume"))
            if torrent.status.paused else
            (translate(30211), action(torrent_action, torrent.info_hash, "pause")),
            (translate(30242), action(torrent_action, torrent.info_hash, "remove_torrent")),
            (translate(30212), action(torrent_action, torrent.info_hash, "remove_torrent_and_files")),
            (translate(30245), action(torrent_action, torrent.info_hash, "torrent_status"))
        ])

        torrent_li = list_item(torrent.name, "download.png")
        torrent_li.addContextMenuItems(context_menu_items)
        addDirectoryItem(plugin.handle, plugin.url_for(torrent_files, torrent.info_hash), torrent_li, isFolder=True)


@plugin.route("/torrents/<info_hash>/<action_str>")
@query_arg("prefix", required=False, arg_type=Types.string)
def torrent_action(info_hash, action_str, prefix=""):
    needs_refresh = True

    if action_str == "stop":
        api.stop_torrent(info_hash, prefix=prefix)
    elif action_str == "download":
        api.download_torrent(info_hash, prefix=prefix)
    elif action_str == "pause":
        api.pause_torrent(info_hash)
    elif action_str == "resume":
        api.resume_torrent(info_hash)
    elif action_str == "remove_torrent":
        api.remove_torrent(info_hash, delete=False)
    elif action_str == "remove_torrent_and_files":
        api.remove_torrent(info_hash, delete=True)
    elif action_str == "torrent_status":
        torrent_status(info_hash)
        needs_refresh = False
    else:
        logging.error("Unknown action '%s'", action_str)
        needs_refresh = False

    if needs_refresh:
        refresh()


def torrent_status(info_hash):
    status = api.torrent_status(info_hash)
    notification("{:s} ({:.2f}%)".format(get_state_string(status.state), status.progress),
                 api.torrent_info(info_hash).name, sound=False)


def natural_sort_key(key=lambda v: v):
    digits = re.compile(r"(\d+)")

    def alphanum_key(element):
        return [c if i % 2 == 0 else int(c) for i, c in enumerate(digits.split(key(element)))]

    return alphanum_key


def sort_items(files):
    order = settings.get_files_order()
    if order == settings.FilesOrder.NAME:
        files.sort(key=natural_sort_key(lambda k: k.name))
    elif order == settings.FilesOrder.SIZE:
        files.sort(key=lambda k: k.length)


@plugin.route("/torrents/<info_hash>")
@query_arg("folder", required=False, arg_type=Types.string)
@check_directory
def torrent_files(info_hash, folder=""):
    if folder or settings.folder_listing_enabled():
        items = api.items(info_hash, folder, status=True)
        if not folder and settings.skip_root_folder():
            while len(items.files) == 0 and len(items.folders) == 1:
                items = api.items(info_hash, items.folders[0].path, status=True)

        folders = items.folders
        files = items.files
    else:
        folders = []
        files = api.files(info_hash, status=True)

    sort_items(folders)
    for f in folders:
        folder_li = list_item(f.name, "folder.png")
        folder_li.addContextMenuItems([
            (translate(30208), action(torrent_action, info_hash=info_hash, action_str="stop", prefix=f.path))
            if f.status.wanted_count == f.file_count else
            (translate(30209), action(torrent_action, info_hash=info_hash, action_str="download", prefix=f.path))
        ])
        url = plugin.url_for(torrent_files, info_hash=info_hash, folder=f.path)
        addDirectoryItem(plugin.handle, url, folder_li, isFolder=True)

    sort_items(files)
    for f in files:
        serve_url = api.serve_url(info_hash, f.id)
        file_li = list_item(f.name, "download.png")
        file_li.setPath(serve_url)

        context_menu_items = []
        info_labels = {"title": f.name}
        if is_picture(f.name):
            url = plugin.url_for(display_picture, info_hash, f.id)
            file_li.setInfo("pictures", info_labels)
        elif is_text(f.name):
            url = plugin.url_for(display_text, info_hash, f.id)
        else:
            url = serve_url
            if is_video(f.name):
                info_type = "video"
            elif is_music(f.name):
                info_type = "music"
            else:
                info_type = None

            if info_type is not None:
                kwargs = dict(info_hash=info_hash, file_id=f.id)
                url = plugin.url_for(play, **kwargs)
                file_li.setInfo(info_type, info_labels)
                file_li.setProperty("IsPlayable", "true")
                context_menu_items.append((translate(30235), media(buffer_and_play, **kwargs)))

        context_menu_items.append(
            (translate(30209), action(file_action, info_hash, f.id, "download"))
            if f.status.priority == 0 else
            (translate(30208), action(file_action, info_hash, f.id, "stop"))
        )
        file_li.addContextMenuItems(context_menu_items)

        addDirectoryItem(plugin.handle, url, file_li)


@plugin.route("/display_picture/<info_hash>/<file_id>")
def display_picture(info_hash, file_id):
    show_picture(api.serve_url(info_hash, file_id))


@plugin.route("/display_text/<info_hash>/<file_id>")
def display_text(info_hash, file_id):
    r = requests.get(api.serve_url(info_hash, file_id))
    Dialog().textviewer(api.file_info(info_hash, file_id).name, r.text)


@plugin.route("/play_url")
@check_playable
@query_arg("url", arg_type=Types.string)
@query_arg("file_id", required=False, arg_type=Types.integer)
@query_arg("index", required=False, arg_type=Types.integer)
@query_arg("buffer", required=False, arg_type=Types.boolean)
def play_url(url, file_id=None, index=None, buffer=True):
    r = requests.get(url, stream=True)
    info_hash = add_for_play(lambda: api.add_torrent_obj(r.raw, ignore_duplicate=True, download=False))
    play_info_hash(info_hash, file_id=index if file_id is None else file_id, buffer=buffer)


@plugin.route("/play_magnet")
@check_playable
@query_arg("magnet", arg_type=Types.string)
@query_arg("file_id", required=False, arg_type=Types.integer)
@query_arg("index", required=False, arg_type=Types.integer)
@query_arg("buffer", required=False, arg_type=Types.boolean)
def play_magnet(magnet, file_id=None, index=None, buffer=True):
    global play_magnet_uri
    magnet = play_magnet_uri = with_trackers(magnet)
    info_hash = add_for_play(lambda: api.add_magnet(magnet, ignore_duplicate=True, download=False))
    play_info_hash(info_hash, file_id=index if file_id is None else file_id, buffer=buffer)


@plugin.route("/download_magnet")
@query_arg("magnet", arg_type=Types.string)
@query_arg("file_id", required=False, arg_type=Types.integer)
@query_arg("index", required=False, arg_type=Types.integer)
def download_magnet(magnet, file_id=None, index=None):
    """Downloads a magnet's torrent, or just one of its files (file_id / index), without playing
    it, as the Torrents menu's Download does: for other add-ons (Ultra Player's Download). It's
    then in Torrents, where its progress shows; a later play of it streams what's there."""
    info_hash = api.add_magnet(with_trackers(magnet), ignore_duplicate=True, download=False)
    file_id = index if file_id is None else file_id
    if file_id is None:
        api.download_torrent(info_hash)
    else:
        # A file can only be picked once the torrent's metadata (its file list) is in.
        timeout, start, monitor = settings.get_metadata_timeout(), time.time(), Monitor()
        while not api.torrent_status(info_hash).has_metadata:
            if monitor.waitForAbort(0.5) or (0 < timeout < time.time() - start):
                notification(translate(30238))
                return
        api.download_file(info_hash, file_id, buffer=False)
    notification(translate(30243), time=2000)


def add_for_play(add):
    """Adds the play's torrent, noting whether it is new (so an unstarted play can delete it)."""
    global play_torrent_added
    existing = set(t.info_hash for t in api.torrents(status=False))
    info_hash = add()
    play_torrent_added = info_hash not in existing
    return info_hash


def with_trackers(magnet):
    mode = settings.get_trackers_mode()
    if mode == settings.TrackersMode.DISABLED or (mode == settings.TrackersMode.NO_TRACKERS and "tr=" in magnet):
        return magnet
    existing = unquote(magnet)
    return magnet + "".join("&tr=" + quote(t, safe="") for t in trackers.enabled_urls() if t not in existing)


class QueueWatch(object):
    """
    Re-adds the play's magnet when its torrent sits in libtorrent's queue. A torrent that was
    downloading when the daemon stopped (e.g. Kodi restarted mid-buffer) can come back queued
    and stay there, and pausing/resuming doesn't start it; removing it (keeping its data) and
    adding the magnet again does.
    """

    def __init__(self):
        self._since = None
        self._retried = False

    def check(self, info_hash, status):
        """Returns True when the torrent was re-added (its metadata and file priorities are gone)."""
        if status.state != STATUS_QUEUED or play_magnet_uri is None or self._retried:
            self._since = None
            return False
        now = time.time()
        if self._since is None:
            self._since = now
        if now - self._since < QUEUED_RECOVERY_DELAY:
            return False
        logging.warning("Torrent %s stuck in queue, adding its magnet again", info_hash)
        self._retried = True
        api.remove_torrent(info_hash, delete=False)
        api.add_magnet(play_magnet_uri, ignore_duplicate=True, download=False)
        return True


@plugin.route("/play_path")
@check_playable
@query_arg("path", arg_type=Types.string)
@query_arg("file_id", required=False, arg_type=Types.integer)
@query_arg("index", required=False, arg_type=Types.integer)
@query_arg("buffer", required=False, arg_type=Types.boolean)
def play_file(path, file_id=None, index=None, buffer=True):
    info_hash = add_for_play(lambda: api.add_torrent(path, ignore_duplicate=True, download=False))
    play_info_hash(info_hash, file_id=index if file_id is None else file_id, buffer=buffer)


@plugin.route("/play_info_hash/<info_hash>")
@query_arg("buffer", required=False, arg_type=Types.boolean)
@query_arg("file_id", required=False, arg_type=Types.integer)
@check_playable
def play_info_hash(info_hash, file_id=None, buffer=True):
    global play_torrent
    play_torrent = info_hash
    status = api.torrent_status(info_hash)
    if status.paused:
        # Stopped when an earlier play ended (or when Kodi started)
        api.resume_torrent(info_hash)
    if not status.has_metadata:
        wait_for_metadata(info_hash)

    if file_id is not None and file_id not in [f.id for f in api.files(info_hash, status=False)]:
        logging.warning("File %s not found in %s, choosing a file instead", file_id, info_hash)
        file_id = None

    if file_id is None:
        files = api.files(info_hash, status=False)
        min_candidate_size = settings.get_min_candidate_size() * 1024 * 1024
        candidate_files = [f for f in files if is_video(f.path) and f.length >= min_candidate_size]
        if not candidate_files:
            raise play_failed(30239, "No candidate files found for {}".format(info_hash))
        elif len(candidate_files) == 1:
            file_id = candidate_files[0].id
        else:
            sort_items(candidate_files)
            chosen_index = Dialog().select(translate(30240), [f.name for f in candidate_files])
            if chosen_index < 0:
                raise PlayError("User canceled dialog select")
            file_id = candidate_files[chosen_index].id

    if buffer:
        buffer_and_play(info_hash, file_id)
    else:
        play(info_hash, file_id)


class PlayProgress(object):
    """Torrest's own progress dialog, left out while the skin draws the play status itself."""

    def __init__(self, *create_args):
        self._create_args = create_args
        self._dialog = None

    def update(self, percent, message=None):
        if play_status.skin_ui:
            self.close()
            return
        if self._dialog is None:
            self._dialog = DialogProgress()
            self._dialog.create(ADDON_NAME, *self._create_args)
        if message is None:
            self._dialog.update(percent)
        else:
            self._dialog.update(percent, message)

    def iscanceled(self):
        return self._dialog is not None and self._dialog.iscanceled()

    def close(self):
        if self._dialog is not None:
            self._dialog.close()
            self._dialog = None


def wait_for_metadata(info_hash):
    close_busy_dialog()
    percent = 0
    timeout = settings.get_metadata_timeout()
    start_time = time.time()
    monitor = Monitor()
    progress = PlayProgress(translate(30237))
    progress.update(percent)

    try:
        while True:
            status = api.torrent_status(info_hash)
            if status.has_metadata:
                break
            queue_watch.check(info_hash, status)
            play_status.update(STATE_STARTING, translate(30247 if status.peers else 30246),
                               torrent_status=status)
            if monitor.waitForAbort(0.5):
                raise PlayError("Abort requested")
            passed_time = time.time() - start_time
            if 0 < timeout:
                if timeout < passed_time:
                    raise play_failed(30238, "No metadata after timeout")
                percent = int(100 * passed_time / timeout)
            else:
                percent = 0 if percent == 100 else (percent + 5)
            progress.update(percent)
            if progress.iscanceled() or play_status.cancel_requested():
                raise CanceledError("User canceled metadata", info_hash)
            check_superseded()
    finally:
        progress.close()


@plugin.route("/buffer_and_play/<info_hash>/<file_id>")
@check_playable
def buffer_and_play(info_hash, file_id):
    # The daemon's own buffering is skipped: PreBuffer reads the amounts set in the settings
    api.download_file(info_hash, file_id, buffer=False)
    wait_for_buffering_completion(info_hash, file_id)
    play(info_hash, file_id)


def wait_for_buffering_completion(info_hash, file_id):
    close_busy_dialog()
    info = api.file_info(info_hash, file_id)
    pre_buffer = PreBuffer(api.serve_url(info_hash, file_id), info.length,
                           settings.get_buffer_start_size(), settings.get_buffer_end_size())
    if pre_buffer.completed:
        return
    of = translate(30244)
    timeout = settings.get_buffering_timeout()
    start_time = time.time()

    monitor = Monitor()
    progress = PlayProgress()
    pre_buffer.start()

    try:
        while not pre_buffer.completed:
            current_time = time.time()
            status = api.torrent_status(info_hash)
            if queue_watch.check(info_hash, status):
                wait_for_metadata(info_hash)
                api.download_file(info_hash, file_id, buffer=False)
                continue

            done, total = pre_buffer.done, pre_buffer.total
            percent = 100.0 * done / total
            play_status.update(STATE_BUFFERING, "{} {:d}%".format(translate(30248), int(percent)),
                               progress=percent, torrent_status=status, name=info.name)
            progress.update(
                int(percent),
                "{} - {:.2f}%\n{} {} {} - {}/s\n{}\n".format(
                    get_state_string(status.state), percent, sizeof_fmt(done),
                    of, sizeof_fmt(total), sizeof_fmt(status.download_rate), info.name))

            if progress.iscanceled() or play_status.cancel_requested():
                raise CanceledError("User canceled buffering", info_hash)
            check_superseded()
            if 0 < timeout < current_time - start_time:
                raise play_failed(30236, "Buffering timeout reached")
            if monitor.waitForAbort(0.5):
                raise PlayError("Abort requested")
    finally:
        pre_buffer.stop()
        progress.close()


@plugin.route("/play/<info_hash>/<file_id>")
@check_playable
def play(info_hash, file_id):
    serve_url = api.serve_url(info_hash, file_id)
    name = api.file_info(info_hash, file_id).name

    file_li = ListItem(name, path=serve_url)
    info_labels = {"title": name}
    if is_video(name):
        file_li.setInfo("video", info_labels)
    elif is_music(name):
        file_li.setInfo("music", info_labels)
    file_li.setProperty("IsPlayable", "true")

    status = play_status
    # Resolving now would leave this stream queued behind the newer play's, and Kodi plays it
    # when that one stops (even while quitting)
    check_superseded()
    # Cancelled with nothing left to wait for (already downloaded and buffered): don't start it
    # behind whatever the skin went back to
    if status.cancel_requested():
        raise CanceledError("User canceled before playback", info_hash)
    status.update(STATE_BUFFERING, translate(30249), progress=100, name=name)
    setResolvedUrl(plugin.handle, True, file_li)

    def on_close(watched):
        status.clear()
        handle_player_stop(info_hash, name=name, watched=watched)

    # The skin shows the download status in its own OSD: no overlay, publish it instead
    skin_osd = skin_osd_enabled()
    try:
        with TorrestPlayer(
                text_handler=(lambda: get_status_labels(info_hash) + (name,))
                if settings.show_status_overlay() and not skin_osd else None,
                download_status_handler=(lambda: api.torrent_status(info_hash)) if skin_osd else None,
                on_start_handler=lambda: status.playing(translate(30250)),
                active_handler=lambda: not status.superseded(),
                on_close_handler=on_close,
        ) as player:
            player.handle_events(url=serve_url)
    except Exception as e:
        logging.error("Caught exception while playing file: %s", e, exc_info=True)

    if status.superseded():
        # A newer play took over (maybe the same torrent): leave its status and torrent alone
        return
    if status.state != STATE_PLAYING:
        # Kodi never started the stream (or another file replaced it): don't leave the skin waiting
        status.failed(translate(30251))
        handle_player_stop(info_hash, name=name, added=play_torrent_added)
    else:
        status.clear()


@plugin.route("/torrents/<info_hash>/files/<file_id>/<action_str>")
def file_action(info_hash, file_id, action_str):
    if action_str == "download":
        api.download_file(info_hash, file_id, buffer=False)
    elif action_str == "stop":
        api.stop_file(info_hash, file_id)
    else:
        logging.error("Unknown action '%s'", action_str)
        return
    refresh()


# A list kept up to date with working public trackers
TRACKERS_LIST_URL = "https://ngosang.github.io/trackerslist/trackers_best.txt"


def tracker_label(t):
    if t["failures"]:
        return "{} ({})".format(t["url"], translate(30119).format(t["failures"]))
    return t["url"]


@plugin.route("/trackers/<action_str>")
def trackers_action(action_str):
    """The Trackers settings page's buttons."""
    all_trackers = trackers.load()
    if action_str == "enable":
        selected = Dialog().multiselect(translate(30112), [tracker_label(t) for t in all_trackers],
                                        preselect=[i for i, t in enumerate(all_trackers) if t["enabled"]])
        if selected is not None:
            trackers.set_enabled(set(all_trackers[i]["url"] for i in selected))
    elif action_str == "remove":
        selected = Dialog().multiselect(translate(30115), [tracker_label(t) for t in all_trackers])
        if selected:
            trackers.remove(set(all_trackers[i]["url"] for i in selected))
            notification(translate(30120).format(len(selected)), time=2000)
    elif action_str in ("add", "add_url"):
        if action_str == "add":
            text = Dialog().input(translate(30113))
        else:
            url = Dialog().input(translate(30114), TRACKERS_LIST_URL)
            text = url and requests.get(url, timeout=15).text
        if text:
            urls = trackers.parse(text)
            if urls:
                notification(translate(30117).format(trackers.add(urls)), time=2000)
            else:
                notification(translate(30118))
    elif action_str == "check":
        busy_dialog()
        try:
            answered, checked = trackers.check(settings.get_tracker_max_failures())
        finally:
            close_busy_dialog()
        notification(translate(30122).format(answered, checked))
    elif action_str == "reset":
        if Dialog().yesno(ADDON_NAME, translate(30116) + "?"):
            trackers.reset()
    else:
        logging.error("Unknown action '%s'", action_str)


@plugin.route("/insert")
def dialog_insert():
    window = DialogInsert("DialogInsert.xml", ADDON_PATH, "Default")
    window.doModal()
    if window.type == DialogInsert.TYPE_PATH:
        api.add_torrent(window.ret_val, ignore_duplicate=False, download=settings.download_after_insert())
    elif window.type == DialogInsert.TYPE_URL:
        api.add_magnet(window.ret_val, ignore_duplicate=False, download=settings.download_after_insert())
    else:
        return
    notification(translate(30243), time=2000)


def run():
    try:
        plugin.run()
    except Exception as e:
        logging.error("Caught exception:", exc_info=True)
        notification(str(e))
