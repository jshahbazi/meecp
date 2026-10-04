#include <signal.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static void allocation(size_t size) {
    volatile unsigned char *p = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    if (p == MAP_FAILED) _exit(65);
    for (size_t i = 0; i < size; i += 4096) p[i] = 1;
}
int main(int argc, char **argv) {
    alarm(5); /* Bounded cleanup if the deliberately killed native helper is lost. */
    if (argc != 2) return 64;
    if (!strcmp(argv[1], "echo")) { (void)write(1, "synthetic", 9); usleep(30000); return 0; }
    if (!strcmp(argv[1], "cpu")) {
        volatile uint64_t n = 1;
        for (;;) n = n * 1664525 + 1013904223;
    }
    if (!strcmp(argv[1], "footprint")) {
        /* Keep membership stable so only the footprint threshold is exercised. */
        allocation(24 * 1024 * 1024);
        for (;;) pause();
    }
    if (!strcmp(argv[1], "short")) {
        for (int i = 0; i < 20; ++i) {
            pid_t child = fork();
            if (child < 0) return 66;
            if (!child) _exit(0);
            if (waitpid(child, NULL, 0) != child) return 67;
        }
        usleep(30000); return 20;
    }
    if (!strcmp(argv[1], "overlap") || !strcmp(argv[1], "hang")) {
        allocation(4 * 1024 * 1024);
        for (int i = 0; i < 2; ++i) {
            pid_t child = fork();
            if (child < 0) return 66;
            if (!child) {
                alarm(5); signal(SIGTERM, SIG_IGN);
                allocation(8 * 1024 * 1024);
                for (;;) pause();
            }
        }
        (void)write(1, "running", 7);
        if (!strcmp(argv[1], "overlap")) { usleep(150000); return 0; }
        for (;;) pause();
    }
    return 64;
}
