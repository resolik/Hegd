#!/bin/sh
# The same library written in Rust. Needs Rust: curl https://sh.rustup.rs -sSf | sh   (about 1.5 GB: check df -h / first)
set -e
cd "$(dirname "$0")"
command -v cargo >/dev/null || { echo "Rust is not installed (see the first line of this file)"; exit 1; }
NDK="${NDK:-$HOME/.buildozer/android/platform/android-ndk-r25b}"
BIN="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin"
[ -x "$BIN/aarch64-linux-android21-clang" ] || { echo "NDK compiler not found in $BIN"; exit 1; }
rustup target add aarch64-linux-android
export CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER="$BIN/aarch64-linux-android21-clang"
export RUSTFLAGS="-C link-arg=-Wl,-z,max-page-size=16384"
(cd rust && cargo build --release --target aarch64-linux-android)
mkdir -p libs/arm64-v8a
cp rust/target/aarch64-linux-android/release/libhegdnative.so libs/arm64-v8a/libhegdnative.so
ls -l libs/arm64-v8a/libhegdnative.so
echo "built (Rust). Same next step as for C."
