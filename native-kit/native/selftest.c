/* selftest.c - checks hegd_native.c with known answers. Built with the address and undefined-behaviour checkers. */
#include "hegd_native.c"
#include <limits.h>
#include <stdlib.h>

static int failed = 0;
#define CHECK(cond, msg) do { if (!(cond)) { printf("FAIL %s\n", msg); failed++; } } while (0)

int main(void) {
    char hex[65];
    CHECK(hn_add(2, 3) == 5, "add");
    CHECK(hn_add(INT_MAX, 1) == INT_MIN, "add wraps without undefined behaviour");
    CHECK(strcmp(hn_version(), "hegd_native 1.0 (C)") == 0, "version");
    CHECK(hn_crc32((const uint8_t *)"123456789", 9, 0) == 0xCBF43926u, "crc32 of 123456789");
    CHECK(hn_crc32((const uint8_t *)"6789", 4, hn_crc32((const uint8_t *)"12345", 5, 0)) == 0xCBF43926u, "crc32 can be continued");
    CHECK(hn_crc32(NULL, 0, 7) == 7u, "crc32 of nothing keeps the seed");
    CHECK(hn_sha256_buf((const uint8_t *)"", 0, hex) == 0 && strcmp(hex, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855") == 0, "sha256 of nothing");
    CHECK(hn_sha256_buf((const uint8_t *)"abc", 3, hex) == 0 && strcmp(hex, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad") == 0, "sha256 of abc");
    CHECK(hn_sha256_buf(NULL, 0, hex) == 0, "sha256 of NULL and length 0");
    CHECK(hn_sha256_buf(NULL, 5, hex) == -3 && hn_sha256_buf((const uint8_t *)"a", 1, NULL) == -3, "NULL arguments are refused");
    CHECK(hn_sha256_file(NULL, hex) == -3 && hn_sha256_file("/nonexistent/file", hex) == -1, "file errors");
    uint8_t *big = malloc(1000003);
    for (size_t i = 0; i < 1000003; i++) big[i] = (uint8_t)(i * 31 + 7);
    CHECK(hn_sha256_buf(big, 1000003, hex) == 0 && strlen(hex) == 64, "a big buffer");
    free(big);
    if (failed) { printf("%d check(s) failed\n", failed); return 1; }
    printf("selftest ok\n");
    return 0;
}
