"""Per-user settings and state: ~/.yo/settings.ini and ~/.yo/logs/.

One INI section per command, each complete on its own (no inheritance).
install.py persists settings; yo reads them. Values are strings; callers
convert. Rewrites lose comments. See README.md for the format.
"""
import configparser
import io
from pathlib import Path

SETTINGS_NAME = "settings.ini"

SCHEDULE_KEYS = ("tz", "hours", "window_hours", "num_windows")
COMMAND_KEYS = ("backend", "probe", "model", "effort", "thread_source")


def home_dir():
    """~/.yo, resolved from HOME at call time (tests point HOME at a scratch directory)."""
    return Path.home() / ".yo"


def log_dir():
    return home_dir() / "logs"


def settings_path():
    return home_dir() / SETTINGS_NAME


def load():
    """The settings file as a ConfigParser (empty when there is none)."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(settings_path())
    return parser


def save(parser):
    """Write the file atomically, creating ~/.yo/ if needed."""
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as handle:
        parser.write(handle)
    tmp.replace(path)


def dump(parser):
    """The file's text as save() would write it."""
    out = io.StringIO()
    parser.write(out)
    return out.getvalue()


def update_command(parser, command, values):
    """Register or update `command` with the non-None `values` (booleans as true/false)."""
    if not parser.has_section(command):
        parser.add_section(command)
    for key, value in values.items():
        if value is not None:
            parser[command][key] = str(value).lower() if isinstance(value, bool) else str(value)
