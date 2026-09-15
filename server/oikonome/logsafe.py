"""One log record per line, whatever the message carried.

Much of what the app logs is text somebody else chose — an email address
from a webhook, a merchant label from an import, a header. A line break
inside one of those would end the record early and start a forged one,
which is the whole trick behind log injection: a reader, human or a tool
like fail2ban, trusts line boundaries. Rather than sanitise at every call
site (there are dozens, and the next one would be missed), the record
factory flattens the message and its string arguments once, for every
handler, in every process that calls install().
"""
import logging

_BREAKS = str.maketrans({"\r": "\\r", "\n": "\\n"})


class _FlatError:
    """An exception argument, rendered without line breaks. Kept as a
    wrapper rather than a string so `%r` still shows the exception's repr
    and `%s` its message — a database error carries several lines and a
    validation error echoes what the user typed."""
    __slots__ = ("_e",)

    def __init__(self, e):
        self._e = e

    def __str__(self):
        return str(self._e).translate(_BREAKS)

    def __repr__(self):
        return repr(self._e).translate(_BREAKS)


def _flat(v):
    if isinstance(v, str):
        return v.translate(_BREAKS)
    if isinstance(v, BaseException):
        return _FlatError(v)
    return v


def install() -> None:
    """Idempotent; safe to call from the app and the worker alike."""
    current = logging.getLogRecordFactory()
    if getattr(current, "_oikonome_flat", False):
        return

    def factory(*args, **kwargs):
        record = current(*args, **kwargs)
        record.msg = _flat(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_flat(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _flat(v) for k, v in record.args.items()}
        return record

    factory._oikonome_flat = True
    logging.setLogRecordFactory(factory)
