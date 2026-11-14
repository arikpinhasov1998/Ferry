"""Copying file metadata between local paths, per platform.

Everything is "best effort with reporting": when the current user lacks the
rights for something (e.g. changing the owner without admin/root), the item is
skipped and a warning is reported once per run instead of failing the file.
"""
from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"


@dataclass
class MetaOptions:
    timestamps: bool = True
    permissions: bool = True     # unix mode bits / windows attributes
    acl: bool = True             # windows DACL (owner/SACL too when elevated)
    owner: bool = False          # unix uid/gid, windows owner (needs admin/root)
    xattrs: bool = True          # extended attributes / macOS metadata
    symlinks: str = "copy"       # copy | follow | skip

    @classmethod
    def from_spec(cls, d: dict | None) -> "MetaOptions":
        d = d or {}
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def is_elevated() -> bool:
    if IS_WIN:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


# ---------------------------------------------------------------- Windows ---
if IS_WIN:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    _k32.GetFileAttributesW.restype = wintypes.DWORD
    _k32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    _k32.SetFileAttributesW.restype = wintypes.BOOL
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.SetFileTime.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    _k32.GetFileTime.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]

    FILE_ATTRIBUTE_READONLY = 0x1
    _COPYABLE_ATTRS = 0x1 | 0x2 | 0x4 | 0x20 | 0x2000  # readonly, hidden, system, archive, not-indexed
    _INVALID = wintypes.HANDLE(-1).value

    def win_get_attrs(path: str) -> int:
        a = _k32.GetFileAttributesW(path)
        return 0 if a == 0xFFFFFFFF else a

    def win_set_attrs(path: str, attrs: int):
        if not _k32.SetFileAttributesW(path, attrs or 0x80):
            raise ctypes.WinError(ctypes.get_last_error())

    def win_clear_readonly(path: str):
        a = win_get_attrs(path)
        if a & FILE_ATTRIBUTE_READONLY:
            _k32.SetFileAttributesW(path, a & ~FILE_ATTRIBUTE_READONLY)

    def _open_for_times(path: str, write: bool):
        access = 0x100 if write else 0x80  # FILE_WRITE_ATTRIBUTES / FILE_READ_ATTRIBUTES
        h = _k32.CreateFileW(path, access, 0x7, None, 3, 0x02000000 | 0x00200000, None)  # backup sem. + reparse
        if h == _INVALID:
            raise ctypes.WinError(ctypes.get_last_error())
        return h

    def win_copy_creation_time(src: str, dst: str):
        ft = ctypes.c_ulonglong()
        h = _open_for_times(src, False)
        try:
            if not _k32.GetFileTime(h, ctypes.byref(ft), None, None):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _k32.CloseHandle(h)
        h = _open_for_times(dst, True)
        try:
            if not _k32.SetFileTime(h, ctypes.byref(ft), None, None):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _k32.CloseHandle(h)

    _privs_enabled = False

    def win_enable_privileges():
        """Enable backup/restore/security privileges if the token has them (admin)."""
        global _privs_enabled
        if _privs_enabled:
            return
        _privs_enabled = True
        try:
            import win32api
            import win32security
            token = win32security.OpenProcessToken(
                win32api.GetCurrentProcess(),
                win32security.TOKEN_ADJUST_PRIVILEGES | win32security.TOKEN_QUERY)
            privs = []
            for name in ("SeBackupPrivilege", "SeRestorePrivilege", "SeSecurityPrivilege",
                         "SeTakeOwnershipPrivilege"):
                try:
                    privs.append((win32security.LookupPrivilegeValue(None, name),
                                  win32security.SE_PRIVILEGE_ENABLED))
                except Exception:
                    pass
            win32security.AdjustTokenPrivileges(token, False, privs)
        except Exception:
            pass

    def win_copy_security(src: str, dst: str, owner: bool, warn):
        try:
            import win32security
        except ImportError:
            warn("acl", "NTFS ACLs not copied: pywin32 is not available")
            return
        win_enable_privileges()
        info = win32security.DACL_SECURITY_INFORMATION
        if owner:
            info |= win32security.OWNER_SECURITY_INFORMATION | win32security.GROUP_SECURITY_INFORMATION
            if is_elevated():
                info |= win32security.SACL_SECURITY_INFORMATION
        try:
            sd = win32security.GetNamedSecurityInfo(src, win32security.SE_FILE_OBJECT, info)
        except Exception as e:
            if info != win32security.DACL_SECURITY_INFORMATION:
                warn("acl-owner", f"owner/audit info not readable ({e}); copying permissions only")
                info = win32security.DACL_SECURITY_INFORMATION
                sd = win32security.GetNamedSecurityInfo(src, win32security.SE_FILE_OBJECT, info)
            else:
                raise
        dacl = sd.GetSecurityDescriptorDacl()
        ctrl, _rev = sd.GetSecurityDescriptorControl()
        set_info = win32security.DACL_SECURITY_INFORMATION
        # keep "inherit from parent" semantics
        if ctrl & win32security.SE_DACL_PROTECTED:
            set_info |= win32security.PROTECTED_DACL_SECURITY_INFORMATION
        else:
            set_info |= win32security.UNPROTECTED_DACL_SECURITY_INFORMATION
        own = grp = sacl = None
        if info & win32security.OWNER_SECURITY_INFORMATION:
            own, grp = sd.GetSecurityDescriptorOwner(), sd.GetSecurityDescriptorGroup()
            set_info |= win32security.OWNER_SECURITY_INFORMATION | win32security.GROUP_SECURITY_INFORMATION
        if info & win32security.SACL_SECURITY_INFORMATION:
            sacl = sd.GetSecurityDescriptorSacl()
            set_info |= win32security.SACL_SECURITY_INFORMATION
        try:
            win32security.SetNamedSecurityInfo(dst, win32security.SE_FILE_OBJECT, set_info, own, grp, dacl, sacl)
        except Exception as e:
            if own is not None or sacl is not None:
                warn("acl-owner", f"setting owner/audit info needs admin rights ({e}); copied permissions only")
                set_info &= ~(win32security.OWNER_SECURITY_INFORMATION | win32security.GROUP_SECURITY_INFORMATION
                              | win32security.SACL_SECURITY_INFORMATION)
                win32security.SetNamedSecurityInfo(dst, win32security.SE_FILE_OBJECT, set_info, None, None, dacl, None)
            else:
                raise


# ------------------------------------------------------------------ xattrs ---
def _xattr_funcs():
    if hasattr(os, "listxattr"):
        return (lambda p: os.listxattr(p, follow_symlinks=False),
                lambda p, n: os.getxattr(p, n, follow_symlinks=False),
                lambda p, n, v: os.setxattr(p, n, v, follow_symlinks=False))
    if IS_MAC:
        try:
            import xattr  # optional dependency on macOS
            return (lambda p: xattr.listxattr(p, symlink=True),
                    lambda p, n: xattr.getxattr(p, n, symlink=True),
                    lambda p, n, v: xattr.setxattr(p, n, v, symlink=True))
        except ImportError:
            return None
    return None


_XATTR = _xattr_funcs()


def copy_xattrs(src: str, dst: str, warn):
    if _XATTR is None:
        if not IS_WIN:
            warn("xattr", "extended attributes not supported on this platform/build")
        return
    lst, get, put = _XATTR
    try:
        names = lst(src)
    except OSError:
        return
    for n in names:
        # security.* and system.* usually need root; trusted.* always needs root
        try:
            put(dst, n, get(src, n))
        except PermissionError:
            warn("xattr-perm", f"some extended attributes (e.g. {n}) need elevated rights; skipped")
        except OSError as e:
            if e.errno not in (95,):   # EOPNOTSUPP: destination fs has no xattrs
                warn("xattr", f"extended attribute {n} not copied: {e}")
            else:
                warn("xattr-fs", "destination filesystem does not support extended attributes")
                return


# ------------------------------------------------------------------- apply ---
def apply_local(src: str, dst: str, st: os.stat_result, opts: MetaOptions, warn, is_link=False, is_dir=False):
    """Copy metadata from src to dst (both local paths). ``st`` is lstat(src)."""
    if opts.xattrs and not IS_WIN:
        copy_xattrs(src, dst, warn)

    if IS_WIN:
        if opts.acl:
            try:
                win_copy_security(src, dst, opts.owner, warn)
            except Exception as e:
                warn("acl", f"NTFS permissions not copied for some items: {e}")
        if opts.timestamps and not is_link:
            try:
                win_copy_creation_time(src, dst)
            except Exception:
                pass
    else:
        if opts.owner and not is_link:
            try:
                os.chown(dst, st.st_uid, st.st_gid)
            except PermissionError:
                warn("owner", "owner/group not copied: changing ownership needs root")
            except OSError as e:
                warn("owner", f"owner/group not copied: {e}")
        if opts.permissions and not is_link:
            try:
                os.chmod(dst, stat.S_IMODE(st.st_mode))
            except OSError as e:
                warn("mode", f"permission bits not copied: {e}")

    if opts.timestamps:
        try:
            if is_link:
                if os.utime in os.supports_follow_symlinks:
                    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
            else:
                os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError as e:
            warn("times", f"timestamps not copied: {e}")

    # Windows attributes last: READONLY would block the timestamp/ACL writes.
    if IS_WIN and opts.permissions and not is_link:
        try:
            win_set_attrs(dst, win_get_attrs(src) & _COPYABLE_ATTRS)
        except Exception as e:
            warn("attrs", f"file attributes not copied: {e}")


def make_writable(path: str):
    """Allow overwriting/deleting a read-only destination file."""
    try:
        if IS_WIN:
            win_clear_readonly(path)
        else:
            st = os.lstat(path)
            if not stat.S_ISLNK(st.st_mode) and not (st.st_mode & stat.S_IWUSR):
                os.chmod(path, st.st_mode | stat.S_IWUSR)
    except OSError:
        pass
