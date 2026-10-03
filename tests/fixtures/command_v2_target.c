/* Finite synthetic source only. No ambient files, credentials or networking. */
#define _DARWIN_C_SOURCE 1
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <spawn.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <sys/wait.h>
#include <unistd.h>
extern char **environ;
int main(int argc, char **argv) {
    alarm(3);
    if (argc != 2) return 64;
    if (!strcmp(argv[1], "echo")) {
        char buffer[4096]; ssize_t size;
        while ((size = read(0, buffer, sizeof(buffer))) > 0)
            if (write(1, buffer, (size_t)size) != size) return 70;
        return 0;
    }
    if (!strcmp(argv[1], "fds")) {
        for (int i = 3; i < 1024; ++i) if (fcntl(i, F_GETFD) >= 0) return 71;
        puts("closed"); return 0;
    }
    if (!strcmp(argv[1], "effects")) {
        if (environ[0] != NULL) return 80;
        errno = 0;
        int file = open("/private/etc/hosts", O_RDONLY);
        if (file >= 0 || errno != EPERM) return 81;
        file = open("unexpected-write", O_WRONLY | O_CREAT, 0600);
        if (file >= 0 || errno != EPERM) return 82;
        errno = 0;
        if (kill(getppid(), SIGTERM) != -1 || errno != EPERM) return 83;
        int connection = socket(AF_INET, SOCK_STREAM, 0);
        if (connection >= 0) {
            struct sockaddr_in address;
            memset(&address, 0, sizeof(address));
            address.sin_family = AF_INET;
            address.sin_port = htons(9);
            address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
            errno = 0;
            if (connect(connection, (struct sockaddr *)&address, sizeof(address)) != -1 || errno != EPERM) return 84;
            close(connection);
        } else if (errno != EPERM) return 85;
        puts("denied"); return 0;
    }
    if (!strcmp(argv[1], "large")) {
        for (int i = 0; i < 10000; ++i) puts("synthetic");
        return 0;
    }
    if (!strcmp(argv[1], "hang")) { signal(SIGTERM, SIG_IGN); for (;;) pause(); }
    if (!strcmp(argv[1], "escape-exec")) {
        errno = 0;
        if (setsid() != -1 || errno != EPERM) return 72;
        puts("denied"); return 0;
    }
    if (!strcmp(argv[1], "spawn-group") || !strcmp(argv[1], "spawn-session")) {
        posix_spawnattr_t attr;
        if (posix_spawnattr_init(&attr)) return 73;
        short flags = !strcmp(argv[1], "spawn-group") ? POSIX_SPAWN_SETPGROUP : POSIX_SPAWN_SETSID;
        if (posix_spawnattr_setflags(&attr, flags) || posix_spawnattr_setpgroup(&attr, 0)) return 73;
        char *args[] = {argv[0], "fds", NULL};
        pid_t child;
        int result = posix_spawn(&child, argv[0], NULL, &attr, args, environ);
        posix_spawnattr_destroy(&attr);
        if (result == EPERM) { puts("denied"); return 0; }
        if (!result) (void)waitpid(child, NULL, 0);
        return 74;
    }
    pid_t child = fork();
    if (child < 0) return 75;
    if (!child) {
        alarm(3);
        if (!strcmp(argv[1], "setsid") || !strcmp(argv[1], "setpgid")) {
            errno = 0;
            int result = !strcmp(argv[1], "setsid") ? setsid() : setpgid(0, 0);
            if (result != -1 || errno != EPERM) _exit(76);
            (void)write(1, "denied\n", 7); _exit(0);
        }
        if (!strcmp(argv[1], "fork-exec")) {
            char *args[] = {argv[0], "escape-exec", NULL};
            execve(argv[0], args, environ); _exit(77);
        }
        signal(SIGTERM, SIG_IGN);
        if (!strcmp(argv[1], "fork-hang")) (void)write(1, "forking\n", 8);
        if (!strcmp(argv[1], "tree") || !strcmp(argv[1], "fork-race")) (void)write(1, "ready\n", 6);
        if (!strcmp(argv[1], "tree") || !strcmp(argv[1], "fork-race") || !strcmp(argv[1], "fork-hang")) {
            int count = !strcmp(argv[1], "tree") ? 1 : 32;
            for (int i = 0; i < count; ++i) {
                pid_t extra = fork();
                if (!extra) { alarm(3); for (;;) pause(); }
                if (extra < 0) break;
                usleep(2000);
            }
        }
        for (;;) pause();
    }
    if (!strcmp(argv[1], "tree") || !strcmp(argv[1], "fork-race")) {
        usleep(10000); return 0;
    }
    int status;
    if (waitpid(child, &status, 0) != child) return 78;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 79;
}
