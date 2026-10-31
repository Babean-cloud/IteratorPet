"""管理员身份下的拖文件退路：从 HDROP 里取路径。"""

import ctypes
import struct
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def _hdrop(paths):
    names = ("\0".join(paths) + "\0\0").encode("utf-16-le")
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)        # DROPFILES，fWide=1
    k32 = ctypes.windll.kernel32
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    handle = k32.GlobalAlloc(0x42, len(header) + len(names))
    pointer = k32.GlobalLock(handle)
    ctypes.memmove(pointer, header + names, len(header) + len(names))
    k32.GlobalUnlock(handle)
    return handle


def test_dropped_files_reads_unicode_paths():
    from iterator_pet.platform.legacy_drop import dropped_files

    paths = [r"C:\temp\笔记.txt", r"D:\代码\main.py"]
    assert dropped_files(_hdrop(paths)) == paths


def test_other_messages_are_ignored():
    from ctypes import wintypes

    from iterator_pet.platform.legacy_drop import files_from_message

    msg = wintypes.MSG()
    msg.message = 0x0200                                    # WM_MOUSEMOVE
    assert files_from_message(b"windows_generic_MSG", ctypes.addressof(msg)) is None
    assert files_from_message(b"something_else", ctypes.addressof(msg)) is None
