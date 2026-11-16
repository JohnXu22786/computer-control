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
    def test_windows_control_directory_rejects_regular_file_before_acl_check(self):
        with tempfile.NamedTemporaryFile() as regular_file:
            handle = object()

            def reject_file_attributes(_handle, checked_path):
                control_permissions._validate_windows_directory_attributes(0, checked_path)

            with mock.patch.object(control_permissions.sys, "platform", "win32"), \
                    mock.patch.object(control_permissions.os, "name", "nt"), \
                    mock.patch.object(control_permissions,
                                      "_open_windows_control_directory", return_value=handle), \
                    mock.patch.object(control_permissions,
                                      "_validate_windows_control_directory_handle",
                                      side_effect=reject_file_attributes), \
                    mock.patch.object(control_permissions,
                                      "_close_windows_control_directory_handle"):
                with self.assertRaises(PermissionError):
                    validate_control_directory(regular_file.name)

    def test_windows_control_directory_checks_and_closes_opened_handle(self):
        path = r"C:\\host"
        handle = object()
        with mock.patch.object(control_permissions.sys, "platform", "win32"), \
                mock.patch.object(control_permissions.os, "name", "nt"), \
                mock.patch.object(control_permissions, "_open_windows_control_directory",
                                  return_value=handle, create=True) as open_directory, \
                mock.patch.object(control_permissions,
                                  "_validate_windows_control_directory_handle",
                                  create=True) as validate_handle, \
                mock.patch.object(control_permissions,
                                  "_close_windows_control_directory_handle",
                                  create=True) as close_handle, \
                mock.patch.object(control_permissions, "_validate_windows_acl"):
            validate_control_directory(path)
        open_directory.assert_called_once_with(path)
        validate_handle.assert_called_once_with(handle, path)
        close_handle.assert_called_once_with(handle)

    def test_windows_directory_type_policy_rejects_files_and_reparse_points(self):
        validate_type = control_permissions._validate_windows_directory_attributes
        validate_type(0x10, r"C:\\host")  # FILE_ATTRIBUTE_DIRECTORY
        for attributes in (0, 0x10 | 0x400):  # regular file, reparse directory
            with self.subTest(attributes=attributes), self.assertRaises(PermissionError):
                validate_type(attributes, r"C:\\host")

    def test_missing_windows_control_file_maps_to_not_found(self):
        winapi = mock.Mock()
        winapi.CreateFileW.return_value = ctypes.c_void_p(-1).value
        with mock.patch.object(ctypes, "WinDLL", return_value=winapi, create=True), \
                mock.patch.object(ctypes, "get_last_error", return_value=2, create=True), \
                mock.patch.dict(sys.modules, {"msvcrt": mock.Mock()}):
            with self.assertRaises(FileNotFoundError):
                control_permissions._open_windows_control_file("missing-control.json")

    def test_missing_windows_parent_path_is_not_missing_snapshot(self):
        cases = (
            (control_permissions._open_windows_control_directory, r"C:\\missing"),
            (control_permissions._open_windows_control_file, r"C:\\missing\\control.json"),
        )
        for open_path, path in cases:
            with self.subTest(path=path):
                winapi = mock.Mock()
                winapi.CreateFileW.return_value = ctypes.c_void_p(-1).value
                with mock.patch.object(ctypes, "WinDLL", return_value=winapi, create=True), \
                        mock.patch.object(ctypes, "get_last_error", return_value=3, create=True), \
                        mock.patch.dict(sys.modules, {"msvcrt": mock.Mock()}):
                    with self.assertRaises(PermissionError):
                        open_path(path)

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
        with mock.patch.object(control_permissions, "_validate_linux_capabilities"):
            policy("control.json", host_owned=True, plugin_unprivileged=True)
        with self.assertRaises(PermissionError):
            policy("control.json", host_owned=False, plugin_unprivileged=True)
        with self.assertRaises(PermissionError):
            policy("control.json", host_owned=True, plugin_unprivileged=False)

    def test_windows_token_rejects_dacl_bypass_privileges_even_when_disabled(self):
        policy = control_permissions._windows_token_privileges_are_safe
        # These LUIDs are SE_TAKE_OWNERSHIP_PRIVILEGE (9) and
        # SE_RESTORE_PRIVILEGE (18), as defined by the Windows SDK.
        cases = (
            ("take ownership disabled", [(9, 0, 0)]),
            ("take ownership enabled", [(9, 0, 2)]),
            ("restore disabled", [(18, 0, 0)]),
            ("restore enabled", [(18, 0, 2)]),
        )
        for label, privileges in cases:
            with self.subTest(privilege=label):
                self.assertFalse(policy(privileges))

    def test_windows_token_rejects_create_token_privilege_regardless_of_attributes(self):
        policy = control_permissions._windows_token_privileges_are_safe
        # Inject the local LUID returned by LookupPrivilegeValueW.
        create_token_luid = {(0x12345678, 1)}
        for attributes in (0, 2):
            with self.subTest(attributes=attributes):
                self.assertFalse(policy([(0x12345678, 1, attributes)], create_token_luid))
        self.assertTrue(policy([(23, 0, 2)], create_token_luid))

    def test_windows_token_rejects_debug_privilege_regardless_of_attributes(self):
        policy = control_permissions._windows_token_privileges_are_safe
        # SE_DEBUG_NAME maps to LUID 20 in the Windows privilege assignments.
        for attributes in (0, 2):
            with self.subTest(attributes=attributes):
                self.assertFalse(policy([(20, 0, attributes)]))

    def test_windows_token_rejects_impersonate_privilege_when_enabled(self):
        policy = control_permissions._windows_token_privileges_are_safe
        # SE_IMPERSONATE_NAME maps to LUID {0, 29} in the Windows privilege table.
        self.assertFalse(policy([(29, 0, 2)]))

    def test_windows_token_rejects_impersonate_privilege_when_disabled(self):
        policy = control_permissions._windows_token_privileges_are_safe
        self.assertFalse(policy([(29, 0, 0)]))

    def test_windows_token_rejects_tcb_privilege_when_enabled(self):
        policy = control_permissions._windows_token_privileges_are_safe
        # SE_TCB_NAME maps to LUID 7 in the Windows privilege assignments.
        self.assertFalse(policy([(7, 0, 2)]))

    def test_windows_token_rejects_tcb_privilege_when_disabled(self):
        policy = control_permissions._windows_token_privileges_are_safe
        self.assertFalse(policy([(7, 0, 0)]))

    def test_windows_token_rejects_load_driver_privilege_when_enabled(self):
        policy = control_permissions._windows_token_privileges_are_safe
        # SE_LOAD_DRIVER_NAME maps to LUID 10 and is unsafe even for non-admins.
        self.assertFalse(policy([(10, 0, 2)]))

    def test_windows_token_rejects_load_driver_privilege_when_disabled(self):
        policy = control_permissions._windows_token_privileges_are_safe
        self.assertFalse(policy([(10, 0, 0)]))

    def test_windows_token_allows_unrelated_privileges(self):
        policy = control_permissions._windows_token_privileges_are_safe
        self.assertTrue(policy([]))
        # SE_CHANGE_NOTIFY_NAME (23) does not affect host-control isolation.
        self.assertTrue(policy([(23, 0, 2)]))

    def test_windows_token_rejects_unverifiable_privilege_data(self):
        policy = control_permissions._windows_token_privileges_are_safe
        for privileges in (None, [(18, 0)], [("invalid", 0, 0)], [None]):
            with self.subTest(privileges=privileges):
                self.assertFalse(policy(privileges))

    def test_filtered_admin_is_rejected_but_standard_user_is_allowed(self):
        is_unprivileged = control_permissions._windows_token_is_unprivileged
        cases = (
            ("filtered administrator", False, 3, False, False),
            ("ordinary user", False, 1, False, True),
            ("elevated token", True, 2, True, False),
            ("administrator membership", False, 1, True, False),
        )
        for label, elevated, elevation_type, admin_member, expected in cases:
            with self.subTest(identity=label):
                self.assertEqual(is_unprivileged(elevated, elevation_type, admin_member),
                                 expected)

    def test_windows_admin_membership_uses_closed_impersonation_token(self):
        primary_token = ctypes.c_void_p(0x1111)
        admins_sid = ctypes.c_void_p(0x2222)
        impersonation_token_value = 0x3333
        advapi32 = mock.Mock()
        kernel32 = mock.Mock()

        def duplicate_token(_primary, _access, _attributes, _level, _type, output):
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = (
                impersonation_token_value)
            return True

        def check_membership(token, sid, member):
            self.assertEqual(token.value, impersonation_token_value)
            self.assertIs(sid, admins_sid)
            ctypes.cast(member, ctypes.POINTER(ctypes.c_int)).contents.value = 1
            return True

        advapi32.DuplicateTokenEx.side_effect = duplicate_token
        advapi32.CheckTokenMembership.side_effect = check_membership

        self.assertTrue(control_permissions._check_windows_admin_membership(
            advapi32, kernel32, primary_token, admins_sid, "control.json"))

        duplicate_args = advapi32.DuplicateTokenEx.call_args.args
        self.assertEqual(duplicate_args[0].value, primary_token.value)
        self.assertEqual(duplicate_args[1:5], (0x0008 | 0x0002, None, 2, 2))
        self.assertEqual(advapi32.CheckTokenMembership.call_count, 1)
        self.assertEqual(kernel32.CloseHandle.call_args.args[0].value,
                         impersonation_token_value)

    def test_windows_admin_membership_api_failures_are_closed(self):
        cases = (
            ("duplicate", False, True, True, 0),
            ("membership", True, False, True, 1),
            ("close", True, True, False, 1),
        )
        for label, duplicate_succeeds, membership_succeeds, close_succeeds, close_count in cases:
            with self.subTest(failure=label):
                advapi32 = mock.Mock()
                kernel32 = mock.Mock()
                kernel32.CloseHandle.return_value = close_succeeds

                def duplicate_token(_primary, _access, _attributes, _level, _type, output):
                    if duplicate_succeeds:
                        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = 0x3333
                    return duplicate_succeeds

                def check_membership(_token, _sid, _member):
                    return membership_succeeds

                advapi32.DuplicateTokenEx.side_effect = duplicate_token
                advapi32.CheckTokenMembership.side_effect = check_membership
                with self.assertRaises(PermissionError):
                    control_permissions._check_windows_admin_membership(
                        advapi32, kernel32, ctypes.c_void_p(0x1111),
                        ctypes.c_void_p(0x2222), "control.json")
                self.assertEqual(kernel32.CloseHandle.call_count, close_count)

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
    def _validate_identity_with_status(self, status):
        with mock.patch.object(control_permissions.os, "listdir", return_value=["101"]), \
                mock.patch("builtins.open", mock.mock_open(read_data=status)):
            control_permissions._validate_host_control_identity(
                "control.json", host_owned=True, plugin_unprivileged=True)

    @staticmethod
    def _stat_user_namespace(namespace_inode, directory_info=None):
        def stat_path(path):
            if path == "/proc/self/ns/user":
                if namespace_inode is None:
                    raise OSError("cannot inspect user namespace")
                return mock.Mock(st_ino=namespace_inode)
            if path == "control-dir":
                return directory_info
            raise AssertionError("unexpected stat path: %r" % path)
        return stat_path

    def test_unprivileged_mapped_uid_zero_is_rejected_for_directory_and_file(self):
        # In this rootless namespace, namespace UID 0 maps to host UID 1000;
        # stat reports the protected owner's UID as 0 inside the namespace.
        namespace_inode = control_permissions._LINUX_INITIAL_USER_NAMESPACE_INODE + 1
        directory_info = mock.Mock(st_mode=control_permissions.stat.S_IFDIR | 0o700,
                                   st_uid=0)
        file_info = mock.Mock(st_mode=control_permissions.stat.S_IFREG | 0o600,
                              st_uid=0)
        with mock.patch.object(control_permissions.os, "stat",
                               side_effect=self._stat_user_namespace(namespace_inode,
                                                                     directory_info)), \
                mock.patch.object(control_permissions.os, "geteuid", return_value=1000), \
                mock.patch.object(control_permissions, "_validate_linux_capabilities"), \
                mock.patch.object(control_permissions, "_validate_linux_acl_path"):
            with self.assertRaisesRegex(PermissionError, "owned by the host administrator"):
                validate_control_directory("control-dir")

        with mock.patch.object(control_permissions.os, "stat",
                               side_effect=self._stat_user_namespace(namespace_inode)), \
                mock.patch.object(control_permissions.os, "geteuid", return_value=1000), \
                mock.patch.object(control_permissions, "_validate_linux_capabilities"), \
                mock.patch.object(control_permissions, "_validate_linux_acl_path"):
            with self.assertRaisesRegex(PermissionError, "owned by the host administrator"):
                validate_control_file("control.json", info=file_info)

    def test_identity_mapped_nested_namespace_uid_zero_is_rejected(self):
        # An identity row still maps only into the parent namespace; it does
        # not establish that UID 0 is host root.
        namespace_inode = control_permissions._LINUX_INITIAL_USER_NAMESPACE_INODE + 1
        directory_info = mock.Mock(st_mode=control_permissions.stat.S_IFDIR | 0o700,
                                   st_uid=0)
        file_info = mock.Mock(st_mode=control_permissions.stat.S_IFREG | 0o600,
                              st_uid=0)
        with mock.patch.object(control_permissions.os, "stat",
                               side_effect=self._stat_user_namespace(namespace_inode,
                                                                     directory_info)), \
                mock.patch.object(control_permissions.os, "geteuid", return_value=1000), \
                mock.patch.object(control_permissions, "_validate_linux_capabilities"), \
                mock.patch.object(control_permissions, "_validate_linux_acl_path"):
            with self.assertRaisesRegex(PermissionError, "owned by the host administrator"):
                validate_control_directory("control-dir")

        with mock.patch.object(control_permissions.os, "stat",
                               side_effect=self._stat_user_namespace(namespace_inode)), \
                mock.patch.object(control_permissions.os, "geteuid", return_value=1000), \
                mock.patch.object(control_permissions, "_validate_linux_capabilities"), \
                mock.patch.object(control_permissions, "_validate_linux_acl_path"):
            with self.assertRaisesRegex(PermissionError, "owned by the host administrator"):
                validate_control_file("control.json", info=file_info)

    def test_initial_namespace_uid_zero_is_accepted_for_directory_and_file(self):
        namespace_inode = control_permissions._LINUX_INITIAL_USER_NAMESPACE_INODE
        directory_info = mock.Mock(st_mode=control_permissions.stat.S_IFDIR | 0o700,
                                   st_uid=0)
        file_info = mock.Mock(st_mode=control_permissions.stat.S_IFREG | 0o600,
                              st_uid=0)
        with mock.patch.object(control_permissions.os, "stat",
                               side_effect=self._stat_user_namespace(namespace_inode,
                                                                     directory_info)), \
                mock.patch.object(control_permissions.os, "geteuid", return_value=1000), \
                mock.patch.object(control_permissions, "_validate_linux_capabilities"), \
                mock.patch.object(control_permissions, "_validate_linux_acl_path"):
            validate_control_directory("control-dir")

        with mock.patch.object(control_permissions.os, "stat",
                               side_effect=self._stat_user_namespace(namespace_inode)), \
                mock.patch.object(control_permissions.os, "geteuid", return_value=1000), \
                mock.patch.object(control_permissions, "_validate_linux_capabilities"), \
                mock.patch.object(control_permissions, "_validate_linux_acl_path"):
            validate_control_file("control.json", info=file_info)

    def test_unreadable_user_namespace_identity_fails_closed_for_directory_and_file(self):
        directory_info = mock.Mock(st_mode=control_permissions.stat.S_IFDIR | 0o700,
                                   st_uid=0)
        file_info = mock.Mock(st_mode=control_permissions.stat.S_IFREG | 0o600,
                              st_uid=0)
        with mock.patch.object(control_permissions.os, "stat",
                               side_effect=self._stat_user_namespace(None, directory_info)), \
                mock.patch.object(control_permissions.os, "geteuid", return_value=1000), \
                mock.patch.object(control_permissions, "_validate_linux_capabilities"), \
                mock.patch.object(control_permissions, "_validate_linux_acl_path"):
            with self.assertRaisesRegex(PermissionError, "owned by the host administrator"):
                validate_control_directory("control-dir")

        with mock.patch.object(control_permissions.os, "stat",
                               side_effect=self._stat_user_namespace(None)), \
                mock.patch.object(control_permissions.os, "geteuid", return_value=1000), \
                mock.patch.object(control_permissions, "_validate_linux_capabilities"), \
                mock.patch.object(control_permissions, "_validate_linux_acl_path"):
            with self.assertRaisesRegex(PermissionError, "owned by the host administrator"):
                validate_control_file("control.json", info=file_info)

    @staticmethod
    def _safe_linux_thread_status(no_new_privs=b"1"):
        status = b"Uid:\t1000\t1000\t1000\t1000\n"
        if no_new_privs is not None:
            status += b"NoNewPrivs:\t" + no_new_privs + b"\n"
        return (status + b"CapPrm:\t0000000000000000\nCapEff:\t0000000000000000\n"
                b"CapAmb:\t0000000000000000\nCapInh:\t0000000000000000\n"
                b"CapBnd:\t0000000000000000\n")

    def test_no_new_privs_one_allows_control_channel(self):
        self._validate_identity_with_status(self._safe_linux_thread_status())

    def test_no_new_privs_zero_on_nonleader_thread_is_rejected(self):
        statuses = {
            "101": self._safe_linux_thread_status(b"1"),
            "102": self._safe_linux_thread_status(b"0"),
        }

        def open_thread_status(path, *args, **kwargs):
            task_id = path.rsplit("/", 2)[-2]
            return mock.mock_open(read_data=statuses[task_id])()

        with mock.patch.object(control_permissions.os, "listdir",
                               return_value=["101", "102"]), \
                mock.patch("builtins.open", side_effect=open_thread_status):
            with self.assertRaisesRegex(PermissionError, "NoNewPrivs"):
                control_permissions._validate_host_control_identity(
                    "control.json", host_owned=True, plugin_unprivileged=True)

    def test_missing_or_malformed_no_new_privs_fails_closed(self):
        for no_new_privs in (None, b"not-a-number", b"1 extra",
                             b"1\nNoNewPrivs:\t1"):
            with self.subTest(no_new_privs=no_new_privs):
                with self.assertRaisesRegex(PermissionError, "NoNewPrivs"):
                    self._validate_identity_with_status(
                        self._safe_linux_thread_status(no_new_privs))

    def test_control_path_without_filename_is_rejected_before_open(self):
        path = os.path.abspath(os.sep)
        real_open = control_permissions.os.open
        with mock.patch.object(control_permissions, "validate_control_directory"), \
                mock.patch.object(control_permissions.os, "open", wraps=real_open) as open_path:
            with self.assertRaisesRegex(PermissionError, "must name a file"):
                read_control_file(path)
        open_path.assert_not_called()

    def test_real_uid_zero_is_rejected_with_nonroot_effective_uid(self):
        status = (b"Uid:\t0\t1000\t1000\t1000\n"
                  b"CapPrm:\t0000000000000000\nCapEff:\t0000000000000000\n"
                  b"CapAmb:\t0000000000000000\nCapInh:\t0000000000000000\nCapBnd:\t0000000000000000\n")
        with self.assertRaises(PermissionError):
            self._validate_identity_with_status(status)

    def test_saved_uid_zero_is_rejected_with_nonroot_effective_uid(self):
        status = (b"Uid:\t1000\t1000\t0\t1000\n"
                  b"CapPrm:\t0000000000000000\nCapEff:\t0000000000000000\n"
                  b"CapAmb:\t0000000000000000\nCapInh:\t0000000000000000\nCapBnd:\t0000000000000000\n")
        with self.assertRaises(PermissionError):
            self._validate_identity_with_status(status)

    def test_filesystem_uid_zero_is_rejected(self):
        status = (b"Uid:\t1000\t1000\t1000\t0\n"
                  b"CapPrm:\t0000000000000000\nCapEff:\t0000000000000000\n"
                  b"CapAmb:\t0000000000000000\nCapInh:\t0000000000000000\nCapBnd:\t0000000000000000\n")
        with self.assertRaises(PermissionError):
            self._validate_identity_with_status(status)

    def test_missing_or_malformed_uid_data_fails_closed(self):
        capability_data = (b"CapPrm:\t0000000000000000\nCapEff:\t0000000000000000\n"
                           b"CapAmb:\t0000000000000000\nCapInh:\t0000000000000000\nCapBnd:\t0000000000000000\n")
        for uid_data in (b"", b"Uid:\t1000\tinvalid\t1000\t1000\n",
                         b"Uid:\t99999999999\t1000\t1000\t1000\n",
                         b"Uid:\t1000\t1000\t1000\n"):
            with self.subTest(uid_data=uid_data):
                with self.assertRaises(PermissionError):
                    self._validate_identity_with_status(uid_data + capability_data)

    def test_effective_dac_bypass_capability_is_rejected(self):
        status = (b"Name:\tpython\nUid:\t1000\t1000\t1000\t1000\n"
                  b"CapPrm:\t0000000000000000\n"
                  b"CapEff:\t0000000000000002\nCapAmb:\t0000000000000000\n"
                  b"CapInh:\t0000000000000000\nCapBnd:\t0000000000000000\n")
        with mock.patch("builtins.open", mock.mock_open(read_data=status)):
            with self.assertRaises(PermissionError):
                control_permissions._validate_host_control_identity(
                    "control.json", host_owned=True, plugin_unprivileged=True)

    def test_permitted_dac_bypass_capability_is_rejected(self):
        status = (b"Name:\tpython\nUid:\t1000\t1000\t1000\t1000\n"
                  b"CapPrm:\t0000000000000002\n"
                  b"CapEff:\t0000000000000000\nCapAmb:\t0000000000000000\n"
                  b"CapInh:\t0000000000000000\nCapBnd:\t0000000000000000\n")
        with mock.patch("builtins.open", mock.mock_open(read_data=status)):
            with self.assertRaises(PermissionError):
                control_permissions._validate_host_control_identity(
                    "control.json", host_owned=True, plugin_unprivileged=True)

    def test_ambient_dac_bypass_capability_is_rejected(self):
        status = (b"Name:\tpython\nUid:\t1000\t1000\t1000\t1000\n"
                  b"CapPrm:\t0000000000000000\n"
                  b"CapEff:\t0000000000000000\nCapAmb:\t0000000000000002\n"
                  b"CapInh:\t0000000000000000\nCapBnd:\t0000000000000000\n")
        with mock.patch("builtins.open", mock.mock_open(read_data=status)):
            with self.assertRaises(PermissionError):
                control_permissions._validate_host_control_identity(
                    "control.json", host_owned=True, plugin_unprivileged=True)

    def test_inheritable_dac_bypass_capability_is_rejected(self):
        status = (b"Name:\tpython\nUid:\t1000\t1000\t1000\t1000\n"
                  b"CapPrm:\t0000000000000000\n"
                  b"CapEff:\t0000000000000000\nCapAmb:\t0000000000000000\n"
                  b"CapInh:\t0000000000000002\n")
        with self.assertRaises(PermissionError):
            self._validate_identity_with_status(status)

    def test_missing_or_malformed_inheritable_capabilities_fail_closed(self):
        capability_data = (b"CapPrm:\t0000000000000000\n"
                           b"CapEff:\t0000000000000000\n"
                           b"CapAmb:\t0000000000000000\nCapBnd:\t0000000000000000\n")
        for inheritable_data in (b"", b"CapInh:\tnot-hex\n"):
            with self.subTest(inheritable_data=inheritable_data):
                status = (b"Uid:\t1000\t1000\t1000\t1000\n" + capability_data
                          + inheritable_data)
                with self.assertRaises(PermissionError):
                    self._validate_identity_with_status(status)

    def test_capability_on_any_process_thread_is_rejected(self):
        unprivileged = (b"Name:\tpython\nUid:\t1000\t1000\t1000\t1000\n"
                        b"CapPrm:\t0000000000000000\n"
                        b"CapEff:\t0000000000000000\nCapAmb:\t0000000000000000\n"
                        b"CapInh:\t0000000000000000\nCapBnd:\t0000000000000000\n")
        privileged = (b"Name:\tpython\nUid:\t1000\t1000\t1000\t1000\n"
                      b"CapPrm:\t0000000000000002\n"
                      b"CapEff:\t0000000000000002\nCapAmb:\t0000000000000000\n"
                      b"CapInh:\t0000000000000000\nCapBnd:\t0000000000000000\n")

        def open_thread_status(path, *args, **kwargs):
            status = privileged if path.endswith("/102/status") else unprivileged
            return mock.mock_open(read_data=status)()

        with mock.patch.object(control_permissions.os, "listdir", return_value=["101", "102"]), \
                mock.patch("builtins.open", side_effect=open_thread_status):
            with self.assertRaises(PermissionError):
                control_permissions._validate_host_control_identity(
                    "control.json", host_owned=True, plugin_unprivileged=True)

    def test_empty_capability_sets_preserve_unprivileged_policy(self):
        status = (b"Name:\tpython\nUid:\t1000\t1000\t1000\t1000\n"
                  b"NoNewPrivs:\t1\n"
                  b"CapPrm:\t0000000000000000\n"
                  b"CapEff:\t0000000000000000\nCapAmb:\t0000000000000000\n"
                  b"CapInh:\t0000000000000000\nCapBnd:\t0000000000000000\n")
        with mock.patch("builtins.open", mock.mock_open(read_data=status)):
            control_permissions._validate_host_control_identity(
                "control.json", host_owned=True, plugin_unprivileged=True)

    def test_file_capability_dac_bypass_is_rejected(self):
        status = (b"Name:\tpython\nUid:\t1000\t1000\t1000\t1000\n"
                  b"CapPrm:\t0000000000000000\nCapEff:\t0000000000000000\n"
                  b"CapAmb:\t0000000000000000\nCapInh:\t0000000000000000\n"
                  b"CapBnd:\t0000000000000002\n")
        with self.assertRaisesRegex(PermissionError, "CAP_DAC_OVERRIDE"):
            self._validate_identity_with_status(status)

    def test_file_capability_control_path_bypass_bits_are_rejected(self):
        status = (b"Name:\tpython\nUid:\t1000\t1000\t1000\t1000\n"
                  b"CapPrm:\t0000000000000000\nCapEff:\t0000000000000000\n"
                  b"CapAmb:\t0000000000000000\nCapInh:\t0000000000000000\n")
        for bit, name in ((1 << 0, "CAP_CHOWN"),
                          (1 << 1, "CAP_DAC_OVERRIDE"),
                          (1 << 3, "CAP_FOWNER"),
                          (1 << 7, "CAP_SETUID"),
                          (1 << 19, "CAP_SYS_PTRACE"),
                          (1 << 21, "CAP_SYS_ADMIN")):
            with self.subTest(capability=name):
                with self.assertRaisesRegex(PermissionError, name):
                    self._validate_identity_with_status(
                        status + ("CapBnd:\t%016x\n" % bit).encode("ascii"))

    def test_unrelated_bounding_capabilities_preserve_unprivileged_policy(self):
        status = (b"Name:\tpython\nUid:\t1000\t1000\t1000\t1000\n"
                  b"NoNewPrivs:\t1\n"
                  b"CapPrm:\t0000000000000000\nCapEff:\t0000000000000000\n"
                  b"CapAmb:\t0000000000000000\nCapInh:\t0000000000000000\n"
                  b"CapBnd:\t0000000000000020\n")
        self._validate_identity_with_status(status)

    def test_missing_or_malformed_bounding_capabilities_fail_closed(self):
        capability_data = (b"Name:\tpython\nUid:\t1000\t1000\t1000\t1000\n"
                           b"CapPrm:\t0000000000000000\nCapEff:\t0000000000000000\n"
                           b"CapAmb:\t0000000000000000\nCapInh:\t0000000000000000\n")
        for bounding_data in (b"", b"CapBnd:\tnot-hex\n", b"CapBnd:\t0000\n",
                              b"CapBnd:\t0000\nCapBnd:\t0000\n"):
            with self.subTest(bounding_data=bounding_data):
                with self.assertRaises(PermissionError):
                    self._validate_identity_with_status(capability_data + bounding_data)

    def test_bounding_dac_bypass_on_any_process_thread_is_rejected(self):
        unprivileged = (b"Name:\tpython\nUid:\t1000\t1000\t1000\t1000\n"
                        b"CapPrm:\t0000000000000000\n"
                        b"CapEff:\t0000000000000000\nCapAmb:\t0000000000000000\n"
                        b"CapInh:\t0000000000000000\nCapBnd:\t0000000000000000\n")
        vulnerable = unprivileged.replace(
            b"CapBnd:\t0000000000000000", b"CapBnd:\t0000000000000002")

        def open_thread_status(path, *args, **kwargs):
            status = vulnerable if path.endswith("/102/status") else unprivileged
            return mock.mock_open(read_data=status)()

        with mock.patch.object(control_permissions.os, "listdir", return_value=["101", "102"]), \
                mock.patch("builtins.open", side_effect=open_thread_status):
            with self.assertRaisesRegex(PermissionError, "CAP_DAC_OVERRIDE"):
                control_permissions._validate_host_control_identity(
                    "control.json", host_owned=True, plugin_unprivileged=True)

    def test_bounding_owner_bypass_on_any_process_thread_is_rejected(self):
        unprivileged = (b"Name:\tpython\nUid:\t1000\t1000\t1000\t1000\n"
                        b"CapPrm:\t0000000000000000\n"
                        b"CapEff:\t0000000000000000\nCapAmb:\t0000000000000000\n"
                        b"CapInh:\t0000000000000000\nCapBnd:\t0000000000000000\n")
        vulnerable = unprivileged.replace(
            b"CapBnd:\t0000000000000000", b"CapBnd:\t0000000000000008")

        def open_thread_status(path, *args, **kwargs):
            status = vulnerable if path.endswith("/102/status") else unprivileged
            return mock.mock_open(read_data=status)()

        with mock.patch.object(control_permissions.os, "listdir", return_value=["101", "102"]), \
                mock.patch("builtins.open", side_effect=open_thread_status):
            with self.assertRaisesRegex(PermissionError, "CAP_FOWNER"):
                control_permissions._validate_host_control_identity(
                    "control.json", host_owned=True, plugin_unprivileged=True)

    def test_bounding_sys_ptrace_on_nonleader_thread_is_rejected(self):
        unprivileged = (b"Name:\tpython\nUid:\t1000\t1000\t1000\t1000\n"
                        b"CapPrm:\t0000000000000000\n"
                        b"CapEff:\t0000000000000000\nCapAmb:\t0000000000000000\n"
                        b"CapInh:\t0000000000000000\nCapBnd:\t0000000000000000\n")
        vulnerable = unprivileged.replace(
            b"CapBnd:\t0000000000000000", b"CapBnd:\t0000000000080000")

        def open_thread_status(path, *args, **kwargs):
            status = vulnerable if path.endswith("/102/status") else unprivileged
            return mock.mock_open(read_data=status)()

        with mock.patch.object(control_permissions.os, "listdir", return_value=["101", "102"]), \
                mock.patch("builtins.open", side_effect=open_thread_status):
            with self.assertRaisesRegex(PermissionError, "CAP_SYS_PTRACE"):
                control_permissions._validate_host_control_identity(
                    "control.json", host_owned=True, plugin_unprivileged=True)

    def test_bounding_sys_admin_on_nonleader_thread_is_rejected(self):
        unprivileged = (b"Name:\tpython\nUid:\t1000\t1000\t1000\t1000\n"
                        b"CapPrm:\t0000000000000000\n"
                        b"CapEff:\t0000000000000000\nCapAmb:\t0000000000000000\n"
                        b"CapInh:\t0000000000000000\nCapBnd:\t0000000000000000\n")
        vulnerable = unprivileged.replace(
            b"CapBnd:\t0000000000000000", b"CapBnd:\t0000000000200000")

        def open_thread_status(path, *args, **kwargs):
            status = vulnerable if path.endswith("/102/status") else unprivileged
            return mock.mock_open(read_data=status)()

        with mock.patch.object(control_permissions.os, "listdir", return_value=["101", "102"]), \
                mock.patch("builtins.open", side_effect=open_thread_status):
            with self.assertRaisesRegex(PermissionError, "CAP_SYS_ADMIN"):
                control_permissions._validate_host_control_identity(
                    "control.json", host_owned=True, plugin_unprivileged=True)

    def test_no_new_privs_does_not_override_dac_bounding_policy(self):
        status = (b"Name:\tpython\nUid:\t1000\t1000\t1000\t1000\n"
                  b"NoNewPrivs:\t1\n"
                  b"CapPrm:\t0000000000000000\nCapEff:\t0000000000000000\n"
                  b"CapAmb:\t0000000000000000\nCapInh:\t0000000000000000\n"
                  b"CapBnd:\t0000000000000002\n")
        with self.assertRaisesRegex(PermissionError, "CAP_DAC_OVERRIDE"):
            self._validate_identity_with_status(status)

    def test_non_ascii_thread_name_preserves_capability_check(self):
        status = (b"Name:\tpyth\xffon\nUid:\t1000\t1000\t1000\t1000\n"
                  b"NoNewPrivs:\t1\n"
                  b"CapPrm:\t0000000000000000\n"
                  b"CapEff:\t0000000000000000\nCapAmb:\t0000000000000000\n"
                  b"CapInh:\t0000000000000000\nCapBnd:\t0000000000000000\n")
        with tempfile.TemporaryDirectory() as directory:
            status_path = os.path.join(directory, "status")
            with open(status_path, "wb") as status_file:
                status_file.write(status)
            real_open = open

            def open_status_file(_path, *args, **kwargs):
                return real_open(status_path, *args, **kwargs)

            with mock.patch.object(control_permissions.os, "listdir", return_value=["101"]), \
                    mock.patch("builtins.open", side_effect=open_status_file):
                control_permissions._validate_host_control_identity(
                    "control.json", host_owned=True, plugin_unprivileged=True)

    def test_malformed_capability_data_fails_closed(self):
        for status in (
                b"Name:\tpython\nCapAmb:\t0000000000000000\n",
                b"Name:\tpython\nCapEff:\t0000000000000000\n",
                b"CapPrm:\t0000000000000000\nCapEff:\tnot-hex\nCapAmb:\t0000000000000000\n",
                b"CapPrm:\t0000000000000000\nCapEff:\t0000000000000000\nCapAmb:\tnot-hex\n",
                b"CapPrm:\t0000000000000000\nCapEff:\t0000000000000000\r0000000000000002\nCapAmb:\t0000000000000000\n",
                b"CapPrm:\t0000000000000000\nCapEff:\v0000000000000000\nCapAmb:\t0000000000000000\n",
                b"CapPrm:\t0000000000000000\nCapEff:0000000000000000\nCapAmb:\t0000000000000000\n",
                b"CapPrm:\t0000000000000000\nCapEff:\t0000000000000000\nCapEff:\t0000000000000000\nCapAmb:\t0000000000000000\n",
                b"CapPrm:\t0000000000000000\nCapEff:\t0000000000000000\nCapAmb:\t0000000000000000\nCapAmb:\t0000000000000000\n",
                b"CapEff:\t0000000000000000\nCapAmb:\t0000000000000000\n",
                b"CapPrm:\tnot-hex\nCapEff:\t0000000000000000\nCapAmb:\t0000000000000000\n",
                b"CapPrm:\t0000000000000000\nCapPrm:\t0000000000000000\nCapEff:\t0000000000000000\nCapAmb:\t0000000000000000\nCapBnd:\t0000000000000000\n"):
            with self.subTest(status=status):
                status = (b"Uid:\t1000\t1000\t1000\t1000\n"
                          b"CapInh:\t0000000000000000\nCapBnd:\t0000000000000000\n" + status)
                with mock.patch("builtins.open", mock.mock_open(read_data=status)):
                    with self.assertRaises(PermissionError):
                        control_permissions._validate_host_control_identity(
                            "control.json", host_owned=True, plugin_unprivileged=True)

    def test_unreadable_capability_data_fails_closed(self):
        with mock.patch("builtins.open", side_effect=OSError("proc unavailable")):
            with self.assertRaises(PermissionError):
                control_permissions._validate_host_control_identity(
                    "control.json", host_owned=True, plugin_unprivileged=True)

    def test_thread_set_change_during_inspection_retries(self):
        status = (b"Name:\tpython\nUid:\t1000\t1000\t1000\t1000\n"
                  b"NoNewPrivs:\t1\n"
                  b"CapPrm:\t0000000000000000\n"
                  b"CapEff:\t0000000000000000\nCapAmb:\t0000000000000000\n"
                  b"CapInh:\t0000000000000000\nCapBnd:\t0000000000000000\n")
        with mock.patch.object(control_permissions.os, "listdir",
                               side_effect=[["101"], ["101", "102"], ["101"], ["101"]]) as listdir, \
                mock.patch("builtins.open", mock.mock_open(read_data=status)):
            control_permissions._validate_host_control_identity(
                "control.json", host_owned=True, plugin_unprivileged=True)
        self.assertEqual(listdir.call_count, 4)

    def test_thread_list_permission_race_retries(self):
        status = (b"Name:\tpython\nUid:\t1000\t1000\t1000\t1000\n"
                  b"NoNewPrivs:\t1\n"
                  b"CapPrm:\t0000000000000000\n"
                  b"CapEff:\t0000000000000000\nCapAmb:\t0000000000000000\n"
                  b"CapInh:\t0000000000000000\nCapBnd:\t0000000000000000\n")
        with self.subTest(race="task list"):
            with mock.patch.object(
                    control_permissions.os, "listdir",
                    side_effect=[PermissionError(errno.EACCES, "thread list raced"),
                                 ["101"], ["101"]]) as listdir, \
                    mock.patch("builtins.open", mock.mock_open(read_data=status)):
                control_permissions._validate_host_control_identity(
                    "control.json", host_owned=True, plugin_unprivileged=True)
            self.assertEqual(listdir.call_count, 3)

        with self.subTest(race="task status"):
            permission_pending = [True]

            def open_status(_path, *args, **kwargs):
                if permission_pending[0]:
                    permission_pending[0] = False
                    raise PermissionError(errno.EACCES, "task status raced")
                return mock.mock_open(read_data=status)()

            with mock.patch.object(control_permissions.os, "listdir",
                                   side_effect=[["101"]] * 4) as listdir, \
                    mock.patch("builtins.open", side_effect=open_status):
                control_permissions._validate_host_control_identity(
                    "control.json", host_owned=True, plugin_unprivileged=True)
            self.assertEqual(listdir.call_count, 3)

    def test_task_status_disappearance_during_scan_is_retried(self):
        status = (b"Name:\tpython\nUid:\t1000\t1000\t1000\t1000\n"
                  b"NoNewPrivs:\t1\n"
                  b"CapPrm:\t0000000000000000\n"
                  b"CapEff:\t0000000000000000\nCapAmb:\t0000000000000000\n"
                  b"CapInh:\t0000000000000000\nCapBnd:\t0000000000000000\n")
        task_lists = [["101"], ["101"], ["101"]]
        remaining_disappearances = [1]

        def open_status(path, *args, **kwargs):
            if path.endswith("/101/status") and remaining_disappearances[0]:
                remaining_disappearances[0] -= 1
                raise FileNotFoundError(path)
            return mock.mock_open(read_data=status)()

        with mock.patch.object(control_permissions.os, "listdir",
                               side_effect=task_lists), \
                mock.patch("builtins.open", side_effect=open_status):
            control_permissions._validate_host_control_identity(
                "control.json", host_owned=True, plugin_unprivileged=True)

    def test_repeated_thread_scan_churn_fails_closed_after_bounded_retries(self):
        task_lists = (["101"], ["102"]) * control_permissions._LINUX_CAPABILITY_SCAN_ATTEMPTS
        status = (b"Name:\tpython\nUid:\t1000\t1000\t1000\t1000\n"
                  b"CapPrm:\t0000000000000000\n"
                  b"CapEff:\t0000000000000000\nCapAmb:\t0000000000000000\n"
                  b"CapInh:\t0000000000000000\nCapBnd:\t0000000000000000\n")
        with mock.patch.object(control_permissions.os, "listdir",
                               side_effect=task_lists) as listdir, \
                mock.patch("builtins.open", mock.mock_open(read_data=status)):
            with self.assertRaises(PermissionError):
                control_permissions._validate_host_control_identity(
                    "control.json", host_owned=True, plugin_unprivileged=True)
        self.assertEqual(listdir.call_count,
                         control_permissions._LINUX_CAPABILITY_SCAN_ATTEMPTS * 2)

    def test_fifo_control_path_is_rejected_without_blocking_open(self):
        with tempfile.TemporaryDirectory() as directory:
            fifo_path = os.path.join(directory, "control.json")
            os.mkfifo(fifo_path)
            real_open = os.open

            def require_nonblocking(path, flags, *args, **kwargs):
                if path == fifo_path or "dir_fd" in kwargs:
                    self.assertTrue(flags & os.O_NONBLOCK,
                                    "special control paths must use nonblocking open")
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(control_permissions, "_validate_host_control_identity"), \
                    mock.patch("computer_control.control_permissions.os.open",
                               side_effect=require_nonblocking):
                with self.assertRaises(PermissionError):
                    read_control_file(fifo_path)

    def test_read_checks_acl_on_the_open_file_after_parent_path_swap(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = os.path.join(temporary_directory, "control")
            replacement_directory = os.path.join(temporary_directory, "replacement")
            moved_directory = os.path.join(temporary_directory, "moved")
            os.mkdir(directory)
            os.mkdir(replacement_directory)
            path = os.path.join(directory, "control.json")
            replacement_path = os.path.join(replacement_directory, "control.json")
            with open(path, "w", encoding="utf-8") as control_file:
                control_file.write("untrusted")
            with open(replacement_path, "w", encoding="utf-8") as control_file:
                control_file.write("safe")

            directory_inode = os.stat(directory).st_ino
            file_inode = os.stat(path).st_ino
            replacement_inode = os.stat(replacement_path).st_ino
            safe_acl = linux_acl(named_user_permissions=0x6, mask_permissions=0x4)
            write_acl = linux_acl(named_user_permissions=0x6, mask_permissions=0x6)
            swapped = False

            def swap_parent_path():
                nonlocal swapped
                if not swapped:
                    os.replace(directory, moved_directory)
                    os.replace(replacement_directory, directory)
                    swapped = True

            def fake_getxattr(target, attribute, follow_symlinks=True):
                if isinstance(target, int):
                    info = os.fstat(target)
                    if info.st_ino == file_inode:
                        swap_parent_path()
                        self.assertEqual(os.fstat(target).st_ino, file_inode)
                        self.assertEqual(os.stat(os.path.join(directory, "control.json")).st_ino,
                                         replacement_inode)
                        return write_acl
                    self.assertEqual(info.st_ino, directory_inode)
                    return safe_acl

                if target == directory:
                    return safe_acl
                if target == path:
                    swap_parent_path()
                    return safe_acl
                raise AssertionError("unexpected ACL lookup target: %r" % (target,))

            with mock.patch.object(control_permissions, "_validate_host_control_identity"), \
                    mock.patch("computer_control.control_permissions.os.getxattr",
                               side_effect=fake_getxattr):
                with self.assertRaisesRegex(PermissionError, "grants write access"):
                    read_control_file(path)
            self.assertTrue(swapped)

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
