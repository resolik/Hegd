#!/bin/sh
# Builds the C library for THIS computer and runs every check. Run it first: sh check_host.sh
set -e
cd "$(dirname "$0")"
mkdir -p build
gcc -Wall -Wextra -O2 -fsanitize=address,undefined -o build/selftest native/selftest.c
./build/selftest
gcc -shared -fPIC -O2 -Wall -Wextra -o build/libhegdnative.so native/hegd_native.c
HEGD_NATIVE="$PWD/build/libhegdnative.so" python3 native_test.py
echo "host check done"
