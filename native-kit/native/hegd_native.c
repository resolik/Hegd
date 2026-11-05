/* hegd_native.c - a tiny native library for Hegd (language: C).
 *
 * It exists to prove that code that is NOT Python can live inside the APK and be called from Python (ctypes).
 * Interface (rust/src/lib.rs has exactly the same one, so the Python side does not care which one it loads):
 *   const char *hn_version(void)
 *   int         hn_add(int a, int b)
 *   uint32_t    hn_crc32(const uint8_t *data, size_t len, uint32_t seed)      same result as zlib.crc32(data, seed)
 *   int         hn_sha256_buf(const uint8_t *data, size_t len, char *out)     out: 65 bytes, hex + NUL; 0 = ok
 *   int         hn_sha256_file(const char *path, char *out)                   0 ok, -1 cannot open, -2 read error
 * Bad arguments (NULL pointers) give -3; nothing here can crash on them.
 */
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

#define HN_API __attribute__((visibility("default")))

static const uint32_t K256[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
    0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
    0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
    0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
    0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
    0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
};
static const uint32_t H256[8] = {
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19
};

static uint32_t crc_table[256];

__attribute__((constructor)) static void hn_init(void) {
    for (uint32_t i = 0; i < 256; i++) {
        uint32_t c = i;
        for (int k = 0; k < 8; k++) c = (c & 1u) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
        crc_table[i] = c;
    }
}

typedef struct { uint32_t h[8]; uint64_t total; uint8_t buf[64]; size_t used; } sha_ctx;

#define ROR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static void sha_block(uint32_t h[8], const uint8_t *p) {
    uint32_t w[64];
    for (int i = 0; i < 16; i++)
        w[i] = ((uint32_t)p[4 * i] << 24) | ((uint32_t)p[4 * i + 1] << 16) | ((uint32_t)p[4 * i + 2] << 8) | (uint32_t)p[4 * i + 3];
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = ROR(w[i - 15], 7) ^ ROR(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = ROR(w[i - 2], 17) ^ ROR(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
    for (int i = 0; i < 64; i++) {
        uint32_t S1 = ROR(e, 6) ^ ROR(e, 11) ^ ROR(e, 25);
        uint32_t ch = (e & f) ^ (~e & g);
        uint32_t t1 = hh + S1 + ch + K256[i] + w[i];
        uint32_t S0 = ROR(a, 2) ^ ROR(a, 13) ^ ROR(a, 22);
        uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
        uint32_t t2 = S0 + maj;
        hh = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
    }
    h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
}

static void sha_init(sha_ctx *c) { memcpy(c->h, H256, sizeof c->h); c->total = 0; c->used = 0; }

static void sha_update(sha_ctx *c, const uint8_t *d, size_t n) {
    c->total += n;
    while (n > 0) {
        size_t take = 64 - c->used;
        if (take > n) take = n;
        memcpy(c->buf + c->used, d, take);
        c->used += take; d += take; n -= take;
        if (c->used == 64) { sha_block(c->h, c->buf); c->used = 0; }
    }
}

static void sha_final(sha_ctx *c, char *out_hex) {
    static const char hex[] = "0123456789abcdef";
    uint64_t bits = c->total * 8u;
    c->buf[c->used++] = 0x80;
    if (c->used > 56) {
        memset(c->buf + c->used, 0, 64 - c->used);
        sha_block(c->h, c->buf);
        c->used = 0;
    }
    memset(c->buf + c->used, 0, 56 - c->used);
    for (int i = 0; i < 8; i++) c->buf[56 + i] = (uint8_t)(bits >> (56 - 8 * i));
    sha_block(c->h, c->buf);
    for (int i = 0; i < 8; i++)
        for (int j = 0; j < 4; j++) {
            uint8_t byte = (uint8_t)(c->h[i] >> (24 - 8 * j));
            out_hex[i * 8 + j * 2] = hex[byte >> 4];
            out_hex[i * 8 + j * 2 + 1] = hex[byte & 15];
        }
    out_hex[64] = 0;
}

HN_API const char *hn_version(void) { return "hegd_native 1.0 (C)"; }

HN_API int hn_add(int a, int b) { return (int)((uint32_t)a + (uint32_t)b); }

HN_API uint32_t hn_crc32(const uint8_t *data, size_t len, uint32_t seed) {
    uint32_t c = ~seed;
    if (data != NULL)
        for (size_t i = 0; i < len; i++) c = crc_table[(c ^ data[i]) & 0xFFu] ^ (c >> 8);
    return ~c;
}

HN_API int hn_sha256_buf(const uint8_t *data, size_t len, char *out_hex) {
    if (out_hex == NULL || (data == NULL && len > 0)) return -3;
    sha_ctx c;
    sha_init(&c);
    if (len > 0) sha_update(&c, data, len);
    sha_final(&c, out_hex);
    return 0;
}

HN_API int hn_sha256_file(const char *path, char *out_hex) {
    if (path == NULL || out_hex == NULL) return -3;
    FILE *f = fopen(path, "rb");
    if (f == NULL) return -1;
    sha_ctx c;
    sha_init(&c);
    uint8_t buf[16384];
    size_t n;
    while ((n = fread(buf, 1, sizeof buf, f)) > 0) sha_update(&c, buf, n);
    int bad = ferror(f);
    fclose(f);
    if (bad) return -2;
    sha_final(&c, out_hex);
    return 0;
}
