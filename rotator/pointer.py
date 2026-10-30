"""Read and atomically rewrite Orca's global ``selected-host`` account pointer."""
from __future__ import annotations

import os
import tempfile
from typing import Callable, Optional

from rotator.models import PROFILES_ROOT_DEFAULT, Profile, ReadText

POINTER_FILENAME = "selected-host"


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        raise OSError("file is not valid UTF-8") from None


def read_selected_host(root: Optional[str] = None, *, read_text: Optional[ReadText] = None) -> Optional[str]:
    """Return the pointer text exactly as stored ("" when empty), or None when missing or unreadable."""
    root = PROFILES_ROOT_DEFAULT if root is None else root
    rt = _read_text if read_text is None else read_text
    try:
        return rt(os.path.join(root, POINTER_FILENAME))
    except OSError:
        return None


def _validate(target: Profile, root: str) -> None:
    if not (os.path.isabs(root) and os.path.isdir(root)):
        raise ValueError("pointer root must be an existing absolute directory")
    if target.account_id in ("", ".", "..") or os.sep in target.account_id:
        raise ValueError("account_id must be one path component")
    if not os.path.isabs(target.home):
        raise ValueError("target home must be absolute")
    if os.path.normpath(target.home) != os.path.normpath(os.path.join(root, target.account_id, "home")):
        raise ValueError("target home is not <root>/<account_id>/home")
    if not os.path.isdir(target.home):
        raise ValueError("target home does not exist")
    if not os.path.isfile(os.path.join(root, target.account_id, "profile.json")):
        raise ValueError("target has no profile.json")
    if not os.path.realpath(target.home).startswith(os.path.realpath(root) + os.sep):
        raise ValueError("target home resolves outside the root")


def set_selected_host(
    target: Profile,
    root: Optional[str] = None,
    *,
    replace: Optional[Callable[[str, str], None]] = None,
    fsync: Optional[Callable[[int], None]] = None,
) -> Optional[str]:
    """Point ``selected-host`` at ``target.home`` and return the previous pointer text.

    Raises ValueError before touching any file when ``target`` is not a profile under ``root``.
    An OSError from the write propagates with the previous pointer left byte-identical.
    The returned previous text is a best-effort snapshot read before the replace; it is not safe to
    restore from under a concurrent writer.
    """
    root = PROFILES_ROOT_DEFAULT if root is None else root
    replace = os.replace if replace is None else replace
    fsync = os.fsync if fsync is None else fsync
    _validate(target, root)

    previous = read_selected_host(root)
    dest = os.path.join(root, POINTER_FILENAME)
    fd, tmp = tempfile.mkstemp(prefix=".selected-host.", dir=root)
    handed = False
    try:
        os.fchmod(fd, 0o600)
        f = os.fdopen(fd, "wb")
        handed = True
        with f:
            # Verbatim, no newline: Claude Code derives the Keychain service name from this exact string.
            f.write(target.home.encode("utf-8"))
            f.flush()
            fsync(f.fileno())
        replace(tmp, dest)
    except BaseException:
        if not handed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise

    try:
        dfd = os.open(root, os.O_RDONLY)
        try:
            fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass
    return previous
