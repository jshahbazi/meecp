/* Nonsecret real executable fixture; no JSON/library/runtime dependencies. */
#define _DARWIN_C_SOURCE 1
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc != 2) return 64;
    if (!strcmp(argv[1], "foreground")) {
        char input[256];
        size_t count = fread(input, 1, sizeof(input), stdin);
        printf("{\"input_bytes\":%zu,\"foreground\":true}\n", count);
        return 0;
    }
    if (!strcmp(argv[1], "fork") || !strcmp(argv[1], "descendant")) {
        int depth = !strcmp(argv[1], "descendant") ? 2 : 1;
        pid_t child = fork();
        if (child < 0) {
            printf("{\"fork_denied\":%s,\"requested_depth\":%d}\n", errno == EPERM ? "true" : "false", depth);
            return 0;
        }
        if (!child) {
            if (depth == 2) (void)fork();
            sleep(60);
            return 0;
        }
        return 71;
    }
    if (!strcmp(argv[1], "setsid")) {
        int denied = setsid() < 0 && errno == EPERM;
        printf("{\"setsid_denied\":%s}\n", denied ? "true" : "false");
        return denied ? 0 : 71;
    }
    if (!strcmp(argv[1], "crash")) {
        raise(SIGSEGV);
        return 71;
    }
    if (!strcmp(argv[1], "sleep")) {
        signal(SIGTERM, SIG_IGN);
        puts("{\"started\":true}");
        fflush(stdout);
        sleep(60);
        return 0;
    }
    return 64;
}
