"""Exercise real LD_PRELOAD interception on Linux, with a synthetic SSL library."""
import os
import shutil
import struct
import subprocess
import sys

import pytest

from superlink.bridge.extraction.build import NATIVE


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("cc"), reason="Linux ELF loader and C compiler required")
def test_tls_hook_preserves_return_values_partial_writes_and_avoids_nested_duplicates(tmp_path):
    library = tmp_path / "ssl.c"
    library.write_text('''
#include <stddef.h>
#include <string.h>
void *SSL_new(void *ctx) { return (void *)0x1234; }
void SSL_free(void *ssl) {}
int SSL_read_ex(void *ssl, void *buf, size_t size, size_t *n) {
    if(size < 3) return 0; memcpy(buf,"abc",3); *n=3; return 1;
}
int SSL_write_ex(void *ssl, const void *buf, size_t size, size_t *n) { *n=size-1; return 1; }
int SSL_read(void *ssl, void *buf, int size) { size_t n; return SSL_read_ex(ssl,buf,size,&n) ? (int)n : -1; }
int SSL_write(void *ssl, const void *buf, int size) { size_t n; return SSL_write_ex(ssl,buf,size,&n) ? (int)n : -1; }
''')
    harness = tmp_path / "main.c"
    harness.write_text('''
#include <assert.h>
#include <stddef.h>
#include <string.h>
extern void *SSL_new(void *); extern void SSL_free(void *);
extern int SSL_read(void *,void *,int); extern int SSL_write(void *,const void *,int);
extern int SSL_read_ex(void *,void *,size_t,size_t *); extern int SSL_write_ex(void *,const void *,size_t,size_t *);
int main(void) {
    void *ssl=SSL_new(0); char buf[10]; size_t n=0;
    assert(SSL_read(ssl,buf,10)==3 && !memcmp(buf,"abc",3));
    assert(SSL_write(ssl,"12345",5)==4);
    assert(SSL_read_ex(ssl,buf,10,&n)==1 && n==3);
    assert(SSL_write_ex(ssl,"6789",4,&n)==1 && n==3);
    SSL_free(ssl); return 0;
}
''')
    subprocess.run(["cc", "-shared", "-fPIC", str(library), "-o", str(tmp_path / "libfake.so")], check=True)
    subprocess.run(["cc", str(harness), "-L" + str(tmp_path), "-lfake", "-Wl,-rpath," + str(tmp_path), "-o", str(tmp_path / "harness")], check=True)
    subprocess.run(["cc", "-shared", "-fPIC", "-O2", "-Wall", "-Werror", str(NATIVE / "capture.c"), "-ldl", "-pthread", "-o", str(tmp_path / "hook.so")], check=True)
    path = tmp_path / "capture.bin"
    subprocess.run([str(tmp_path / "harness")], env={**os.environ,
        "LD_PRELOAD": str(tmp_path / "hook.so"), "SUPERLINK_CAPTURE_PATH": str(path)}, check=True)
    raw, offset, records = path.read_bytes(), 0, []
    while offset < len(raw):
        magic, direction, reserved, pointer, size = struct.unpack_from("<4sc3sQI", raw, offset)
        assert magic == b"SLK1" and reserved == bytes(3) and pointer == 0x1234
        records.append((direction, raw[offset + 20:offset + 20 + size]))
        offset += 20 + size
    assert records == [(b"N", b""), (b"R", b"abc"), (b"W", b"1234"), (b"R", b"abc"), (b"W", b"678"), (b"F", b"")]
    assert path.stat().st_mode & 0o777 == 0o600
