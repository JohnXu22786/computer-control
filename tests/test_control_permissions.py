"""Platform-independent tests for host-control ACL policy decisions."""

import errno
import ctypes
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

import computer_control.control_permissions as control_permissions
from computer_control.control_permissions import (
    _validate_linux_acl,
    _validate_windows_write_aces,
    read_control_file,
    validate_control_directory,
    validate_control_file,
)


def linux_acl(named_user_permissions, mask_permissions, group_object_permissions=0x4,
              other_permissions=0x4):
    entries = [
        (0x01, 0x7, 0xFFFFFFFF),  # ACL_USER_OBJ
        (0x02, named_user_permissions, 12001),  # ACL_USER
        (0x04, group_object_permissions, 0xFFFFFFFF),  # ACL_GROUP_OBJ
        (0x10, mask_permissions, 0xFFFFFFFF),  # ACL_MASK
        (0x20, other_permissions, 0xFFFFFFFF),  # ACL_OTHER
    ]
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)


class TestWindowsAclPolicy(unittest.TestCase):
    def test_missing_windows_control_file_maps_to_not_found(self):
        winapi = mock.Mock()
        winapi.CreateFileW.return_value = ctypes.c_void_p(-1).value
        with mock.patch.object(ctypes, "WinDLL", return_value=winapi, create=True), \
                mock.patch.object(ctypes, "get_last_error", return_value=2, create=True), \
                mock.patch.dict(sys.modules, {"msvcrt": mock.Mock()}):
            with self.assertRaises(FileNotFoundError):
                control_permissions._open_windows_control_file("missing-control.json")

    def test_windows_reader_rejects_file_swapped_before_open_by_handle_acl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "control.json")
            replacement = os.path.join(directory, "replacement.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("trusted")
            with open(replacement, "w", encoding="utf-8") as fh:
                fh.write("attacker")
            replacement_inode = os.stat(replacement).st_ino

            def swap_then_open(open_path):
                os.replace(replacement, open_path)
                return os.open(open_path, os.O_RDONLY)

            def replace_after_path_check(checked_path):
                os.replace(replacement, checked_path)

            def reject_untrusted_handle(handle, checked_path):
                self.assertEqual(checked_path, path)
                if os.fstat(handle).st_ino == replacement_inode:
                    raise PermissionError("untrusted file handle")

            with mock.patch.object(control_permissions.sys, "platform", "win32"), \
                    mock.patch.object(control_permissions, "validate_control_directory"), \
                    mock.patch.object(control_permissions, "validate_control_file",
                                      side_effect=replace_after_path_check) as path_check, \
                    mock.patch.object(control_permissions, "_open_windows_control_file",
                                      side_effect=swap_then_open, create=True), \
                    mock.patch.object(control_permissions, "_windows_handle_for_fd",
                                      side_effect=lambda fd: fd, create=True), \
                    mock.patch.object(control_permissions, "_validate_windows_control_handle",
                                      side_effect=reject_untrusted_handle, create=True):
                with self.assertRaises(PermissionError):
                    read_control_file(path)
            path_check.assert_not_called()

    def test_untrusted_write_ace_is_rejected(self):
        with self.assertRaises(PermissionError):
            _validate_windows_write_aces([(0, 0, 0x40000000, False)],
                                         "C:\\host\\control.json", False)

    def test_host_control_owner_requires_separate_unprivileged_plugin(self):
        policy = getattr(control_permissions, "_validate_host_control_identity")
        policy("control.json", host_owned=True, plugin_unprivileged=True)
        with self.assertRaises(PermissionError):
            policy("control.json", host_owned=False, plugin_unprivileged=True)
        with self.assertRaises(PermissionError):
            policy("control.json", host_owned=True, plugin_unprivileged=False)

    @unittest.skipUnless(os.name == "nt", "native Windows owner policy")
    def test_plugin_owned_windows_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PermissionError):
                validate_control_directory(directory)

    def test_trusted_writers_and_nonwrite_aces_are_allowed(self):
        _validate_windows_write_aces([
            (0, 0, 0x40000000, True),       # trusted host identity
            (0, 0, 0x00000001, False),      # read-only access
            (1, 0, 0x40000000, False),      # deny ACE cannot grant access
            (17, 0, 0x00000001, False),     # mandatory label, not an access grant
            (0, 0x08, 0x40000000, False),   # inherit-only ACE is not effective here
        ], "C:\\host\\control.json", False)

    def test_inherit_only_write_ace_to_child_files_is_rejected(self):
        with self.assertRaises(PermissionError):
            _validate_windows_write_aces([(0, 0x09, 0x40000000, False)],
                                         "C:\\host", True)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux POSIX ACL policy")
class TestLinuxAclPolicy(unittest.TestCase):
    def test_fifo_control_path_is_rejected_without_blocking_open(self):
        with tempfile.TemporaryDirectory() as directory:
            fifo_path = os.path.join(directory, "control.json")
            os.mkfifo(fifo_path)
            real_open = os.open

            def require_nonblocking(path, flags, *args, **kwargs):
                self.assertTrue(flags & os.O_NONBLOCK,
                                "special control paths must use nonblocking open")
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(control_permissions, "_validate_host_control_identity"), \
                    mock.patch("computer_control.control_permissions.os.open",
                               side_effect=require_nonblocking):
                with self.assertRaises(PermissionError):
                    read_control_file(fifo_path)

    def test_plugin_owned_control_parent_and_file_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "control.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{}")
            with self.assertRaises(PermissionError):
                validate_control_directory(directory)
            with self.assertRaises(PermissionError):
                validate_control_file(path)

    def test_unmasked_named_user_write_acl_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            file_path = os.path.join(directory, "control.json")
            with open(file_path, "w", encoding="utf-8") as fh:
                fh.write("{}")
            write_acl = linux_acl(named_user_permissions=0x6, mask_permissions=0x6)
            safe_acl = linux_acl(named_user_permissions=0x6, mask_permissions=0x4)
            group_write_acl = linux_acl(named_user_permissions=0x4, mask_permissions=0x6,
                                        group_object_permissions=0x6)

            def fake_getxattr(path, attribute, follow_symlinks=False):
                if path == directory and attribute == "system.posix_acl_access":
                    return safe_acl
                if path == directory and attribute == "system.posix_acl_default":
                    return group_write_acl
                if path == file_path and attribute == "system.posix_acl_access":
                    return write_acl
                raise OSError(errno.ENODATA, "no extended ACL")

            with mock.patch.object(control_permissions, "_validate_host_control_identity"), \
                    mock.patch("computer_control.control_permissions.os.getxattr",
                               side_effect=fake_getxattr):
                with self.assertRaises(PermissionError):
                    validate_control_directory(directory)
                with self.assertRaises(PermissionError):
                    validate_control_file(file_path)

    def test_default_acl_group_object_and_other_writes_are_rejected(self):
        for acl in (linux_acl(0x4, 0x6, group_object_permissions=0x2),
                    linux_acl(0x4, 0x4, other_permissions=0x2)):
            with self.subTest(acl=acl):
                with self.assertRaises(PermissionError):
                    _validate_linux_acl(acl, "/host", "system.posix_acl_default")

    def test_named_user_write_masked_off_is_not_effective(self):
        _validate_linux_acl(linux_acl(named_user_permissions=0x6, mask_permissions=0x4),
                            "/host/control.json", "system.posix_acl_access")


if __name__ == "__main__":
    unittest.main()
