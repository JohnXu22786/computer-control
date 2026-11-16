"""Permission checks for the host-only control directive file.

The control file can widen authorization, so its file and parent must be
owned by a separate host administrator while the plugin runs unprivileged
with read-only access. On Linux the plugin must also have no permitted,
effective, ambient, or inheritable capabilities, and its bounding set must not
contain CAP_CHOWN, CAP_DAC_OVERRIDE, CAP_FOWNER, CAP_SETUID, CAP_SYS_PTRACE,
or CAP_SYS_ADMIN, and NoNewPrivs must be enabled. These capabilities can
bypass the control file's owner/mode checks, switch a process's filesystem UID
to root, control same-user-namespace processes including a trusted host
writer, or mount an attacker-controlled filesystem over the control path. The
check covers only plugin threads; the entire interactive session, including
its desktop and launchers, must inherit NoNewPrivs and a bounding set without
these bits. The runtime checks plugin threads only and cannot attest same-UID
sibling or future processes unless enforceable system-wide isolation applies.
A JSON-RPC client is not an OS principal, and desktop input can launch local
applications as the plugin user; ACLs must therefore separate that identity
from the host writer.
"""

from __future__ import annotations

import errno
import os
import stat
import struct
import sys

_WRITE_RIGHTS = (0x00000002 | 0x00000004 | 0x00000010 | 0x00000040 |
                 0x00000100 | 0x00010000 | 0x00040000 | 0x00080000 |
                 0x10000000 | 0x40000000)
_LINUX_CAPABILITY_SCAN_ATTEMPTS = 3
_LINUX_INITIAL_USER_NAMESPACE_INODE = 0xEFFFFFFD  # USER_NS_INIT_INO from linux/nsfs.h
_LINUX_CAPABILITIES_DENIED_FROM_BOUNDING_SET = (
    ("CAP_CHOWN", 1 << 0),
    ("CAP_DAC_OVERRIDE", 1 << 1),
    ("CAP_FOWNER", 1 << 3),
    ("CAP_SETUID", 1 << 7),
    ("CAP_SYS_PTRACE", 1 << 19),
    ("CAP_SYS_ADMIN", 1 << 21),
)
_WINDOWS_UNSAFE_PRIVILEGE_LUIDS = frozenset(((7, 0), (9, 0), (10, 0), (18, 0), (20, 0), (29, 0)))
_WINDOWS_CREATE_TOKEN_PRIVILEGE_NAME = "SeCreateTokenPrivilege"
_WINDOWS_TOKEN_QUERY = 0x0008
_WINDOWS_TOKEN_DUPLICATE = 0x0002
_WINDOWS_SECURITY_IMPERSONATION = 2
_WINDOWS_TOKEN_IMPERSONATION = 2
_ALLOWED_ACE_TYPES = {0, 5, 9, 11}
_DENIED_ACE_TYPES = {1, 6, 10, 12}
_NON_ACCESS_ACE_TYPES = {2, 3, 7, 8, 13, 14, 15, 16, 17, 18, 19, 20, 21}


def _validate_linux_acl(raw: bytes, path: str, acl_name: str) -> None:
    """Reject effective write grants to non-owners in a Linux ACL."""
    if len(raw) < 4 or (len(raw) - 4) % 8:
        raise PermissionError("cannot verify host control ACL for %s: malformed %s ACL" % (path, acl_name))
    version, = struct.unpack_from("<I", raw)
    if version != 2:
        raise PermissionError("cannot verify host control ACL for %s: unsupported %s ACL version %d"
                              % (path, acl_name, version))

    entries = []
    mask = None
    needs_mask = False
    tags = set()
    valid_tags = {0x01, 0x02, 0x04, 0x08, 0x10, 0x20}
    for offset in range(4, len(raw), 8):
        tag, permissions, _ = struct.unpack_from("<HHI", raw, offset)
        if tag not in valid_tags or permissions & ~0x7:
            raise PermissionError("cannot verify host control ACL for %s: malformed %s ACL entry"
                                  % (path, acl_name))
        tags.add(tag)
        if tag == 0x10:  # ACL_MASK
            mask = permissions
        if tag in (0x02, 0x08):  # ACL_USER / ACL_GROUP require ACL_MASK.
            needs_mask = True
            entries.append((permissions, True))
        elif tag == 0x04:  # ACL_GROUP_OBJ is masked when a mask exists.
            entries.append((permissions, True))
        elif tag == 0x20:  # ACL_OTHER is not masked.
            entries.append((permissions, False))

    if not {0x01, 0x04, 0x20}.issubset(tags):
        raise PermissionError("cannot verify host control ACL for %s: incomplete %s ACL"
                              % (path, acl_name))
    if needs_mask and mask is None:
        raise PermissionError("cannot verify host control ACL for %s: %s ACL has no mask"
                              % (path, acl_name))
    for permissions, masked in entries:
        effective_mask = mask if masked and mask is not None else 0x7
        if permissions & effective_mask & 0x2:  # effective ACL_WRITE
            raise PermissionError("host control %s ACL grants write access to a non-owner principal: %s"
                                  % (acl_name, path))


def _validate_linux_acl_path(path: str, is_directory: bool, fd=None) -> None:
    attributes = ["system.posix_acl_access"]
    if is_directory:
        # Temp files used for atomic replacement inherit this default ACL.
        attributes.append("system.posix_acl_default")
    getxattr = getattr(os, "getxattr", None)
    if getxattr is None:
        raise PermissionError("cannot inspect POSIX ACLs for host control path: %s" % path)
    for attribute in attributes:
        try:
            if fd is None:
                raw = getxattr(path, attribute, follow_symlinks=False)
            else:
                raw = getxattr(fd, attribute)
        except OSError as exc:
            if exc.errno == errno.ENODATA:
                continue
            raise PermissionError("cannot inspect %s on host control path %r: %s"
                                  % (attribute, path, exc))
        _validate_linux_acl(raw, path, attribute)


def _validate_windows_write_aces(records, path: str, is_directory: bool) -> None:
    """Apply the Windows DACL policy to (type, flags, mask, trusted) ACEs."""
    for ace_type, ace_flags, mask, trusted in records:
        if ace_type in _DENIED_ACE_TYPES or ace_type in _NON_ACCESS_ACE_TYPES:
            continue
        if ace_type not in _ALLOWED_ACE_TYPES:
            raise PermissionError("cannot verify host control ACL for %s: unsupported ACE type %d"
                                  % (path, ace_type))
        if not mask & _WRITE_RIGHTS or trusted:
            continue
        if ace_flags & 0x08:  # INHERIT_ONLY_ACE does not apply to this object.
            # It still affects descendant objects when OI/CI is set; a host
            # atomic-replacement temp file inherits directory ACEs.
            if not is_directory or not ace_flags & (0x01 | 0x02):
                continue
        if mask & _WRITE_RIGHTS and not trusted:
            rights = "directory replacement" if is_directory else "file modification"
            raise PermissionError("cannot verify host control ACL for %s: an untrusted principal has %s rights"
                                  % (path, rights))


def _parse_linux_capability_mask(status: bytes, field: str) -> int:
    """Return a capability mask from proc status, rejecting malformed data."""
    key = (field + ":").encode("ascii")
    entries = [line for line in status.split(b"\n") if line.startswith(key)]
    if len(entries) != 1:
        raise PermissionError("cannot verify Linux %s capabilities" % field)
    field_value = entries[0][len(key):]
    if not field_value.startswith((b" ", b"\t")):
        raise PermissionError("cannot verify Linux %s capabilities" % field)
    value = field_value.strip(b" \t")
    if len(value) != 16 or any(
            char not in b"0123456789abcdefABCDEF" for char in value):
        raise PermissionError("cannot verify Linux %s capabilities" % field)
    return int(value, 16)


def _linux_thread_has_no_new_privs(status: bytes) -> bool:
    """Return whether proc status verifiably reports NoNewPrivs=1."""
    key = b"NoNewPrivs:"
    entries = [line for line in status.split(b"\n") if line.startswith(key)]
    if len(entries) != 1:
        return False
    field_value = entries[0][len(key):]
    if not field_value.startswith((b" ", b"\t")):
        return False
    return field_value.strip(b" \t") == b"1"


def _parse_linux_uids(status: bytes) -> tuple[int, int, int, int]:
    """Return real, effective, saved, and filesystem UIDs from proc status."""
    entries = [line for line in status.split(b"\n") if line.startswith(b"Uid:")]
    if len(entries) != 1:
        raise PermissionError("cannot verify Linux process UIDs")
    fields = entries[0][len(b"Uid:"):].split()
    if len(fields) != 4 or any(not field.isdigit() or len(field) > 10 for field in fields):
        raise PermissionError("cannot verify Linux process UIDs")
    uids = tuple(int(field, 10) for field in fields)
    if any(uid > 0xFFFFFFFF for uid in uids):
        raise PermissionError("cannot verify Linux process UIDs")
    return uids


def _validate_linux_capabilities(path: str) -> None:
    """Require safe UIDs/capabilities and NoNewPrivs for every process thread."""
    task_directory = "/proc/self/task"
    for attempt in range(_LINUX_CAPABILITY_SCAN_ATTEMPTS):
        transient_change = None
        try:
            try:
                task_ids = os.listdir(task_directory)
            except (FileNotFoundError, PermissionError) as exc:
                transient_change = exc

            if transient_change is None:
                if not task_ids or any(not task_id.isascii() or not task_id.isdecimal()
                                       for task_id in task_ids):
                    raise PermissionError("cannot verify Linux process threads")

                has_capabilities = False
                has_root_uid = False
                has_no_new_privs = True
                acquirable_write_bypasses = 0
                for task_id in task_ids:
                    status_path = os.path.join(task_directory, task_id, "status")
                    try:
                        with open(status_path, "rb") as status_file:
                            status = status_file.read()
                    except (FileNotFoundError, PermissionError) as exc:
                        transient_change = exc
                        break
                    has_root_uid = has_root_uid or any(
                        uid == 0 for uid in _parse_linux_uids(status))
                    has_no_new_privs = (has_no_new_privs and
                                        _linux_thread_has_no_new_privs(status))
                    permitted = _parse_linux_capability_mask(status, "CapPrm")
                    effective = _parse_linux_capability_mask(status, "CapEff")
                    ambient = _parse_linux_capability_mask(status, "CapAmb")
                    inheritable = _parse_linux_capability_mask(status, "CapInh")
                    bounding = _parse_linux_capability_mask(status, "CapBnd")
                    has_capabilities = has_capabilities or bool(
                        permitted or effective or ambient or inheritable)
                    for _, capability_bit in _LINUX_CAPABILITIES_DENIED_FROM_BOUNDING_SET:
                        acquirable_write_bypasses |= bounding & capability_bit

                if transient_change is None:
                    try:
                        current_task_ids = os.listdir(task_directory)
                    except (FileNotFoundError, PermissionError) as exc:
                        transient_change = exc
                    else:
                        if set(current_task_ids) != set(task_ids):
                            transient_change = RuntimeError(
                                "Linux process threads changed during capability inspection")
        except (OSError, UnicodeError) as exc:
            raise PermissionError(
                    "cannot inspect process-thread security status for host control path: %s"
                % path) from exc

        if transient_change is not None:
            if attempt + 1 < _LINUX_CAPABILITY_SCAN_ATTEMPTS:
                continue
            raise PermissionError(
                "cannot inspect stable process-thread security status for host control path: %s"
                % path) from transient_change
        if has_root_uid:
            raise PermissionError(
                "plugin process threads must not retain UID 0 for host control path: %s"
                % path)
        if has_capabilities:
            raise PermissionError("plugin process threads must not have permitted, effective, "
                                  "ambient, or inheritable Linux capabilities for host control "
                                  "path: %s" % path)
        if acquirable_write_bypasses:
            capabilities = ", ".join(
                name for name, bit in _LINUX_CAPABILITIES_DENIED_FROM_BOUNDING_SET
                if acquirable_write_bypasses & bit)
            raise PermissionError("plugin process threads must not be able to acquire %s "
                                  "from file capabilities for host control path: %s"
                                  % (capabilities, path))
        if not has_no_new_privs:
            raise PermissionError("plugin process threads must have NoNewPrivs=1 for host control "
                                  "path: %s" % path)
        return


def _validate_host_control_identity(path: str, host_owned: bool,
                                   plugin_unprivileged: bool) -> None:
    """Require a host-admin-owned path and a separate unprivileged reader."""
    if not host_owned:
        raise PermissionError("host control path must be owned by the host administrator: %s" % path)
    if not plugin_unprivileged:
        raise PermissionError("plugin must run unprivileged, separately from the host-control owner: %s"
                              % path)
    if sys.platform.startswith("linux"):
        _validate_linux_capabilities(path)


def _linux_uid_zero_maps_to_host_root() -> bool:
    """Trust UID 0 only when the process is in the initial user namespace."""
    try:
        return os.stat("/proc/self/ns/user").st_ino == _LINUX_INITIAL_USER_NAMESPACE_INODE
    except OSError:
        return False


def _windows_token_is_unprivileged(token_is_elevated: bool, token_elevation_type: int,
                                   admin_member: bool) -> bool:
    """Reject elevated, administrator, and split-token Windows identities."""
    # TokenElevationTypeDefault means this identity has no linked UAC token.
    return (not token_is_elevated and token_elevation_type == 1 and not admin_member)


def _windows_token_privileges_are_safe(privileges, additional_unsafe_luids=()) -> bool:
    """Reject privileges that can bypass host-control isolation, even when disabled."""
    if privileges is None:
        return False
    try:
        unsafe_luids = _WINDOWS_UNSAFE_PRIVILEGE_LUIDS | frozenset(additional_unsafe_luids)
        for privilege in privileges:
            if (not isinstance(privilege, (tuple, list)) or len(privilege) != 3 or
                    any(not isinstance(value, int) for value in privilege)):
                return False
            low_part, high_part, attributes = privilege
            if (not 0 <= low_part <= 0xFFFFFFFF or
                    not -0x80000000 <= high_part <= 0x7FFFFFFF or
                    not 0 <= attributes <= 0xFFFFFFFF):
                return False
            if (low_part, high_part) in unsafe_luids:
                return False
    except Exception:
        return False
    return True


def _validate_windows_directory_attributes(attributes: int, path: str) -> None:
    """Require a real directory, not a reparse point, for host control."""
    if attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
        raise PermissionError("host control parent must not be a reparse point: %s" % path)
    if not attributes & 0x10:  # FILE_ATTRIBUTE_DIRECTORY
        raise PermissionError("host control parent is not a directory: %s" % path)


def validate_control_directory(path: str, fd=None) -> None:
    """Require the control file's parent directory to be host-owned/private."""
    if sys.platform.startswith("linux"):
        try:
            info = os.fstat(fd) if fd is not None else os.stat(path)
        except OSError as exc:
            raise PermissionError("cannot inspect host control directory %r: %s" % (path, exc))
        if not stat.S_ISDIR(info.st_mode):
            raise PermissionError("host control parent is not a directory: %s" % path)
        host_owned = info.st_uid == 0 and _linux_uid_zero_maps_to_host_root()
        _validate_host_control_identity(path, host_owned=host_owned,
                                        plugin_unprivileged=os.geteuid() != 0)
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError(
                "host control directory must not be group/world writable: %s"
                % path)
        _validate_linux_acl_path(path, is_directory=True, fd=fd)
        return
    if os.name == "nt":
        _validate_windows_control_directory(path)
        return
    raise PermissionError("host control ACLs cannot be verified on this platform")


def validate_control_file(path: str, info=None, fd=None) -> None:
    """Require an existing directive to be a protected regular file."""
    try:
        info = os.fstat(fd) if fd is not None else (info or os.lstat(path))
    except OSError as exc:
        raise PermissionError("cannot inspect host control file %r: %s" % (path, exc))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PermissionError("host control path must be a regular, non-symlink file: %s" % path)
    if sys.platform.startswith("linux"):
        host_owned = info.st_uid == 0 and _linux_uid_zero_maps_to_host_root()
        _validate_host_control_identity(path, host_owned=host_owned,
                                        plugin_unprivileged=os.geteuid() != 0)
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError(
                "host control file must not be group/world writable: %s"
                % path)
        _validate_linux_acl_path(path, is_directory=False, fd=fd)
        return
    if os.name == "nt":
        _validate_windows_acl(path, is_directory=False)
        return
    raise PermissionError("host control ACLs cannot be verified on this platform")


def read_control_file(path: str) -> str:
    """Open and read a protected control file, rejecting symlinks on POSIX."""
    if not os.path.basename(path):
        raise PermissionError("host control path must name a file: %s" % path)
    if sys.platform.startswith("linux"):
        if not hasattr(os, "O_NOFOLLOW"):
            raise PermissionError("host control file cannot be opened without following symlinks")
        # A path can be replaced with a FIFO between validation and open.
        # O_NONBLOCK prevents that special-file race from hanging startup;
        # fstat below then rejects anything except a regular file.
        directory_path = os.path.dirname(path) or os.curdir
        directory_flags = os.O_RDONLY | os.O_DIRECTORY
        try:
            directory_fd = os.open(directory_path, directory_flags)
        except OSError as exc:
            raise PermissionError("cannot inspect host control directory %r: %s"
                                  % (directory_path, exc)) from exc
        try:
            validate_control_directory(directory_path, fd=directory_fd)
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            fd = os.open(os.path.basename(path), flags, dir_fd=directory_fd)
            with os.fdopen(fd, "r", encoding="utf-8") as fh:
                validate_control_file(path, fd=fh.fileno())
                return fh.read()
        finally:
            os.close(directory_fd)
    if sys.platform.startswith("win"):
        validate_control_directory(os.path.dirname(path) or os.curdir)
        return _read_windows_control_file(path)

    validate_control_directory(os.path.dirname(path) or os.curdir)
    validate_control_file(path)
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _open_windows_control_directory(path: str):
    """Open the directory itself for attribute and security checks."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                            wintypes.HANDLE]
    create_file.restype = wintypes.HANDLE
    # FILE_READ_ATTRIBUTES | READ_CONTROL, shared for read/write/delete;
    # OPEN_REPARSE_POINT lets validation reject a final junction or symlink.
    handle = create_file(path, 0x80 | 0x00020000, 0x1 | 0x2 | 0x4, None, 3,
                         0x00200000 | 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error in (2, 3):  # ERROR_FILE_NOT_FOUND / ERROR_PATH_NOT_FOUND
            raise PermissionError("host control directory does not exist: %s (Windows error %d)"
                                  % (path, error))
        raise PermissionError("cannot open host control directory %r: Windows error %d"
                              % (path, error))
    return handle


def _close_windows_control_directory_handle(handle) -> None:
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    if not close_handle(handle):
        raise PermissionError("cannot close opened host control directory: Windows error %d"
                              % ctypes.get_last_error())


def _validate_windows_control_directory_handle(handle, path: str) -> None:
    """Check directory type and ACL on the same opened Windows object."""
    import ctypes
    from ctypes import wintypes

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_file_type = kernel32.GetFileType
    get_file_type.argtypes = [wintypes.HANDLE]
    get_file_type.restype = wintypes.DWORD
    if get_file_type(handle) != 1:  # FILE_TYPE_DISK
        raise PermissionError("host control parent is not a disk directory: %s" % path)

    info = FileAttributeTagInfo()
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    get_info.restype = wintypes.BOOL
    if not get_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise PermissionError("cannot inspect opened host control directory %s: Windows error %d"
                              % (path, ctypes.get_last_error()))
    _validate_windows_directory_attributes(info.FileAttributes, path)
    _validate_windows_acl(path, is_directory=True, handle=handle)


def _validate_windows_control_directory(path: str) -> None:
    handle = _open_windows_control_directory(path)
    try:
        _validate_windows_control_directory_handle(handle, path)
    finally:
        _close_windows_control_directory_handle(handle)


def _open_windows_control_file(path: str) -> int:
    """Open without following a final reparse point and return its file descriptor."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                            wintypes.HANDLE]
    create_file.restype = wintypes.HANDLE
    handle = create_file(path, 0x80000000, 0x1 | 0x4, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error == 2:  # ERROR_FILE_NOT_FOUND for the final control file.
            raise FileNotFoundError(error, "host control file does not exist", path)
        if error == 3:  # ERROR_PATH_NOT_FOUND means the parent path is unavailable.
            raise PermissionError("host control parent directory does not exist: %s" % path)
        raise PermissionError("cannot open host control file %r: Windows error %d" % (path, error))
    import msvcrt

    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(handle)
        raise


def _windows_handle_for_fd(fd: int):
    import msvcrt

    return msvcrt.get_osfhandle(fd)


def _read_windows_control_file(path: str) -> str:
    """Validate and read the same Windows file handle, closing the path race."""
    fd = _open_windows_control_file(path)
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        handle = _windows_handle_for_fd(fh.fileno())
        _validate_windows_control_handle(handle, path)
        return fh.read()


def _check_windows_admin_membership(advapi32, kernel32, token, admins_sid,
                                   path: str) -> bool:
    """Check Administrators membership using a duplicated impersonation token."""
    import ctypes
    from ctypes import wintypes

    def fail(message):
        raise PermissionError("cannot verify host control ACL for %s: %s" % (path, message))

    impersonation_token = wintypes.HANDLE()
    duplicate_token = advapi32.DuplicateTokenEx
    duplicate_token.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                ctypes.c_int, ctypes.c_int,
                                ctypes.POINTER(wintypes.HANDLE)]
    duplicate_token.restype = wintypes.BOOL
    if not duplicate_token(token, _WINDOWS_TOKEN_QUERY | _WINDOWS_TOKEN_DUPLICATE,
                           None, _WINDOWS_SECURITY_IMPERSONATION,
                           _WINDOWS_TOKEN_IMPERSONATION, ctypes.byref(impersonation_token)):
        fail("DuplicateTokenEx(Administrators membership) failed")

    try:
        admin_member = wintypes.BOOL()
        check_token_membership = advapi32.CheckTokenMembership
        check_token_membership.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                           ctypes.POINTER(wintypes.BOOL)]
        check_token_membership.restype = wintypes.BOOL
        if not check_token_membership(impersonation_token, admins_sid,
                                      ctypes.byref(admin_member)):
            fail("CheckTokenMembership(Administrators) failed")
        return bool(admin_member.value)
    finally:
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        if not kernel32.CloseHandle(impersonation_token):
            fail("CloseHandle(Administrators impersonation token) failed")


def _validate_windows_control_handle(handle, path: str) -> None:
    """Reject reparse points and verify the opened file's DACL by handle."""
    import ctypes
    from ctypes import wintypes

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_file_type = kernel32.GetFileType
    get_file_type.argtypes = [wintypes.HANDLE]
    get_file_type.restype = wintypes.DWORD
    if get_file_type(handle) != 1:  # FILE_TYPE_DISK
        raise PermissionError("host control path is not a disk file: %s" % path)

    info = FileAttributeTagInfo()
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    get_info.restype = wintypes.BOOL
    if not get_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise PermissionError("cannot inspect opened host control file %s: Windows error %d"
                              % (path, ctypes.get_last_error()))
    if info.FileAttributes & (0x10 | 0x400):  # directory or reparse point
        raise PermissionError("host control path must be a regular, non-reparse file: %s" % path)
    _validate_windows_acl(path, is_directory=False, handle=handle)


def _validate_windows_acl(path: str, is_directory: bool, handle=None) -> None:
    """Reject Windows DACLs that grant write/replace rights to other users.

    Only LocalSystem and built-in Administrators may own or write the path.
    The plugin process must be unprivileged so desktop input cannot use its
    identity to write host directives. The parent directory is checked
    separately because write access there can replace an otherwise read-only
    file.
    """
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class AclSizeInfo(ctypes.Structure):
        _fields_ = [("AceCount", wintypes.DWORD),
                    ("AclBytesInUse", wintypes.DWORD),
                    ("AclBytesFree", wintypes.DWORD)]

    class AceHeader(ctypes.Structure):
        _fields_ = [("AceType", ctypes.c_ubyte),
                    ("AceFlags", ctypes.c_ubyte),
                    ("AceSize", wintypes.WORD)]

    class TokenElevation(ctypes.Structure):
        _fields_ = [("TokenIsElevated", wintypes.DWORD)]

    class Luid(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class LuidAndAttributes(ctypes.Structure):
        _fields_ = [("Luid", Luid), ("Attributes", wintypes.DWORD)]

    def fail(message):
        raise PermissionError("cannot verify host control ACL for %s: %s" % (path, message))

    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    if handle is None:
        get_security = advapi32.GetNamedSecurityInfoW
        get_security.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                 ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                 ctypes.POINTER(ctypes.c_void_p)]
        get_security.restype = wintypes.DWORD
        error = get_security(path, 1, 0x1 | 0x4, ctypes.byref(owner), None,
                             ctypes.byref(dacl), None, ctypes.byref(descriptor))
        error_source = "GetNamedSecurityInfoW"
    else:
        get_security = advapi32.GetSecurityInfo
        get_security.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                 ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                 ctypes.POINTER(ctypes.c_void_p)]
        get_security.restype = wintypes.DWORD
        error = get_security(handle, 1, 0x1 | 0x4, ctypes.byref(owner), None,
                             ctypes.byref(dacl), None, ctypes.byref(descriptor))
        error_source = "GetSecurityInfo"
    if error:
        fail("%s returned %d" % (error_source, error))
    try:
        token = wintypes.HANDLE()
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                               ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                         _WINDOWS_TOKEN_QUERY | _WINDOWS_TOKEN_DUPLICATE,
                                         ctypes.byref(token)):
            fail("OpenProcessToken failed")
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        system_sid_buf = ctypes.create_string_buffer(
            b"\x01\x01\x00\x00\x00\x00\x00\x05\x12\x00\x00\x00")
        admins_sid_buf = ctypes.create_string_buffer(
            b"\x01\x02\x00\x00\x00\x00\x00\x05\x20\x00\x00\x00\x20\x02\x00\x00")
        system_sid = ctypes.cast(system_sid_buf, ctypes.c_void_p)
        admins_sid = ctypes.cast(admins_sid_buf, ctypes.c_void_p)
        try:
            needed = wintypes.DWORD()
            get_token_info = advapi32.GetTokenInformation
            get_token_info.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
            get_token_info.restype = wintypes.BOOL
            elevation = TokenElevation()
            if not get_token_info(token, 20, ctypes.byref(elevation),
                                  ctypes.sizeof(elevation), ctypes.byref(needed)):
                fail("GetTokenInformation(TokenElevation) failed")

            elevation_type = wintypes.DWORD()
            if not get_token_info(token, 18, ctypes.byref(elevation_type),
                                  ctypes.sizeof(elevation_type), ctypes.byref(needed)):
                fail("GetTokenInformation(TokenElevationType) failed")

            lookup_privilege_value = advapi32.LookupPrivilegeValueW
            lookup_privilege_value.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                               ctypes.POINTER(Luid)]
            lookup_privilege_value.restype = wintypes.BOOL
            create_token_luid = Luid()
            if not lookup_privilege_value(None, _WINDOWS_CREATE_TOKEN_PRIVILEGE_NAME,
                                          ctypes.byref(create_token_luid)):
                fail("LookupPrivilegeValueW(%s) failed" %
                     _WINDOWS_CREATE_TOKEN_PRIVILEGE_NAME)

            privilege_buffer_size = wintypes.DWORD()
            if get_token_info(token, 3, None, 0, ctypes.byref(privilege_buffer_size)):
                fail("GetTokenInformation(TokenPrivileges) unexpectedly succeeded without a buffer")
            if (ctypes.get_last_error() != 122 or
                    privilege_buffer_size.value < ctypes.sizeof(wintypes.DWORD)):
                fail("GetTokenInformation(TokenPrivileges) size query failed")
            privilege_buffer = ctypes.create_string_buffer(privilege_buffer_size.value)
            if not get_token_info(token, 3, privilege_buffer,
                                  privilege_buffer_size.value,
                                  ctypes.byref(privilege_buffer_size)):
                fail("GetTokenInformation(TokenPrivileges) failed")
            privilege_count = ctypes.cast(
                privilege_buffer, ctypes.POINTER(wintypes.DWORD)).contents.value
            privilege_header_size = ctypes.sizeof(wintypes.DWORD)
            privilege_entry_size = ctypes.sizeof(LuidAndAttributes)
            if privilege_count > ((privilege_buffer_size.value - privilege_header_size) //
                                  privilege_entry_size):
                fail("GetTokenInformation(TokenPrivileges) returned a truncated list")
            token_privileges = []
            for index in range(privilege_count):
                entry_address = (ctypes.addressof(privilege_buffer) + privilege_header_size +
                                 index * privilege_entry_size)
                entry = ctypes.cast(entry_address,
                                    ctypes.POINTER(LuidAndAttributes)).contents
                token_privileges.append((entry.Luid.LowPart, entry.Luid.HighPart,
                                         entry.Attributes))
            if not _windows_token_privileges_are_safe(
                    token_privileges,
                    {(create_token_luid.LowPart, create_token_luid.HighPart)}):
                fail("token has an unsafe or unverifiable privilege")

            plugin_unprivileged = _windows_token_is_unprivileged(
                bool(elevation.TokenIsElevated), elevation_type.value,
                _check_windows_admin_membership(advapi32, kernel32, token,
                                                admins_sid, path))
        finally:
            kernel32.CloseHandle(token)

        equal_sid = advapi32.EqualSid
        equal_sid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        equal_sid.restype = wintypes.BOOL
        trusted_sids = (system_sid, admins_sid)
        _validate_host_control_identity(
            path,
            host_owned=bool(owner.value and any(equal_sid(owner, sid) for sid in trusted_sids)),
            plugin_unprivileged=plugin_unprivileged)

        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        get_dacl = advapi32.GetSecurityDescriptorDacl
        get_dacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                             ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
        get_dacl.restype = wintypes.BOOL
        if not get_dacl(descriptor, ctypes.byref(present), ctypes.byref(dacl),
                        ctypes.byref(defaulted)) or not present.value or not dacl.value:
            fail("missing or null DACL grants no enforceable restriction")

        size_info = AclSizeInfo()
        get_acl_info = advapi32.GetAclInformation
        get_acl_info.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
        get_acl_info.restype = wintypes.BOOL
        if not get_acl_info(dacl, ctypes.byref(size_info), ctypes.sizeof(size_info), 2):
            fail("GetAclInformation failed")
        get_ace = advapi32.GetAce
        get_ace.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        get_ace.restype = wintypes.BOOL
        records = []
        for index in range(size_info.AceCount):
            ace = ctypes.c_void_p()
            if not get_ace(dacl, index, ctypes.byref(ace)):
                fail("GetAce failed")
            header = ctypes.cast(ace, ctypes.POINTER(AceHeader)).contents
            if header.AceType in _DENIED_ACE_TYPES or header.AceType in _NON_ACCESS_ACE_TYPES:
                records.append((header.AceType, header.AceFlags, 0, False))
                continue
            if header.AceType not in _ALLOWED_ACE_TYPES:
                fail("unsupported ACE type %d" % header.AceType)
            address = ace.value
            mask = ctypes.cast(address + 4, ctypes.POINTER(wintypes.DWORD)).contents.value
            sid_offset = 8
            if header.AceType in (5, 11):
                flags = ctypes.cast(address + 8, ctypes.POINTER(wintypes.DWORD)).contents.value
                if flags & ~0x3:
                    fail("unsupported object ACE flags 0x%x" % flags)
                sid_offset = 12 + (16 if flags & 1 else 0) + (16 if flags & 2 else 0)
            ace_sid = ctypes.c_void_p(address + sid_offset)
            trusted = any(equal_sid(ace_sid, sid) for sid in trusted_sids)
            records.append((header.AceType, header.AceFlags, mask, trusted))
        _validate_windows_write_aces(records, path, is_directory)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(descriptor)
