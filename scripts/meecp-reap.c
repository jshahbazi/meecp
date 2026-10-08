/* One foreground target, an execution-spec FD, and an ownership-control FD.
 * Opt-in preparation groups retain the leader's unreaped identity through
 * group cleanup. This covers descendants that remain in the declared group;
 * arbitrary executable workers which escape that group remain unsupported.
 * No child output or executable/environment values are diagnostics. */
#define _POSIX_C_SOURCE 200809L
#define _DARWIN_C_SOURCE 1
#define _DEFAULT_SOURCE 1
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
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t stopping = 0;
static pid_t owner;
static int control;
static int completion = -1;
static char receipt[105];
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
static int report(const char *event, long long value) {
    char message[100];
    int length = snprintf(message, sizeof(message), "{\"event\":\"%s\",\"value\":%lld}\n", event, value);
    ssize_t count;
    do { count = write(control, message, (size_t)length); } while (count < 0 && errno == EINTR);
    return count == length ? 0 : -1;
}
static int declared(int fd, uint32_t *fds, uint32_t count) {
    for (uint32_t i = 0; i < count; ++i) if (fds[i] == (uint32_t)fd) return 1;
    return 0;
}
static int lower_hex(const char *value, size_t size) {
    for (size_t i = 0; i < size; ++i)
        if (!((value[i] >= '0' && value[i] <= '9') || (value[i] >= 'a' && value[i] <= 'f'))) return 0;
    return 1;
}
static int receipt_capability(int fd) {
    struct stat info;
    if (fstat(fd, &info) || !S_ISREG(info.st_mode) || info.st_uid != getuid() ||
        (info.st_mode & 07777) != 0600 || info.st_nlink != 1 || info.st_size != (off_t)sizeof(receipt) ||
        (fcntl(fd, F_GETFL) & O_ACCMODE) != O_RDWR) return -1;
    ssize_t count;
    do { count = pread(fd, receipt, sizeof(receipt), 0); } while (count < 0 && errno == EINTR);
    if (count != (ssize_t)sizeof(receipt) || memcmp(receipt, "MCPMEOR1", 8) ||
        !lower_hex(receipt + 8, 32) || !lower_hex(receipt + 40, 64) || receipt[104] != 'P') return -1;
    return 0;
}
static int completed_receipt(void) {
    if (completion < 0) return 0;
    /* The pre-fork seed is already complete and durable. Only the completion
     * marker changes, after held-child waitpid proof or confirmed no fork. */
    ssize_t count;
    do { count = pwrite(completion, "C", 1, 104); } while (count < 0 && errno == EINTR);
    if (count != 1) return -1;
    int result;
    do { result = fsync(completion); } while (result < 0 && errno == EINTR);
    return result;
}
int main(int argc, char **argv) {
    if (argc != 4 && argc != 5) return 64;
    int spec = atoi(argv[1]);
    control = atoi(argv[2]); owner = (pid_t)atoi(argv[3]);
    if (spec < 3 || control < 3 || spec == control || owner <= 1) return 64;
    if (argc == 5) {
        int supplied = atoi(argv[4]);
        if (supplied < 3 || supplied == spec || supplied == control || receipt_capability(supplied)) return 64;
        completion = supplied;
    }
    signal(SIGPIPE, SIG_IGN);
    signal(SIGTERM, stop_signal); signal(SIGINT, stop_signal);
    char magic[8];
    int owned_group = 0;
    uint32_t grace, input, output, extra_count, arg_count, env_count;
    if (read_exact(spec, magic, 8)) goto invalid;
    owned_group = !memcmp(magic, "MCPME003", 8) || !memcmp(magic, "MCPME004", 8);
    if (completion >= 0) {
        char *instance = NULL, *nonce = NULL;
        if ((memcmp(magic, "MCPME002", 8) && memcmp(magic, "MCPME004", 8)) || !(instance = string(spec)) || !(nonce = string(spec)) ||
            strlen(instance) != 32 || strlen(nonce) != 64 ||
            memcmp(instance, receipt + 8, 32) || memcmp(nonce, receipt + 40, 64)) {
            free(instance); free(nonce); goto invalid;
        }
        free(instance); free(nonce);
    } else if (memcmp(magic, "MCPME001", 8) && memcmp(magic, "MCPME003", 8)) goto invalid;
    if (number(spec, &grace) || grace > 30000 || number(spec, &input) ||
        number(spec, &output) || number(spec, &extra_count) || extra_count > 64) goto invalid;
    if (input < 3 || output < 3 || input == output || input == (uint32_t)spec || output == (uint32_t)spec ||
        input == (uint32_t)control || output == (uint32_t)control ||
        (completion >= 0 && (input == (uint32_t)completion || output == (uint32_t)completion))) goto invalid;
    uint32_t extras[64];
    for (uint32_t i = 0; i < extra_count; ++i) {
        if (number(spec, &extras[i]) || extras[i] < 3 || extras[i] == (uint32_t)spec ||
            extras[i] == (uint32_t)control || (completion >= 0 && extras[i] == (uint32_t)completion)) goto invalid;
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
        umask(0077);
        if (owned_group && setsid() < 0) goto exec_failed;
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
        struct rusage usage;
        pid_t result;
        if (owned_group) {
            siginfo_t info;
            memset(&info, 0, sizeof(info));
            /* Do not consume the leader: its held identity prevents a reused
             * PID/group ID from becoming a signal target during settlement. */
            if (waitid(P_PID, (id_t)child, &info, WEXITED | WNOHANG | WNOWAIT)) {
                if (errno == EINTR) continue;
                return 70;
            }
            result = info.si_pid == child ? child : 0;
            if (result == child) {
                if (!acknowledged && !exec_failed_flag) {
                    int failure;
                    ssize_t count = read(exec_status[0], &failure, sizeof(failure));
                    exec_failed_flag = count > 0;
                    if (count == 0) {
                        acknowledged = 1;
                        (void)report("spawn", (int)child);
                    }
                }
                if (!stop_sent) {
                    if (acknowledged) (void)kill(-child, SIGTERM);
                    stop_sent = 1; stop_at = now();
                }
                if (now() - stop_at < (double)grace / 1000) {
                    (void)poll(NULL, 0, 10);
                    continue;
                }
                if (acknowledged) (void)kill(-child, SIGKILL);
                do { result = wait4(child, &status, 0, &usage); } while (result < 0 && errno == EINTR);
            }
        } else result = waitpid(child, &status, WNOHANG);
        if (result == child) {
            if (completed_receipt()) { close(exec_status[0]); close(control); if (completion >= 0) close(completion); return 74; }
            /* A very short successful target may exit before spawn reporting. */
            if (!acknowledged && !exec_failed_flag) {
                int failure;
                ssize_t count = read(exec_status[0], &failure, sizeof(failure));
                exec_failed_flag = count > 0;
                if (!exec_failed_flag) (void)report("spawn", (int)child);
            }
            int code = WIFEXITED(status) ? WEXITSTATUS(status) : -WTERMSIG(status);
            if (owned_group) {
                /* Darwin reports bytes; the other supported test platform
                 * reports KiB. This is the foreground target's observed peak,
                 * not an aggregate simultaneous descendant-tree measurement. */
#ifdef __APPLE__
                long long peak = (long long)usage.ru_maxrss;
#else
                long long peak = (long long)usage.ru_maxrss * 1024;
#endif
                (void)report("peak_memory", peak);
            }
            (void)report(exec_failed_flag ? "launch_failed" : "exit", code);
            close(exec_status[0]); close(control); if (completion >= 0) close(completion);
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
            if (!owned_group || acknowledged) (void)kill(owned_group ? -child : child, SIGTERM);
            /* The target may still be before setsid(). Its child identity is
             * also held, so signal it directly at this launch boundary. */
            if (owned_group) (void)kill(child, SIGTERM);
            stop_sent = 1; stop_at = now();
        }
        if (stop_sent && !killed && now() - stop_at >= (double)grace / 1000) {
            if (!owned_group || acknowledged) (void)kill(owned_group ? -child : child, SIGKILL);
            if (owned_group) (void)kill(child, SIGKILL);
            killed = 1;
        }
    }
invalid:
    if (completed_receipt()) return 74;
    (void)report("launch_failed", 64);
    if (completion >= 0) close(completion);
    return 64;
}
