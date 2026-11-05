"""hegd_native.py - the Python side of the native test library (libhegdnative.so, written in C or Rust).

    import hegd_native
    lib = hegd_native.load()          # raises NativeUnavailable (with a clear text) if the library is not there
    lib.version(); lib.add(2, 3); lib.crc32(b"abc"); lib.sha256(b"abc"); lib.sha256_file("/path")
"""
import ctypes
import os


class NativeUnavailable(Exception):
    pass


def _candidates(path):
    if path:
        yield path
    if os.environ.get("HEGD_NATIVE"):
        yield os.environ["HEGD_NATIVE"]
    yield "libhegdnative.so"                       # inside the APK: found through Android's own library search
    try:                                           # ... or in the folder where Android keeps the app's native libraries
        from jnius import autoclass
        info = autoclass("org.kivy.android.PythonActivity").mActivity.getApplicationInfo()
        yield os.path.join(str(info.nativeLibraryDir), "libhegdnative.so")
    except Exception:
        pass
    try:
        yield os.path.join(os.path.dirname(os.path.abspath(__file__)), "libhegdnative.so")
    except NameError:
        pass


class Native:
    def __init__(self, lib):
        self.lib = lib
        lib.hn_version.restype, lib.hn_version.argtypes = ctypes.c_char_p, []
        lib.hn_add.restype, lib.hn_add.argtypes = ctypes.c_int, [ctypes.c_int, ctypes.c_int]
        lib.hn_crc32.restype, lib.hn_crc32.argtypes = ctypes.c_uint32, [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32]
        lib.hn_sha256_buf.restype, lib.hn_sha256_buf.argtypes = ctypes.c_int, [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p]
        lib.hn_sha256_file.restype, lib.hn_sha256_file.argtypes = ctypes.c_int, [ctypes.c_char_p, ctypes.c_char_p]

    def version(self):
        return self.lib.hn_version().decode("utf-8", "replace")

    def add(self, a, b):
        return self.lib.hn_add(a, b)

    def crc32(self, data, seed=0):
        return self.lib.hn_crc32(bytes(data), len(data), seed & 0xFFFFFFFF)

    def sha256(self, data):
        data = bytes(data)
        out = ctypes.create_string_buffer(65)
        if self.lib.hn_sha256_buf(data, len(data), out) != 0:
            raise ValueError("hn_sha256_buf refused its arguments")
        return out.value.decode("ascii")

    def sha256_file(self, path):
        out = ctypes.create_string_buffer(65)
        rc = self.lib.hn_sha256_file(os.fsencode(path), out)
        if rc == -1:
            raise FileNotFoundError(2, "cannot open the file", str(path))
        if rc != 0:
            raise OSError("cannot read the file (%d)" % rc)
        return out.value.decode("ascii")


def load(path=None):
    tried = []
    for cand in _candidates(path):
        try:
            return Native(ctypes.CDLL(cand))
        except OSError as e:
            tried.append("%s: %s" % (cand, e))
        except AttributeError as e:
            tried.append("%s: not a hegd_native library (%s)" % (cand, e))
    raise NativeUnavailable("the library was not found. Tried:\n  " + "\n  ".join(tried))
