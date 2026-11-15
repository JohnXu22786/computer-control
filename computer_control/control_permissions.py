"""Permission checks for the host-only control directive file.

The control file can widen authorization, so its file and parent must be
owned by a separate host administrator while the plugin runs unprivileged
with read-only access. A JSON-RPC client is not an OS principal, and desktop
input can launch local applications as the plugin user; ACLs must therefore
separate that identity from the host writer.
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


def _validate_linux_acl_path(path: str, is_directory: bool) -> None:
    attributes = ["system.posix_acl_access"]
    if is_directory:
        # Temp files used for atomic replacement inherit this default ACL.
        attributes.append("system.posix_acl_default")
    getxattr = getattr(os, "getxattr", None)
    if getxattr is None:
        raise PermissionError("cannot inspect POSIX ACLs for host control path: %s" % path)
    for attribute in attributes:
        try:
            raw = getxattr(path, attribute, follow_symlinks=False)
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


def _validate_host_control_identity(path: str, host_owned: bool,
                                   plugin_unprivileged: bool) -> None:
    """Require a host-admin-owned path and a separate unprivileged reader."""
    if not host_owned:
        raise PermissionError("host control path must be owned by the host administrator: %s" % path)
    if not plugin_unprivileged:
        raise PermissionError("plugin must run unprivileged, separately from the host-control owner: %s"
                              % path)


def validate_control_directory(path: str) -> None:
    """Require the control file's parent directory to be host-owned/private."""
    if sys.platform.startswith("linux"):
        try:
            info = os.stat(path)
        except OSError as exc:
            raise PermissionError("cannot inspect host control directory %r: %s" % (path, exc))
        if not stat.S_ISDIR(info.st_mode):
            raise PermissionError("host control parent is not a directory: %s" % path)
        _validate_host_control_identity(path, host_owned=info.st_uid == 0,
                                        plugin_unprivileged=os.geteuid() != 0)
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError(
                "host control directory must not be group/world writable: %s"
                % path)
        _validate_linux_acl_path(path, is_directory=True)
        return
    if os.name == "nt":
        _validate_windows_acl(path, is_directory=True)
        return
    raise PermissionError("host control ACLs cannot be verified on this platform")


def validate_control_file(path: str, info=None) -> None:
    """Require an existing directive to be a protected regular file."""
    try:
        info = info or os.lstat(path)
    except OSError as exc:
        raise PermissionError("cannot inspect host control file %r: %s" % (path, exc))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PermissionError("host control path must be a regular, non-symlink file: %s" % path)
    if sys.platform.startswith("linux"):
        _validate_host_control_identity(path, host_owned=info.st_uid == 0,
                                        plugin_unprivileged=os.geteuid() != 0)
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError(
                "host control file must not be group/world writable: %s"
                % path)
        _validate_linux_acl_path(path, is_directory=False)
        return
    if os.name == "nt":
        _validate_windows_acl(path, is_directory=False)
        return
    raise PermissionError("host control ACLs cannot be verified on this platform")


def read_control_file(path: str) -> str:
    """Open and read a protected control file, rejecting symlinks on POSIX."""
    validate_control_directory(os.path.dirname(path) or os.curdir)
    if sys.platform.startswith("linux"):
        if not hasattr(os, "O_NOFOLLOW"):
            raise PermissionError("host control file cannot be opened without following symlinks")
        # A path can be replaced with a FIFO between validation and open.
        # O_NONBLOCK prevents that special-file race from hanging startup;
        # fstat below then rejects anything except a regular file.
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(path, flags)
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            validate_control_file(path, os.fstat(fh.fileno()))
            return fh.read()
    if sys.platform.startswith("win"):
        return _read_windows_control_file(path)

    validate_control_file(path)
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


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
        if error in (2, 3):  # ERROR_FILE_NOT_FOUND / ERROR_PATH_NOT_FOUND
            raise FileNotFoundError(error, "host control file does not exist", path)
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
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008,
                                         ctypes.byref(token)):
            fail("OpenProcessToken failed")
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
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
        finally:
            kernel32.CloseHandle(token)

        equal_sid = advapi32.EqualSid
        equal_sid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        equal_sid.restype = wintypes.BOOL
        system_sid_buf = ctypes.create_string_buffer(
            b"\x01\x01\x00\x00\x00\x00\x00\x05\x12\x00\x00\x00")
        admins_sid_buf = ctypes.create_string_buffer(
            b"\x01\x02\x00\x00\x00\x00\x00\x05\x20\x00\x00\x00\x20\x02\x00\x00")
        system_sid = ctypes.cast(system_sid_buf, ctypes.c_void_p)
        admins_sid = ctypes.cast(admins_sid_buf, ctypes.c_void_p)
        trusted_sids = (system_sid, admins_sid)
        _validate_host_control_identity(
            path,
            host_owned=bool(owner.value and any(equal_sid(owner, sid) for sid in trusted_sids)),
            plugin_unprivileged=not bool(elevation.TokenIsElevated))

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
