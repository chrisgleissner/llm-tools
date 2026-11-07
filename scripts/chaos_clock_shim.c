/* LD_PRELOAD shim for the clock-jump chaos faults (PRD-008).
 *
 * The coordinator has no clock override, so the chaos harness runs it with this library. Wall-clock
 * reads (CLOCK_REALTIME, gettimeofday, time) are shifted by the number of seconds, positive or
 * negative, written in the file named by CHAOS_CLOCK_OFFSET_FILE. The file is read again at most every
 * 20 ms, so a test changes the offset while the process runs, which is what a stepped clock looks
 * like to it. Monotonic clocks are not touched: a wall-clock step does not move them either.
 *
 * Build: cc -shared -fPIC -O2 -o chaos_clock_shim.so chaos_clock_shim.c -ldl
 * Only effective for dynamically linked glibc programs (the release build of the coordinator on the
 * CI runner and on a developer host; not the static musl container image).
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdlib.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

static int (*real_clock_gettime)(clockid_t, struct timespec *);
static long long cached_offset;
static long long checked_ns = -1000000000LL;

static long long read_offset(void) {
    if (!real_clock_gettime) real_clock_gettime = dlsym(RTLD_NEXT, "clock_gettime");
    struct timespec now;
    real_clock_gettime(CLOCK_MONOTONIC, &now);
    long long now_ns = (long long)now.tv_sec * 1000000000LL + now.tv_nsec;
    if (now_ns - checked_ns < 20000000LL) return cached_offset;
    checked_ns = now_ns;
    const char *path = getenv("CHAOS_CLOCK_OFFSET_FILE");
    if (!path) return cached_offset = 0;
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return cached_offset;
    char buffer[64];
    ssize_t count = read(fd, buffer, sizeof buffer - 1);
    close(fd);
    if (count > 0) {
        buffer[count] = 0;
        cached_offset = atoll(buffer);
    }
    return cached_offset;
}

int clock_gettime(clockid_t id, struct timespec *ts) {
    if (!real_clock_gettime) real_clock_gettime = dlsym(RTLD_NEXT, "clock_gettime");
    int result = real_clock_gettime(id, ts);
    if (result == 0 && (id == CLOCK_REALTIME || id == CLOCK_REALTIME_COARSE)) ts->tv_sec += (time_t)read_offset();
    return result;
}

int gettimeofday(struct timeval *tv, void *tz) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    if (tv) {
        tv->tv_sec = ts.tv_sec;
        tv->tv_usec = ts.tv_nsec / 1000;
    }
    (void)tz;
    return 0;
}

time_t time(time_t *out) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    if (out) *out = ts.tv_sec;
    return ts.tv_sec;
}
