/* Narrow TLS plaintext capture for a bounded migration job.
 * No crypto overrides, raw memory hooks, credentials, or fixed output paths.
 * SLK1 records: magic[4], direction[1], reserved[3], SSL pointer LE64,
 * payload length LE32, followed by exactly that many plaintext bytes.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/uio.h>
#include <unistd.h>

#ifdef __arm__
__asm__(".symver dlsym,dlsym@GLIBC_2.4");
#endif

static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
static __thread int nesting;
static int fd = -1, disabled;
static size_t total;
#define MAX_CAPTURE (16u * 1024u * 1024u)
#define MAX_CHUNK 65536u

static void record(char direction, void *ssl, const void *data, size_t n) {
    pthread_mutex_lock(&mutex);
    if (disabled) goto done;
    if (fd < 0) {
        const char *path = getenv("SUPERLINK_CAPTURE_PATH");
        if (!path || path[0] != '/') { disabled = 1; goto done; }
        fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW | O_CLOEXEC, 0600);
        if (fd < 0) { disabled = 1; goto done; }
    }
    unsigned char header[20] = {'S','L','K','1', (unsigned char)direction};
    uint64_t pointer = (uintptr_t)ssl;
    for (int i = 0; i < 8; i++) header[8+i] = pointer >> (8*i);
    if (n > MAX_CHUNK || total + n + sizeof header > MAX_CAPTURE) {
        header[4] = 'E'; n = 0; disabled = 1;
    }
    for (int i = 0; i < 4; i++) header[16+i] = n >> (8*i);
    struct iovec vectors[2] = {{header, sizeof header}, {(void *)data, n}};
    ssize_t wrote = writev(fd, vectors, 2);
    if (wrote != (ssize_t)(sizeof header + n)) disabled = 1;
    total += sizeof header + n;
done:
    pthread_mutex_unlock(&mutex);
}

void *SSL_new(void *ctx) {
    void *(*real)(void *) = dlsym(RTLD_NEXT, "SSL_new");
    if (!real) return NULL;
    void *ssl = real(ctx);
    if (ssl) record('N', ssl, NULL, 0);
    return ssl;
}
void SSL_free(void *ssl) {
    void (*real)(void *) = dlsym(RTLD_NEXT, "SSL_free");
    if (ssl) record('F', ssl, NULL, 0);
    if (real) real(ssl);
}
int SSL_read(void *ssl, void *buf, int size) {
    int (*real)(void *, void *, int) = dlsym(RTLD_NEXT, "SSL_read");
    if (!real) return -1;
    nesting++;
    int rc = real(ssl, buf, size);
    if (--nesting == 0 && rc > 0) record('R', ssl, buf, (size_t)rc);
    return rc;
}
int SSL_write(void *ssl, const void *buf, int size) {
    int (*real)(void *, const void *, int) = dlsym(RTLD_NEXT, "SSL_write");
    if (!real) return -1;
    nesting++;
    int rc = real(ssl, buf, size);
    if (--nesting == 0 && rc > 0) record('W', ssl, buf, (size_t)rc);
    return rc;
}
int SSL_read_ex(void *ssl, void *buf, size_t size, size_t *count) {
    int (*real)(void *, void *, size_t, size_t *) = dlsym(RTLD_NEXT, "SSL_read_ex");
    if (!real) return 0;
    nesting++;
    int rc = real(ssl, buf, size, count);
    if (--nesting == 0 && rc == 1 && count && *count) record('R', ssl, buf, *count);
    return rc;
}
int SSL_write_ex(void *ssl, const void *buf, size_t size, size_t *count) {
    int (*real)(void *, const void *, size_t, size_t *) = dlsym(RTLD_NEXT, "SSL_write_ex");
    if (!real) return 0;
    nesting++;
    int rc = real(ssl, buf, size, count);
    if (--nesting == 0 && rc == 1 && count && *count) record('W', ssl, buf, *count);
    return rc;
}
