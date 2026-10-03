/* Nonsecret containment probe. Every direct child is waited, no PID discovery. */
#define _DARWIN_C_SOURCE 1
#include <errno.h>
#include <limits.h>
#include <sandbox.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

static void emit(const char *operation, int result, int failure) {
    printf("{\"operation\":\"%s\",\"succeeded\":%s,\"errno\":%d,\"group_changed\":%s}\n",
           operation, result == 0 ? "true" : "false", failure,
           getpgrp() == getpid() ? "true" : "false");
    fflush(stdout);
}

int main(int argc, char **argv) {
    if (argc != 3) return 64;
    if (!strcmp(argv[2], "spawn-result")) {
        emit("spawn-result", 0, 0);
        return 0;
    }
    if (!strcmp(argv[2], "exec-setsid-result")) {
        errno = 0;
        int result = setsid() < 0 ? -1 : 0;
        int failure = errno;
        emit("exec-setsid-result", result, failure);
        return 0;
    }
    int filtered = strcmp(argv[1], "plain") != 0;
    if (filtered) {
        char profile[PATH_MAX * 3 + 1024];
        if (!strcmp(argv[1], "restricted")) {
            char executable[PATH_MAX], directory[PATH_MAX];
            if (!realpath(argv[0], executable) || strchr(executable, '"') || strchr(executable, '\\')) return 64;
            strlcpy(directory, executable, sizeof(directory));
            char *last = strrchr(directory, '/');
            if (!last) return 64;
            *last = 0;
            snprintf(profile, sizeof(profile),
                "(version 1) (deny default (with no-log)) "
                "(allow sysctl-read file-read-metadata process-fork) "
                "(allow file-read* (literal \"/\") (subpath \"/System\") (subpath \"/usr/lib\") "
                "(subpath \"/usr/share\") (subpath \"/private/var/db/dyld\") "
                "(literal \"/dev/null\") (literal \"/dev/urandom\") (literal \"/dev/random\") (subpath \"%s\")) "
                "(allow file-write* (literal \"/dev/null\")) "
                "(allow process-exec (literal \"%s\")) "
                "(deny syscall-unix (syscall-number %d %d %d) (with no-log))",
                directory, executable, SYS_setpgid, SYS_setsid, SYS_posix_spawn);
        } else if (!strcmp(argv[1], "filter-spawn"))
            snprintf(profile, sizeof(profile),
                "(version 1) (allow default) (deny syscall-unix (syscall-number %d %d %d) (with no-log))",
                SYS_setpgid, SYS_setsid, SYS_posix_spawn);
        else
            snprintf(profile, sizeof(profile),
                "(version 1) (allow default) (deny syscall-unix (syscall-number %d %d) (with no-log))",
                SYS_setpgid, SYS_setsid);
        char *error = NULL;
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
        int failed = sandbox_init(profile, 0, &error);
        if (error) sandbox_free_error(error);
#pragma clang diagnostic pop
        if (failed) { puts("{\"sandbox_installed\":false}"); return 78; }
    }
    if (!strcmp(argv[2], "leader-setsid")) {
        errno = 0;
        int result = setsid() < 0 ? -1 : 0;
        int failure = errno;
        emit("leader-setsid", result, failure);
        return 0;
    }
    if (!strcmp(argv[2], "spawn-group") || !strcmp(argv[2], "spawn-session")) {
        posix_spawnattr_t attr;
        if (posix_spawnattr_init(&attr)) return 70;
        short flags = !strcmp(argv[2], "spawn-group") ? POSIX_SPAWN_SETPGROUP : POSIX_SPAWN_SETSID;
        if (posix_spawnattr_setflags(&attr, flags) || posix_spawnattr_setpgroup(&attr, 0)) return 70;
        char *args[] = {argv[0], argv[1], "spawn-result", NULL};
        pid_t child;
        int result = posix_spawn(&child, argv[0], NULL, &attr, args, environ);
        posix_spawnattr_destroy(&attr);
        if (result) { emit(argv[2], -1, result); return 0; }
        int status;
        if (waitpid(child, &status, 0) != child) return 71;
        return WIFEXITED(status) ? WEXITSTATUS(status) : 72;
    }
    pid_t child = fork();
    if (child < 0) { emit("fork", -1, errno); return 0; }
    if (!child) {
        int result;
        errno = 0;
        if (!strcmp(argv[2], "setsid")) result = setsid() < 0 ? -1 : 0;
        else if (!strcmp(argv[2], "setpgid")) result = setpgid(0, 0);
        else if (!strcmp(argv[2], "fork-only")) result = 0;
        else if (!strcmp(argv[2], "fork-exec")) {
            char *args[] = {argv[0], argv[1], "exec-setsid-result", NULL};
            execve(argv[0], args, environ);
            _exit(73);
        }
        else _exit(64);
        int failure = errno;
        emit(argv[2], result, failure);
        _exit(0);
    }
    int status;
    if (waitpid(child, &status, 0) != child) return 71;
    if (WIFSIGNALED(status)) {
        printf("{\"child_signal\":%d}\n", WTERMSIG(status));
        return 72;
    }
    return WEXITSTATUS(status);
}
