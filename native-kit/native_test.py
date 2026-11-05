"""native_test.py - run it in Hegd:  py native_test.py     (needs hegd_native.py next to it)
Checks that the native library (C or Rust) is inside the APK, loads, and gives the same answers as Python's own modules."""
import hashlib
import os
import sys
import tempfile
import time
import zlib

try:
    HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:
    HERE = os.getcwd()
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import hegd_native


def main():
    try:
        lib = hegd_native.load()
    except hegd_native.NativeUnavailable as e:
        print("[ERROR] " + str(e))
        print("[INFO] This is normal in an APK that was built without the library.")
        return
    print("[INFO] loaded: %s" % lib.version())
    bad = []

    def check(ok, what):
        if not ok:
            bad.append(what)
            print("[ERROR] wrong answer: " + what)

    check(lib.add(2, 3) == 5 and lib.add(2147483647, 1) == -2147483648, "add")
    check(lib.crc32(b"123456789") == 0xCBF43926 == zlib.crc32(b"123456789"), "crc32 of 123456789")
    check(lib.crc32(b"6789", lib.crc32(b"12345")) == zlib.crc32(b"123456789"), "crc32 continued")
    for n in (0, 1, 3, 55, 56, 57, 63, 64, 65, 119, 120, 128, 1000, 100003):
        data = bytes((i * 31 + 7) & 255 for i in range(n))
        check(lib.sha256(data) == hashlib.sha256(data).hexdigest(), "sha256 of %d bytes" % n)
        check(lib.crc32(data, 5) == zlib.crc32(data, 5), "crc32 of %d bytes" % n)
    tmp = os.path.join(tempfile.gettempdir(), "hegd_native_test.bin")
    big = os.urandom(8 * 1024 * 1024)
    try:
        with open(tmp, "wb") as f:
            f.write(big)
        t0 = time.time(); a = lib.sha256_file(tmp); t_native = time.time() - t0
        t0 = time.time(); b = hashlib.sha256(big).hexdigest(); t_py = time.time() - t0
        check(a == b, "sha256 of an 8 MB file")
        print("[INFO] 8 MB: native %.0f ms, hashlib %.0f ms (hashlib uses OpenSSL and should win: this only shows that the native code runs)" % (t_native * 1000, t_py * 1000))
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    try:
        lib.sha256_file(os.path.join(tempfile.gettempdir(), "no_such_file_hegd"))
        check(False, "a missing file must raise")
    except FileNotFoundError:
        pass
    if bad:
        print("[ERROR] %d wrong answer(s): the library does not behave" % len(bad))
    else:
        print("[SUCCESS] The native library works: %s" % lib.version())


main()
