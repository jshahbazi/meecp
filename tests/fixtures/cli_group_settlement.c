/* Probe held-leader group settlement. Only the owned group receives signals.
 * Children are finite fixtures; inherited pipe EOF is an independent witness.
 * Member IDs are neither published nor used as individual signal targets. */
#define _DARWIN_C_SOURCE 1
#include <errno.h>
#include <libproc.h>
#include <poll.h>
#include <sandbox.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static double now(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (double)t.tv_sec + (double)t.tv_nsec / 1e9;
}

static int live_members(pid_t group) {
    pid_t members[16];
    // Diagnostic only. The native owner never uses discovered process IDs.
    return proc_listpgrppids(group, members, sizeof(members));
}

static int only_held_leader(pid_t group) {
    pid_t members[2] = {0, 0};
    int count = proc_listpgrppids(group, members, sizeof(members));
    return count == 1 && members[0] == group;
}

int main(int argc, char **argv) {
    int stress = argc == 2 && !strcmp(argv[1], "stress");
    if (argc > 2 || (argc == 2 && !stress)) return 64;
    int ready[2], leader_stop[2], witness[2];
    if (pipe(ready) || pipe(leader_stop) || pipe(witness)) return 64;
    pid_t leader = fork();
    if (leader < 0) return 70;
    if (!leader) {
        close(ready[0]); close(leader_stop[1]); close(witness[0]);
        alarm(5);
        if (setsid() < 0) _exit(71);
        char profile[512];
        snprintf(profile, sizeof(profile),
            "(version 1) (deny default (with no-log)) "
            "(allow sysctl-read file-read-metadata process-fork) "
            "(deny syscall-unix (syscall-number %d %d %d) (with no-log))",
            SYS_setpgid, SYS_setsid, SYS_posix_spawn);
        char *sandbox_error = NULL;
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
        int sandbox_failed = sandbox_init(profile, 0, &sandbox_error);
        if (sandbox_error) sandbox_free_error(sandbox_error);
#pragma clang diagnostic pop
        if (sandbox_failed) _exit(79);
        pid_t child = fork();
        if (child < 0) _exit(72);
        if (!child) {
            close(leader_stop[0]);
            signal(SIGTERM, SIG_IGN);
            alarm(5);
            pid_t grandchild = fork();
            if (grandchild < 0) _exit(73);
            if (!grandchild) alarm(5);
            if (write(ready[1], "D", 1) != 1) _exit(74);
            close(ready[1]);
            if (stress && grandchild > 0) {
                for (int i = 0; i < 32; ++i) {
                    pid_t extra = fork();
                    if (!extra) { alarm(5); for (;;) pause(); }
                    if (extra < 0) break;
                    usleep(1000);
                }
            }
            for (;;) pause();
        }
        if (write(ready[1], "L", 1) != 1) _exit(75);
        close(ready[1]); close(witness[1]);
        char stop;
        (void)read(leader_stop[0], &stop, 1);
        _exit(0);
    }
    close(ready[1]); close(leader_stop[0]); close(witness[1]);
    for (int i = 0; i < 3; ++i) {
        struct pollfd fd = {ready[0], POLLIN, 0};
        if (poll(&fd, 1, 2000) <= 0) goto failure;
        char marker;
        if (read(ready[0], &marker, 1) != 1) goto failure;
    }
    close(ready[0]);
    if (write(leader_stop[1], "X", 1) != 1) goto failure;
    close(leader_stop[1]);
    siginfo_t info;
    memset(&info, 0, sizeof(info));
    if (waitid(P_PID, (id_t)leader, &info, WEXITED | WNOWAIT) || info.si_pid != leader) goto failure;
    int before = live_members(leader);
    int before_signal_zero = kill(-leader, 0);
    (void)kill(-leader, SIGTERM);
    usleep(20000);
    (void)kill(-leader, SIGKILL);
    int immediate_after_kill = live_members(leader);
    struct pollfd observed = {witness[0], POLLIN, 0};
    int after = -1, zero_result = 0, zero_errno = 0;
    int kill_rounds = 1;
    double deadline = now() + 2;
    do {
        after = live_members(leader);
        errno = 0;
        zero_result = kill(-leader, 0);
        zero_errno = errno;
        if (only_held_leader(leader)) break;
        (void)kill(-leader, SIGKILL);
        ++kill_rounds;
        usleep(1000);
    } while (now() < deadline);
    memset(&info, 0, sizeof(info));
    int still_held = waitid(P_PID, (id_t)leader, &info, WEXITED | WNOWAIT) == 0 && info.si_pid == leader;
    int singleton_proved = only_held_leader(leader);
    after = live_members(leader);
    int witness_closed = poll(&observed, 1, 1000) > 0 && (observed.revents & POLLHUP);
    int status;
    if (waitpid(leader, &status, 0) != leader) return 76;
    close(witness[0]);
    printf("{\"before_members\":%d,\"before_signal_zero\":%d,\"after_members\":%d,"
           "\"after_signal_zero\":%d,\"after_signal_errno\":%d,\"witness_closed\":%s,"
           "\"leader_still_held\":%s,\"singleton_proved\":%s,\"immediate_after_kill_members\":%d,\"kill_rounds\":%d}\n",
           before, before_signal_zero, after, zero_result, zero_errno,
           witness_closed ? "true" : "false", still_held ? "true" : "false",
           singleton_proved ? "true" : "false", immediate_after_kill, kill_rounds);
    return witness_closed && still_held && singleton_proved ? 0 : 77;
failure:
    // leader is still our unreaped child; no PID can have been reused.
    (void)kill(-leader, SIGKILL);
    (void)kill(leader, SIGKILL);
    (void)waitpid(leader, NULL, 0);
    return 78;
}
