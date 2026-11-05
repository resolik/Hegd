//! hegd_native in Rust: the same C interface as native/hegd_native.c (see the comment there).
use std::ffi::CStr;
use std::fs::File;
use std::io::Read;
use std::os::raw::c_char;
use std::os::unix::ffi::OsStrExt;

const K: [u32; 64] = [
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
];
const H0: [u32; 8] = [
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19
];

struct Sha {
    h: [u32; 8],
    total: u64,
    buf: [u8; 64],
    used: usize,
}

impl Sha {
    fn new() -> Sha {
        Sha { h: H0, total: 0, buf: [0; 64], used: 0 }
    }

    fn block(h: &mut [u32; 8], p: &[u8; 64]) {
        let mut w = [0u32; 64];
        for i in 0..16 {
            w[i] = u32::from_be_bytes([p[4 * i], p[4 * i + 1], p[4 * i + 2], p[4 * i + 3]]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16].wrapping_add(s0).wrapping_add(w[i - 7]).wrapping_add(s1);
        }
        let mut v = *h;
        for i in 0..64 {
            let s1 = v[4].rotate_right(6) ^ v[4].rotate_right(11) ^ v[4].rotate_right(25);
            let ch = (v[4] & v[5]) ^ (!v[4] & v[6]);
            let t1 = v[7].wrapping_add(s1).wrapping_add(ch).wrapping_add(K[i]).wrapping_add(w[i]);
            let s0 = v[0].rotate_right(2) ^ v[0].rotate_right(13) ^ v[0].rotate_right(22);
            let maj = (v[0] & v[1]) ^ (v[0] & v[2]) ^ (v[1] & v[2]);
            let t2 = s0.wrapping_add(maj);
            v[7] = v[6];
            v[6] = v[5];
            v[5] = v[4];
            v[4] = v[3].wrapping_add(t1);
            v[3] = v[2];
            v[2] = v[1];
            v[1] = v[0];
            v[0] = t1.wrapping_add(t2);
        }
        for i in 0..8 {
            h[i] = h[i].wrapping_add(v[i]);
        }
    }

    fn update(&mut self, mut d: &[u8]) {
        self.total = self.total.wrapping_add(d.len() as u64);
        while !d.is_empty() {
            let take = (64 - self.used).min(d.len());
            self.buf[self.used..self.used + take].copy_from_slice(&d[..take]);
            self.used += take;
            d = &d[take..];
            if self.used == 64 {
                let b = self.buf;
                Sha::block(&mut self.h, &b);
                self.used = 0;
            }
        }
    }

    fn finish(mut self) -> [u8; 64] {
        let bits = self.total.wrapping_mul(8);
        self.buf[self.used] = 0x80;
        self.used += 1;
        if self.used > 56 {
            for i in self.used..64 {
                self.buf[i] = 0;
            }
            let b = self.buf;
            Sha::block(&mut self.h, &b);
            self.used = 0;
        }
        for i in self.used..56 {
            self.buf[i] = 0;
        }
        self.buf[56..64].copy_from_slice(&bits.to_be_bytes());
        let b = self.buf;
        Sha::block(&mut self.h, &b);
        let hex = b"0123456789abcdef";
        let mut out = [0u8; 64];
        for i in 0..8 {
            for j in 0..4 {
                let byte = (self.h[i] >> (24 - 8 * j)) as u8;
                out[i * 8 + j * 2] = hex[(byte >> 4) as usize];
                out[i * 8 + j * 2 + 1] = hex[(byte & 15) as usize];
            }
        }
        out
    }
}

unsafe fn write_hex(out: *mut c_char, hex: &[u8; 64]) {
    std::ptr::copy_nonoverlapping(hex.as_ptr(), out as *mut u8, 64);
    *(out as *mut u8).add(64) = 0;
}

#[no_mangle]
pub extern "C" fn hn_version() -> *const c_char {
    b"hegd_native 1.0 (Rust)\0".as_ptr() as *const c_char
}

#[no_mangle]
pub extern "C" fn hn_add(a: i32, b: i32) -> i32 {
    a.wrapping_add(b)
}

#[no_mangle]
pub unsafe extern "C" fn hn_crc32(data: *const u8, len: usize, seed: u32) -> u32 {
    let mut crc = !seed;
    if !data.is_null() {
        for &byte in std::slice::from_raw_parts(data, len) {
            crc ^= byte as u32;
            for _ in 0..8 {
                crc = if crc & 1 != 0 { (crc >> 1) ^ 0xEDB8_8320 } else { crc >> 1 };
            }
        }
    }
    !crc
}

#[no_mangle]
pub unsafe extern "C" fn hn_sha256_buf(data: *const u8, len: usize, out: *mut c_char) -> i32 {
    if out.is_null() || (data.is_null() && len > 0) {
        return -3;
    }
    let mut s = Sha::new();
    if len > 0 {
        s.update(std::slice::from_raw_parts(data, len));
    }
    write_hex(out, &s.finish());
    0
}

#[no_mangle]
pub unsafe extern "C" fn hn_sha256_file(path: *const c_char, out: *mut c_char) -> i32 {
    if path.is_null() || out.is_null() {
        return -3;
    }
    let name = std::ffi::OsStr::from_bytes(CStr::from_ptr(path).to_bytes());
    let mut f = match File::open(name) {
        Ok(f) => f,
        Err(_) => return -1,
    };
    let mut s = Sha::new();
    let mut buf = vec![0u8; 16384];
    loop {
        match f.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => s.update(&buf[..n]),
            Err(_) => return -2,
        }
    }
    write_hex(out, &s.finish());
    0
}
