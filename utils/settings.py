"""Per-user settings and state: ~/.yo/settings.ini and ~/.yo/logs/.

INI sections register commands and inherit [DEFAULT]. install.py persists
settings; yo reads them. Values are strings; callers convert. Rewrites lose
comments. See README.md for the format and precedence rules.
"""
import configparser
from pathlib import Path

SETTINGS_NAME = "settings.ini"
DEFAULT_SECTION = configparser.DEFAULTSECT  # "DEFAULT": reserved, never a command name

SCHEDULE_KEYS = ("tz", "hours", "window_hours", "num_windows")  # host-wide, per-command override
COMMAND_KEYS = ("backend", "probe", "model", "effort", "thread_source")  # per command


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
    import io
    out = io.StringIO()
    parser.write(out)
    return out.getvalue()


def seed_defaults(parser, values):
    """Fill [DEFAULT] with the values it lacks (the first install seeds the host defaults)."""
    for key, value in values.items():
        if value is not None and key not in parser[DEFAULT_SECTION]:
            parser[DEFAULT_SECTION][key] = format_value(value)


def update_command(parser, command, values):
    """Register or update `command` with the non-None `values`.

    A value equal to [DEFAULT]'s is not repeated in the section, and a stale
    override of it is dropped, so the file stays minimal and a later change to
    [DEFAULT] reaches every command that did not override it.
    """
    if not parser.has_section(command):
        parser.add_section(command)
    section = parser[command]
    for key, value in values.items():
        if value is None:
            continue
        text = format_value(value)
        if parser[DEFAULT_SECTION].get(key) == text:
            parser.remove_option(command, key)
        else:
            section[key] = text


def format_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
