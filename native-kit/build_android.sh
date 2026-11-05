#!/bin/sh
# Builds libs/arm64-v8a/libhegdnative.so from the C source with the NDK that buildozer already downloaded.
set -e
cd "$(dirname "$0")"
NDK="${NDK:-$HOME/.buildozer/android/platform/android-ndk-r25b}"
CLANG="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android21-clang"
[ -x "$CLANG" ] || { echo "NDK compiler not found: $CLANG (set NDK=... if yours is elsewhere)"; exit 1; }
mkdir -p libs/arm64-v8a
"$CLANG" -shared -fPIC -O2 -Wl,-z,max-page-size=16384 -Wl,-soname,libhegdnative.so -o libs/arm64-v8a/libhegdnative.so native/hegd_native.c
ls -l libs/arm64-v8a/libhegdnative.so
echo "built (C). Next: put buildozer.native.spec in place of buildozer.spec and run buildozer."
