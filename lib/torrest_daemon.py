import hashlib
import logging
import os
import shutil
import struct

from lib.daemon import Daemon
from lib.torrest.lib import TorrestLib


def compute_hex_digest(file_path, hash_type, buff_size=4096):
    h = hash_type()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(buff_size), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_sha1(path):
    return compute_hex_digest(path, hashlib.sha1)


def get_current_app_id():
    with open("/proc/{:d}/cmdline".format(os.getpid())) as fp:
        return fp.read().rstrip("\0")


class TorrestDaemonNotFoundError(Exception):
    pass


class TorrestDaemonStartError(Exception):
    """The daemon (or library) exists but can't run on this device."""
    pass


def elf_interpreter(path):
    """The program loader an ELF executable asks for (e.g. /lib/ld-linux-armhf.so.3), or None."""
    try:
        with open(path, "rb") as f:
            data = f.read(64 * 1024)
        if data[:4] != b"\x7fELF":
            return None
        is64, endian = data[4] == 2, "<" if data[5] == 1 else ">"
        if is64:
            ph_off, = struct.unpack_from(endian + "Q", data, 32)
            ph_size, ph_num = struct.unpack_from(endian + "HH", data, 54)
        else:
            ph_off, = struct.unpack_from(endian + "I", data, 28)
            ph_size, ph_num = struct.unpack_from(endian + "HH", data, 42)
        for i in range(ph_num):
            entry = ph_off + i * ph_size
            p_type, = struct.unpack_from(endian + "I", data, entry)
            if p_type == 3:  # PT_INTERP
                if is64:
                    offset, = struct.unpack_from(endian + "Q", data, entry + 8)
                    size, = struct.unpack_from(endian + "Q", data, entry + 32)
                else:
                    offset, size = struct.unpack_from(endian + "I", data, entry + 4)[0], \
                                   struct.unpack_from(endian + "I", data, entry + 16)[0]
                return data[offset:offset + size].rstrip(b"\0").decode("utf-8", "replace")
    except (IOError, OSError, struct.error, IndexError):
        pass
    return None


def start_error(path, error):
    """Why the daemon at path can't start, in words: usually a build for another kind of system."""
    interpreter = elf_interpreter(path)
    if interpreter and not os.path.exists(interpreter):
        return "This Torrest daemon build needs {}, which this device doesn't have".format(interpreter)
    return "Torrest daemon can't run on this device: {}".format(error)


class ConfigError(LookupError):
    pass


class TorrestDaemon(object):
    _sentinel = object()

    def __init__(self, name, directory, config=None, dest_dir=None, android_find_dest_dir=True, android_extra_dirs=()):
        self._name = name
        self._src_dir = directory
        self._src_path = os.path.join(self._src_dir, self._name)
        self._config = {} if config is None else config

        if not os.path.exists(self._src_path):
            raise TorrestDaemonNotFoundError("Torrest daemon source path does not exist: {}".format(self._src_path))

        if "ANDROID_STORAGE" in os.environ and android_find_dest_dir:
            app_dir = os.path.join(os.sep, "data", "data", get_current_app_id())
            if not os.path.exists(app_dir):
                logging.debug("Default android app dir '%s' does not exist", app_dir)
                for extra_dir in android_extra_dirs:
                    if os.path.exists(extra_dir):
                        app_dir = extra_dir
                        break

            logging.debug("Using android app dir '%s'", app_dir)
            self._dir = os.path.join(app_dir, "files", "torrest")
        else:
            self._dir = directory if dest_dir is None else dest_dir

        self._path = os.path.join(self._dir, self._name)
        logging.debug("Using torrest path '%s'", self._path)
        self._copy_to_dest()

    def setup(self):
        pass

    def start(self):
        raise NotImplementedError("Method start must be implemented")

    def stop(self):
        raise NotImplementedError("Method stop must be implemented")

    def poll(self):
        raise NotImplementedError("Method poll must be implemented")

    def get_config(self, config_name, default=_sentinel):
        config_value = self._config.get(config_name, default)
        if config_value is self._sentinel:
            raise ConfigError("No config named " + config_name)
        return config_value

    def set_config(self, config_name, config_value):
        self._config[config_name] = config_value

    def _copy_to_dest(self):
        if self._dir is not self._src_dir and (
                not os.path.exists(self._path) or compute_sha1(self._src_path) != compute_sha1(self._path)):
            logging.info("Updating daemon '%s'", self._path)
            if os.path.exists(self._dir):
                logging.debug("Removing old daemon dir %s", self._dir)
                shutil.rmtree(self._dir)
            shutil.copytree(self._src_dir, self._dir)


class TorrestExecutableDaemon(TorrestDaemon):
    def __init__(self, name, directory, config=None, dest_dir=None,
                 android_find_dest_dir=True, android_extra_dirs=(), **kwargs):
        super(TorrestExecutableDaemon, self).__init__(
            name, directory, config=config, dest_dir=dest_dir,
            android_find_dest_dir=android_find_dest_dir, android_extra_dirs=android_extra_dirs)
        self._daemon = Daemon(self._name, self._dir, **kwargs)

    def setup(self):
        self._daemon.ensure_exec_permissions()
        self._daemon.kill_leftover_process()

    def start(self):
        try:
            self._daemon.start(
                "--port", str(self.get_config("port")),
                "--settings", self.get_config("settings_path"),
                level=logging.INFO, path=self.get_config("log_path"))
        except OSError as e:
            raise TorrestDaemonStartError(start_error(self._path, e))

    def stop(self):
        self._daemon.stop()

    def poll(self):
        return self._daemon.daemon_poll()


class TorrestLibraryDaemon(TorrestDaemon):
    def __init__(self, name, directory, config=None, dest_dir=None,
                 android_find_dest_dir=True, android_extra_dirs=(), work_dir=None):
        super(TorrestLibraryDaemon, self).__init__(
            name, directory, config=config, dest_dir=dest_dir,
            android_find_dest_dir=android_find_dest_dir, android_extra_dirs=android_extra_dirs)
        try:
            self._daemon = TorrestLib(self._path)
        except OSError as e:
            raise TorrestDaemonStartError("Torrest library can't load on this device: {}".format(e))
        self._work_dir = work_dir

    def setup(self):
        # TODO check other way to do this
        if self._work_dir is not None:
            os.chdir(self._work_dir)

    def start(self):
        self._daemon.clear_logging_sinks()
        self._daemon.add_logging_callback_sink()
        self._daemon.add_logging_file_sink(self.get_config("log_path"), truncate=True)
        self._daemon.start_threaded(self.get_config("port"), self.get_config("settings_path"), daemon=True)

    def stop(self):
        self._daemon.stop()
        self._daemon.join_thread()
        self._daemon.clear_logging_sinks()

    def poll(self):
        return self._daemon.poll()
