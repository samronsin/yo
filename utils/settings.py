"""Per-user settings and state: ~/.yo/settings.ini and ~/.yo/logs/.

A setting's scope is the scope of what it describes. Logins, hours and
timezone are per user, like the crontab, so they live in the home rather than
the checkout; the logs and locks yo writes go beside them, which lets one
read-only checkout serve several users, each with their own ~/.yo/.

The file is INI (configparser): one section per command, [DEFAULT] for the
host-wide values a section may override.

    [DEFAULT]
    tz = Europe/Paris
    hours = 9-18
    window_hours = 5
    num_windows = 3

    [codex-pro]
    backend = codex

install.py writes it (flags given win over the file and are persisted), yo
reads it (backend, model/effort/thread_source overrides, tz). Values are
strings; callers convert. Comments are lost when install.py rewrites the file.
"""
import configparser
from pathlib import Path

HOME_DIR = Path.home() / ".yo"  # tests patch this
SETTINGS_NAME = "settings.ini"
DEFAULT_SECTION = configparser.DEFAULTSECT  # "DEFAULT": reserved, never a command name

SCHEDULE_KEYS = ("tz", "hours", "window_hours", "num_windows")  # host-wide, per-command override
COMMAND_KEYS = ("backend", "schedule", "probe", "model", "effort", "thread_source")  # per command
# schedule = false registers a command for yo and --status without a cron block
# (a laptop that must never get a crontab); --refresh skips it.


def log_dir():
    return HOME_DIR / "logs"


def settings_path():
    return HOME_DIR / SETTINGS_NAME


def load():
    """The settings file as a ConfigParser (empty when there is none)."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(settings_path())
    return parser


def save(parser):
    """Write the file atomically, creating ~/.yo/ if needed."""
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    path = settings_path()
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


def commands(parser):
    """Registered command names, in file order."""
    return parser.sections()


def command_settings(parser, command):
    """`command`'s effective settings (its section over [DEFAULT]) as a dict, None if unregistered."""
    return dict(parser[command]) if parser.has_section(command) else None


def defaults(parser):
    """[DEFAULT] as a dict."""
    return dict(parser[DEFAULT_SECTION])


def to_bool(text):
    return str(text).strip().lower() in ("1", "true", "yes", "on")


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


def remove_command(parser, command):
    """Drop `command`'s section; True if there was one."""
    return parser.remove_section(command)


def format_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
