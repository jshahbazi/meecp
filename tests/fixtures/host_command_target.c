#include <signal.h>
#include <errno.h>
#include <fcntl.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

int main(int argc, char **argv) {
    alarm(4); /* Test orphan escape hatch, independently of native ownership. */
    if (argc != 2) return 64;
    if (!strcmp(argv[1], "echo")) {
        char bytes[1024]; ssize_t count;
        while ((count = read(0, bytes, sizeof(bytes))) > 0)
            if (write(1, bytes, (size_t)count) != count) return 65;
        return 0;
    }
    if (!strcmp(argv[1], "identity")) {
        printf("{\"pid\":%d,\"parent\":%d,\"group\":%d}", getpid(), getppid(), getpgrp());
        return 0;
    }
    if (!strcmp(argv[1], "fds")) {
        for (int fd = 3; fd < 4096; ++fd) if (fcntl(fd, F_GETFD) != -1 || errno != EBADF) return 68;
        puts("{\"ok\":true}"); return 0;
    }
    if (!strcmp(argv[1], "escape")) {
        pid_t child = fork();
        if (child < 0) return 66;
        if (!child) {
            if (setsid() != -1 || errno != EPERM || setpgid(0, 0) != -1 || errno != EPERM) _exit(69);
            posix_spawnattr_t attrs;
            posix_spawnattr_init(&attrs);
            posix_spawnattr_setflags(&attrs, POSIX_SPAWN_SETPGROUP);
            posix_spawnattr_setpgroup(&attrs, 0);
            char *args[] = {argv[0], "child", NULL}; char *environment[] = {NULL}; pid_t spawned;
            int result = posix_spawn(&spawned, argv[0], NULL, &attrs, args, environment);
            posix_spawnattr_destroy(&attrs);
            _exit(result == EPERM ? 0 : 70);
        }
        int status = 0;
        if (waitpid(child, &status, 0) != child || status != 0) return 71;
        puts("{\"ok\":true}"); return 0;
    }
    if (!strcmp(argv[1], "child")) {
        signal(SIGTERM, SIG_IGN);
        while (1) pause();
    }
    if (!strcmp(argv[1], "tree") || !strcmp(argv[1], "hang")) {
        pid_t child = fork();
        if (child < 0) return 66;
        if (!child) {
            execl(argv[0], argv[0], "child", (char *)NULL);
            _exit(67);
        }
        if (!strcmp(argv[1], "tree")) { puts("{\"ok\":true}"); return 0; }
        while (1) pause();
    }
    if (!strcmp(argv[1], "exit")) return 17;
    if (!strcmp(argv[1], "bad")) { puts("{\"ok\":true,\"ok\":false}"); return 0; }
    return 64;
}
