/* Publication-format-4 monitored resource owner; intermediate report-only API.
 * Never discover/adopt signal targets. Never log payload values. */
#define _DARWIN_C_SOURCE 1
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <libproc.h>
#include <limits.h>
#include <poll.h>
#include <sandbox.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/sysctl.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "mcpme-command-resource-v1.h"
#define SEED_SIZE 360
#define RECEIPT_SIZE (SEED_SIZE + 5 + RESOURCE_REPORT_SIZE)
#define POLICY "macos26-arm64-monitored-fork-exec-v2"
static struct resource_monitor monitor;
static volatile sig_atomic_t stopping;
static int control, receipt_fd, directory_fd;
static int receipt_valid, forked, transferred;
static char receipt_seed[SEED_SIZE], staged_name[64], complete_name[64];
static pid_t owner;
static size_t remaining = 65536;
static void stop_signal(int number) { (void)number; stopping = 1; }
static double now(void) {
    struct timespec value;
    clock_gettime(CLOCK_MONOTONIC, &value);
    return (double)value.tv_sec + (double)value.tv_nsec / 1e9;
}
static int owner_present(void) {
    struct pollfd item = {control, POLLIN, 0};
    int result = poll(&item, 1, 0);
    return !stopping && getppid() == owner && result >= 0 && !item.revents;
}
static int read_exact(int fd, void *output, size_t size) {
    size_t used = 0;
    double deadline = now() + 5;
    while (used < size) {
        if (!owner_present() || now() >= deadline) return -1;
        struct pollfd item = {fd, POLLIN, 0};
        int result = poll(&item, 1, 10);
        if (result < 0 && errno == EINTR) continue;
        if (result < 0 || (!(item.revents & POLLIN) && item.revents)) return -1;
        if (!(item.revents & POLLIN)) continue;
        ssize_t count = read(fd, (char *)output + used, size - used);
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
static char *string(int fd) {
    uint32_t size;
    if (number(fd, &size) || size > remaining) return NULL;
    remaining -= size;
    char *value = calloc((size_t)size + 1, 1);
    if (!value || read_exact(fd, value, size) || memchr(value, 0, size)) {
        free(value); return NULL;
    }
    return value;
}
static int capability(int fd, const char *seed) {
    struct stat info;
    char frame[RECEIPT_SIZE];
    if (fstat(fd, &info) || !S_ISREG(info.st_mode) || info.st_uid != getuid() ||
        (info.st_mode & 07777) != 0600 || info.st_nlink != 1 || info.st_size != RECEIPT_SIZE ||
        (fcntl(fd, F_GETFL) & O_ACCMODE) != O_RDWR ||
        pread(fd, frame, sizeof(frame), 0) != sizeof(frame) || memcmp(frame, seed, SEED_SIZE) ||
        memcmp(frame, "MCPMECR4", 8) || frame[SEED_SIZE] != 'P') return -1;
    for (int i = 8; i < SEED_SIZE; ++i)
        if (!((frame[i] >= '0' && frame[i] <= '9') || (frame[i] >= 'a' && frame[i] <= 'f'))) return -1;
    return 0;
}
static int publication_capability(mode_t mode) {
    struct stat directory, descriptor, staged;
    snprintf(staged_name, sizeof(staged_name), "%.32s.command-resource-v1.pending", receipt_seed + 8);
    snprintf(complete_name, sizeof(complete_name), "%.32s.command-resource-v1.complete", receipt_seed + 8);
    return fstat(directory_fd, &directory) || !S_ISDIR(directory.st_mode) || directory.st_uid != getuid() ||
        (directory.st_mode & 07777) != 0700 || fstat(receipt_fd, &descriptor) ||
        fstatat(directory_fd, staged_name, &staged, AT_SYMLINK_NOFOLLOW) || !S_ISREG(staged.st_mode) ||
        staged.st_dev != descriptor.st_dev || staged.st_ino != descriptor.st_ino ||
        staged.st_uid != getuid() || (staged.st_mode & 07777) != mode || staged.st_nlink != 1;
}
static int publish_completion(char marker, int code) {
    /* The final path is the publication barrier. Its complete payload and
     * private read-only mode are synced before any recovery reader can see it.
     * No fallible payload operation follows the no-replace rename. */
    char frame[RECEIPT_SIZE];
    memcpy(frame, receipt_seed, SEED_SIZE);
    frame[SEED_SIZE] = marker;
    uint32_t encoded = htonl((uint32_t)(int32_t)code);
    memcpy(frame + SEED_SIZE + 1, &encoded, sizeof(encoded));
    resource_encode(&monitor, frame + SEED_SIZE + 5);
    if (publication_capability(0600) || pwrite(receipt_fd, frame, sizeof(frame), 0) != sizeof(frame) ||
        fchmod(receipt_fd, 0400)) return -1;
    if (fsync(receipt_fd)) return -1;
    if (publication_capability(0400)) return -1;
    /* Close all writable copies before publication. The host transfers its
     * copy before sending the start byte; selected children close theirs. */
    if (close(receipt_fd)) { receipt_fd = -1; return -1; }
    receipt_fd = -1;
    return renameatx_np(directory_fd, staged_name, directory_fd, complete_name, RENAME_EXCL);
}
static int completed(int status) {
    int code = WIFEXITED(status) ? WEXITSTATUS(status) : -WTERMSIG(status);
    return publish_completion('C', code);
}
static int await_transfer(void) {
    double deadline = now() + 5;
    while (!stopping && getppid() == owner && now() < deadline) {
        struct pollfd item = {control, POLLIN, 0};
        int result = poll(&item, 1, 10);
        if (result < 0 && errno == EINTR) continue;
        if (result < 0) return -1;
        if (item.revents & POLLIN) {
            char marker;
            return read(control, &marker, 1) == 1 && marker == 'G' ? 0 : -1;
        }
        if (item.revents) return -1;
    }
    return -1;
}
static char *quoted(const char *value) {
    size_t size = strlen(value);
    if (size >= PATH_MAX) return NULL;
    char *result = calloc(size * 2 + 3, 1);
    if (!result) return NULL;
    size_t offset = 0;
    result[offset++] = '"';
    for (size_t i = 0; i < size; ++i) {
        unsigned char c = (unsigned char)value[i];
        if (c < 32 || c == 127) { free(result); return NULL; }
        if (c == '"' || c == '\\') result[offset++] = '\\';
        result[offset++] = (char)c;
    }
    result[offset] = '"';
    return result;
}
static int confined(const char *root, const char *executable) {
    char *root_literal = quoted(root), *exe_literal = quoted(executable);
    if (!root_literal || !exe_literal) return -1;
    const char *format =
        "(version 1) (deny default (with no-log)) "
        "(allow sysctl-read file-read-metadata process-fork) "
        "(allow file-read* (literal \"/\") (subpath \"/System\") (subpath \"/usr/lib\") "
        "(subpath \"/usr/share\") (subpath \"/private/var/db/dyld\") "
        "(literal \"/dev/null\") (literal \"/dev/urandom\") (literal \"/dev/random\") (subpath %s)) "
        "(allow file-write* (literal \"/dev/null\")) (allow process-exec (literal %s)) "
        "(deny syscall-unix (syscall-number %d %d %d) (with no-log))";
    int size = snprintf(NULL, 0, format, root_literal, exe_literal, SYS_setpgid, SYS_setsid, SYS_posix_spawn);
    if (size < 0 || size > PATH_MAX * 4 + 2048) return -1;
    char *profile = calloc((size_t)size + 1, 1);
    if (!profile) return -1;
    snprintf(profile, (size_t)size + 1, format, root_literal, exe_literal, SYS_setpgid, SYS_setsid, SYS_posix_spawn);
    char *error = NULL;
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
    int result = sandbox_init(profile, 0, &error);
    if (error) sandbox_free_error(error);
#pragma clang diagnostic pop
    free(profile); free(root_literal); free(exe_literal);
    return result;
}
static int platform_supported(void) {
    struct utsname info;
    char version[64]; size_t size = sizeof(version);
    return !uname(&info) && !strcmp(info.machine, "arm64") &&
        !sysctlbyname("kern.osproductversion", version, &size, NULL, 0) && !strncmp(version, "26.", 3);
}
static int held(pid_t child) {
    siginfo_t info;
    memset(&info, 0, sizeof(info));
    if (waitid(P_PID, (id_t)child, &info, WEXITED | WNOHANG | WNOWAIT)) return errno == EINTR ? 0 : -1;
    return info.si_pid == child;
}
static int singleton(pid_t child) {
    pid_t members[2] = {0, 0};
    int count = proc_listpgrppids(child, members, sizeof(members));
    if (count == 1 && members[0] == child) return 1;
    return count == 2 ? 0 : -1;
}
static int run(int argc, char **argv) {
    if (argc != 6 || !platform_supported()) return 64;
    int spec = atoi(argv[1]); control = atoi(argv[2]); receipt_fd = atoi(argv[3]); directory_fd = atoi(argv[4]); owner = (pid_t)atoi(argv[5]);
    if (spec < 3 || control < 3 || receipt_fd < 3 || directory_fd < 3 || spec == control || spec == receipt_fd ||
        control == receipt_fd || directory_fd == spec || directory_fd == control || directory_fd == receipt_fd || owner <= 1) return 64;
    if (pread(receipt_fd, receipt_seed, sizeof(receipt_seed), 0) != sizeof(receipt_seed) ||
        capability(receipt_fd, receipt_seed) || publication_capability(0600)) return 64;
    receipt_valid = 1;
    signal(SIGPIPE, SIG_IGN); signal(SIGTERM, stop_signal); signal(SIGINT, stop_signal);
    if (await_transfer()) return 64;
    transferred = 1;
    char magic[8], seed[SEED_SIZE];
    uint32_t deadline_ms, grace_ms, settlement_ms, count;
    if (read_exact(spec, magic, 8) || memcmp(magic, "MCPMEC04", 8) || read_exact(spec, seed, sizeof(seed)) ||
        capability(receipt_fd, seed) || number(spec, &deadline_ms) || deadline_ms < 1 || deadline_ms > 3600000 ||
        number(spec, &grace_ms) || grace_ms > 1000 || number(spec, &settlement_ms) || settlement_ms < 50 || settlement_ms > 10000) return 64;
    uint64_t resource_values[RESOURCE_POLICY_FIELDS];
    if (read_exact(spec, resource_values, sizeof(resource_values))) return 64;
    for (int i = 0; i < RESOURCE_POLICY_FIELDS; ++i) resource_values[i] = resource_wire64(resource_values[i]);
    monitor.policy = (struct resource_policy){resource_values[0], resource_values[1], resource_values[2], resource_values[3],
        resource_values[4], resource_values[5], resource_values[6], resource_values[7]};
    if (resource_init(&monitor)) return 64;
    char *policy = string(spec), *root = string(spec), *executable = string(spec), *cwd = string(spec);
    if (!policy || strcmp(policy, POLICY) || !root || !executable || !cwd || number(spec, &count) || count > 128) return 64;
    char resolved[PATH_MAX];
    if (!realpath(root, resolved) || strcmp(root, resolved) || !realpath(executable, resolved) || strcmp(executable, resolved) ||
        !realpath(cwd, resolved) || strcmp(cwd, resolved)) return 64;
    size_t root_size = strlen(root);
    if (root_size < 2 || strncmp(root, executable, root_size) || executable[root_size] != '/' ||
        (strcmp(root, cwd) && (strncmp(root, cwd, root_size) || cwd[root_size] != '/'))) return 64;
    char **arguments = calloc((size_t)count + 2, sizeof(char *));
    if (!arguments) return 70;
    arguments[0] = executable;
    for (uint32_t i = 0; i < count; ++i) if (!(arguments[i + 1] = string(spec))) return 64;
    close(spec);
    if (!owner_present()) return 64;
    int ready[2], execute_gate[2];
    if (pipe(execute_gate) || pipe(ready) || fcntl(ready[1], F_SETFD, FD_CLOEXEC) || fcntl(ready[0], F_SETFL, O_NONBLOCK)) return 70;
    struct rlimit descriptors;
    if (getrlimit(RLIMIT_NOFILE, &descriptors) || descriptors.rlim_cur > INT_MAX) return 70;
    pid_t child = fork();
    if (child < 0) return 70;
    if (!child) {
        signal(SIGTERM, SIG_DFL); signal(SIGINT, SIG_DFL); signal(SIGPIPE, SIG_DFL);
        umask(0077);
        for (int fd = 3; fd < (int)descriptors.rlim_cur; ++fd) if (fd != ready[1] && fd != execute_gate[0]) close(fd);
        if (setsid() < 0 || chdir(cwd) || confined(root, executable) || write(ready[1], "G", 1) != 1) _exit(126);
        char permission;
        if (read(execute_gate[0], &permission, 1) != 1 || permission != 'E') _exit(126);
        close(execute_gate[0]);
        char *environment[] = {NULL};
        execve(executable, arguments, environment);
        _exit(127);
    }
    forked = 1;
    close(ready[1]); close(execute_gate[0]); close(STDIN_FILENO); close(STDOUT_FILENO);
    double deadline = now() + (double)deadline_ms / 1000, stop_at = 0, settle_deadline = 0;
    int group_ready = 0, invalid_proof = 0, source_released = 0, resource_stopped = 0;
    uint64_t next_sample = 0;
    for (;;) {
        char marker;
        if (read(ready[0], &marker, 1) == 1 && marker == 'G') group_ready = 1;
        int exited = held(child);
        if (exited < 0) return 70;
        if (exited && !group_ready) {
            /* The child may have written G and exited between the first
             * nonblocking read and waitid. Re-read only after exit proof.
             * EOF without G proves source exec was never reached; anything
             * else is uncertain and cannot certify a descendant-free child. */
            ssize_t count = read(ready[0], &marker, 1);
            if (count == 1 && marker == 'G') group_ready = 1;
            else if (count != 0) invalid_proof = 1;
        }
        if (group_ready && !stop_at && !resource_stopped) {
            uint64_t tick;
            if (resource_clock(&monitor, &tick)) resource_stopped = resource_fail(&monitor, RR_OVERFLOW);
            else if (!source_released || exited || tick >= next_sample) {
                resource_stopped = resource_sample(&monitor, child, exited);
                if (resource_add(tick, monitor.policy.interval, &next_sample)) resource_stopped = resource_fail(&monitor, RR_OVERFLOW);
                if (!source_released && !resource_stopped && !exited) {
                    if (write(execute_gate[1], "E", 1) != 1) resource_stopped = resource_fail(&monitor, RR_CONTROL);
                    source_released = 1;
                    close(execute_gate[1]); execute_gate[1] = -1;
                }
            }
        }
        if (!stop_at && (exited || !owner_present() || now() >= deadline || resource_stopped)) {
            if (!monitor.report.reason && !owner_present()) monitor.report.reason = RR_CONTROL;
            else if (!monitor.report.reason && now() >= deadline) monitor.report.reason = RR_DEADLINE;
            if (!monitor.report.samples) monitor.report.valid = 0;
            if (!monitor.report.reason && !monitor.report.samples) monitor.report.reason = RR_NO_SAMPLE;
            if (execute_gate[1] >= 0) { close(execute_gate[1]); execute_gate[1] = -1; }
            stop_at = now(); settle_deadline = stop_at + (double)(grace_ms + settlement_ms) / 1000;
            if (group_ready) (void)kill(-child, SIGTERM);
            if (!exited) (void)kill(child, SIGTERM);
        }
        if (stop_at && now() - stop_at >= (double)grace_ms / 1000) {
            if (group_ready) (void)kill(-child, SIGKILL);
            if (!exited) (void)kill(child, SIGKILL);
            if (exited) {
                int alone = group_ready ? singleton(child) : 1;
                if (alone < 0) invalid_proof = 1;
                if (alone == 1 && !invalid_proof && held(child) == 1) {
                    int status;
                    pid_t result;
                    do { result = wait4(child, &status, 0, NULL); } while (result < 0 && errno == EINTR);
                    if (result != child || completed(status)) return 74;
                    return 0;
                }
            }
            if (now() >= settle_deadline) return 75;
        }
        (void)poll(NULL, 0, 5);
    }
}

int main(int argc, char **argv) {
    monitor.report.reason = RR_POLICY;
    int result = run(argc, argv);
    /* A canceled/incomplete pre-fork exchange has a positive no-child proof.
     * Only this native owner knows whether fork occurred. */
    if (receipt_valid && !forked && (transferred || getppid() != owner)) {
        monitor.report.valid = 0;
        if (!monitor.report.reason) monitor.report.reason = RR_POLICY;
        if (publish_completion('N', 0)) return 74;
    }
    return result;
}
