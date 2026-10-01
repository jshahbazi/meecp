/* One foreground target, an execution-spec FD, and an ownership-control FD.
 * No child output or executable/environment values are diagnostics. */
#define _POSIX_C_SOURCE 200809L
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t stopping = 0;
static pid_t owner;
static int control;
static void stop_signal(int signum) { (void)signum; stopping = 1; }
static double now(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (double)t.tv_sec + (double)t.tv_nsec / 1e9;
}
static int parent_alive(void) { return !stopping && getppid() == owner; }
static int read_exact(int fd, void *out, size_t size) {
    size_t used = 0;
    while (used < size) {
        if (!parent_alive()) return -1;
        struct pollfd fds[2] = {{fd, POLLIN, 0}, {control, POLLIN, 0}};
        int result = poll(fds, 2, 50);
        if (result < 0 && errno == EINTR) continue;
        if (result < 0 || fds[1].revents) return -1;
        if (!(fds[0].revents & POLLIN)) {
            if (fds[0].revents) return -1;
            continue;
        }
        ssize_t count = read(fd, (char *)out + used, size - used);
        if (count <= 0) return -1;
        used += (size_t)count;
    }
    return 0;
}
static int number(int fd, uint32_t *value) {
    uint32_t raw;
    if (read_exact(fd, &raw, sizeof(raw))) return -1;
    *value = ntohl(raw);
    return 0;
}
static size_t remaining = 1024 * 1024;
static char *string(int fd) {
    uint32_t size;
    if (number(fd, &size) || size > remaining) return NULL;
    remaining -= size;
    char *value = malloc((size_t)size + 1);
    if (!value || read_exact(fd, value, size) || memchr(value, 0, size)) {
        free(value); return NULL;
    }
    value[size] = 0;
    return value;
}
static int report(const char *event, int value) {
    char message[100];
    int length = snprintf(message, sizeof(message), "{\"event\":\"%s\",\"value\":%d}\n", event, value);
    ssize_t count;
    do { count = write(control, message, (size_t)length); } while (count < 0 && errno == EINTR);
    return count == length ? 0 : -1;
}
static int declared(int fd, uint32_t *fds, uint32_t count) {
    for (uint32_t i = 0; i < count; ++i) if (fds[i] == (uint32_t)fd) return 1;
    return 0;
}
int main(int argc, char **argv) {
    if (argc != 4) return 64;
    int spec = atoi(argv[1]);
    control = atoi(argv[2]); owner = (pid_t)atoi(argv[3]);
    if (spec < 3 || control < 3 || spec == control || owner <= 1) return 64;
    signal(SIGPIPE, SIG_IGN);
    signal(SIGTERM, stop_signal); signal(SIGINT, stop_signal);
    char magic[8];
    uint32_t grace, input, output, extra_count, arg_count, env_count;
    if (read_exact(spec, magic, 8) || memcmp(magic, "MCPME001", 8) ||
        number(spec, &grace) || grace > 30000 || number(spec, &input) ||
        number(spec, &output) || number(spec, &extra_count) || extra_count > 64) goto invalid;
    uint32_t extras[64];
    for (uint32_t i = 0; i < extra_count; ++i) {
        if (number(spec, &extras[i]) || extras[i] < 3 || extras[i] == (uint32_t)spec ||
            extras[i] == (uint32_t)control) goto invalid;
    }
    char *executable = string(spec), *cwd = string(spec);
    if (!executable || !cwd || number(spec, &arg_count) || arg_count > 4096) goto invalid;
    char **arguments = calloc((size_t)arg_count + 2, sizeof(char *));
    if (!arguments) goto invalid;
    arguments[0] = executable;
    for (uint32_t i = 0; i < arg_count; ++i) if (!(arguments[i + 1] = string(spec))) goto invalid;
    if (number(spec, &env_count) || env_count > 4096) goto invalid;
    char **environment = calloc((size_t)env_count + 1, sizeof(char *));
    if (!environment) goto invalid;
    for (uint32_t i = 0; i < env_count; ++i) if (!(environment[i] = string(spec))) goto invalid;
    close(spec);
    if (!parent_alive()) goto invalid;
    int exec_status[2];
    if (pipe(exec_status)) goto invalid;
    fcntl(exec_status[1], F_SETFD, FD_CLOEXEC);
    struct rlimit limit;
    if (getrlimit(RLIMIT_NOFILE, &limit) || limit.rlim_cur > INT_MAX) goto invalid;
    pid_t child = fork();
    if (child < 0) goto invalid;
    if (!child) {
        close(exec_status[0]);
        signal(SIGTERM, SIG_DFL); signal(SIGINT, SIG_DFL); signal(SIGPIPE, SIG_DFL);
        int nullfd = open("/dev/null", O_WRONLY);
        if (nullfd < 0 || dup2((int)input, STDIN_FILENO) < 0 ||
            dup2((int)output, STDOUT_FILENO) < 0 || dup2(nullfd, STDERR_FILENO) < 0 || chdir(cwd)) goto exec_failed;
        for (int fd = 3; fd < (int)limit.rlim_cur; ++fd) {
            if (fd != exec_status[1] && !declared(fd, extras, extra_count)) close(fd);
        }
        for (uint32_t i = 0; i < extra_count; ++i) fcntl((int)extras[i], F_SETFD, 0);
        execve(executable, arguments, environment);
exec_failed:
        { int failure = errno; (void)write(exec_status[1], &failure, sizeof(failure)); }
        _exit(127);
    }
    close(exec_status[1]); close((int)input); close((int)output);
    for (uint32_t i = 0; i < extra_count; ++i) close((int)extras[i]);
    int acknowledged = 0, exec_failed_flag = 0, stop_sent = 0, killed = 0;
    double stop_at = 0;
    for (;;) {
        int status;
        pid_t result = waitpid(child, &status, WNOHANG);
        if (result == child) {
            /* A very short successful target may exit before spawn reporting. */
            if (!acknowledged && !exec_failed_flag) {
                int failure;
                ssize_t count = read(exec_status[0], &failure, sizeof(failure));
                exec_failed_flag = count > 0;
                if (!exec_failed_flag) (void)report("spawn", (int)child);
            }
            int code = WIFEXITED(status) ? WEXITSTATUS(status) : -WTERMSIG(status);
            (void)report(exec_failed_flag ? "launch_failed" : "exit", code);
            close(exec_status[0]); close(control);
            return 0;
        }
        if (result < 0 && errno != EINTR) return 70;
        struct pollfd fds[2] = {
            {stopping ? -1 : control, POLLIN, 0},
            {acknowledged || exec_failed_flag ? -1 : exec_status[0], POLLIN, 0}
        };
        (void)poll(fds, 2, 50);
        if (fds[0].revents) stopping = 1;
        if (!acknowledged && !exec_failed_flag && fds[1].revents) {
            int failure;
            ssize_t count = read(exec_status[0], &failure, sizeof(failure));
            if (count > 0) exec_failed_flag = 1;
            else if (count == 0) {
                acknowledged = 1;
                if (report("spawn", (int)child)) stopping = 1;
            }
        }
        if (!parent_alive() && !stop_sent) {
            /* The unreaped child identity is still held by this process. */
            (void)kill(child, SIGTERM); stop_sent = 1; stop_at = now();
        }
        if (stop_sent && !killed && now() - stop_at >= (double)grace / 1000) {
            (void)kill(child, SIGKILL); killed = 1;
        }
    }
invalid:
    (void)report("launch_failed", 64);
    return 64;
}
