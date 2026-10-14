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
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#ifdef __APPLE__
#include <libproc.h>
#include <mach/mach.h>
#include <sys/proc.h>
#include <sys/proc_info.h>
#elif defined(__linux__)
#include <sys/syscall.h>
#endif

static volatile sig_atomic_t stopping = 0;
static pid_t owner;
static int control;
static int completion = -1;
static char receipt[197];
static size_t receipt_size;
static int receipt_v2;
static int receipt_group;

/* V2 binds persisted recovery authority to a kernel process lifetime. A PID
 * remains a lookup key only: signals use a freshly validated audit token on
 * Darwin, or a pidfd on the Linux test platform. */
struct process_identity {
    uint32_t pid;
    uint64_t uniqueid;
    uint32_t audit[8];
};
static uint64_t decode64(const unsigned char *value) {
    uint64_t result = 0;
    for (size_t i = 0; i < 8; ++i) result = (result << 8) | value[i];
    return result;
}
static uint32_t decode32(const unsigned char *value) {
    uint32_t result;
    memcpy(&result, value, sizeof(result));
    return ntohl(result);
}
static void encode64(unsigned char *value, uint64_t number) {
    for (int i = 7; i >= 0; --i) { value[i] = (unsigned char)number; number >>= 8; }
}
static void encode32(unsigned char *value, uint32_t number) {
    number = htonl(number); memcpy(value, &number, sizeof(number));
}
static uint32_t receipt_crc(void) {
    uint32_t crc = UINT32_MAX;
    for (size_t i = 0; i < 193; ++i) {
        if (i == 104) continue;
        crc ^= (unsigned char)receipt[i];
        for (int bit = 0; bit < 8; ++bit) crc = (crc >> 1) ^ ((crc & 1) ? UINT32_C(0xedb88320) : 0);
    }
    return ~crc;
}
static int receipt_crc_valid(void) {
    return receipt_v2 && decode32((const unsigned char *)receipt + 193) == receipt_crc();
}
static void encode_identity(unsigned char *value, const struct process_identity *identity) {
    encode32(value, identity->pid); encode64(value + 4, identity->uniqueid);
    for (size_t i = 0; i < 8; ++i) encode32(value + 12 + 4 * i, identity->audit[i]);
}
static void decode_identity(const unsigned char *value, struct process_identity *identity) {
    identity->pid = decode32(value); identity->uniqueid = decode64(value + 4);
    for (size_t i = 0; i < 8; ++i) identity->audit[i] = decode32(value + 12 + 4 * i);
}
static int identity_empty(const struct process_identity *identity) {
    if (identity->pid || identity->uniqueid) return 0;
    for (size_t i = 0; i < 8; ++i) if (identity->audit[i]) return 0;
    return 1;
}
static int identity_valid(const struct process_identity *identity) {
    if (identity->pid <= 1 || identity->pid > INT_MAX || !identity->uniqueid) return 0;
#ifdef __APPLE__
    if (identity->audit[5] != identity->pid || identity->audit[1] != getuid() ||
        identity->audit[3] != getuid()) return 0;
#elif defined(__linux__)
    for (size_t i = 0; i < 8; ++i) if (identity->audit[i]) return 0;
#else
    return 0;
#endif
    return 1;
}
#ifdef __APPLE__
/* Apple's XNU bsd/sys/proc_info.h defines this PRIVATE flavor. Reject any
 * runtime layout change; public start timestamps cannot bind an atomic signal.
 * p_uniqueid spans exec; audit pidversion is refreshed before every signal. */
struct meecp_uniqidentifierinfo {
    uint8_t p_uuid[16];
    uint64_t p_uniqueid;
    uint64_t p_puniqueid;
    int32_t p_idversion;
    uint32_t p_reserve2;
    uint64_t p_reserve3;
    uint64_t p_reserve4;
};
_Static_assert(sizeof(struct meecp_uniqidentifierinfo) == 56, "XNU identity layout");
static int unique_info(pid_t pid, struct meecp_uniqidentifierinfo *info) {
    memset(info, 0, sizeof(*info)); errno = 0;
    int count = proc_pidinfo(pid, 17, 0, info, (int)sizeof(*info));
    if (count == (int)sizeof(*info) && info->p_uniqueid) return 0;
    return count <= 0 && errno == ESRCH ? 1 : -1;
}
static int kernel_identity(pid_t pid, struct process_identity *identity, int *zombie) {
    for (int attempt = 0; attempt < 8; ++attempt) {
        struct meecp_uniqidentifierinfo before, after;
        int result = unique_info(pid, &before);
        if (result) return result;
        struct proc_bsdinfo bsd;
        memset(&bsd, 0, sizeof(bsd)); errno = 0;
        int count = proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &bsd, (int)sizeof(bsd));
        if (count != (int)sizeof(bsd)) return count <= 0 && errno == ESRCH ? 1 : -1;
        if (bsd.pbi_uid != getuid() || bsd.pbi_ruid != getuid()) return -1;
        task_name_t name = MACH_PORT_NULL;
        audit_token_t token;
        memset(&token, 0, sizeof(token));
        if (bsd.pbi_status != SZOMB) {
            if (task_name_for_pid(mach_task_self(), pid, &name) != KERN_SUCCESS) {
                result = unique_info(pid, &after);
                if (result) return result;
                continue;
            }
            mach_msg_type_number_t words = TASK_AUDIT_TOKEN_COUNT;
            kern_return_t queried = task_info(name, TASK_AUDIT_TOKEN, (task_info_t)&token, &words);
            mach_port_deallocate(mach_task_self(), name);
            if (queried != KERN_SUCCESS || words != TASK_AUDIT_TOKEN_COUNT) continue;
        }
        result = unique_info(pid, &after);
        if (result) return result;
        if (before.p_uniqueid != after.p_uniqueid || before.p_idversion != after.p_idversion) continue;
        memset(identity, 0, sizeof(*identity));
        identity->pid = (uint32_t)pid; identity->uniqueid = before.p_uniqueid;
        *zombie = bsd.pbi_status == SZOMB;
        if (*zombie) return 0;
        if (token.val[5] != (uint32_t)pid || token.val[7] != (uint32_t)after.p_idversion ||
            token.val[1] != getuid() || token.val[3] != getuid()) continue;
        memcpy(identity->audit, token.val, sizeof(identity->audit));
        return 0;
    }
    return -1;
}
#elif defined(__linux__)
static int kernel_identity(pid_t pid, struct process_identity *identity, int *zombie) {
    char path[64], data[4096];
    int length = snprintf(path, sizeof(path), "/proc/%d/stat", (int)pid);
    if (length <= 0 || length >= (int)sizeof(path)) return -1;
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return errno == ENOENT || errno == ESRCH ? 1 : -1;
    struct stat info;
    if (fstat(fd, &info) || info.st_uid != getuid()) { close(fd); return -1; }
    ssize_t count;
    do { count = read(fd, data, sizeof(data) - 1); } while (count < 0 && errno == EINTR);
    close(fd);
    if (count <= 0) return -1;
    data[count] = 0;
    char *cursor = strrchr(data, ')');
    if (!cursor || cursor[1] != ' ' || !cursor[2]) return -1;
    *zombie = cursor[2] == 'Z' || cursor[2] == 'X';
    cursor += 3;
    uint64_t started = 0;
    for (int field = 4; field <= 22; ++field) {
        while (*cursor == ' ') ++cursor;
        char *end;
        errno = 0;
        if (field == 22) started = strtoull(cursor, &end, 10);
        else (void)strtoll(cursor, &end, 10);
        if (end == cursor || errno || (*end && *end != ' ' && *end != '\n')) return -1;
        cursor = end;
    }
    if (!started) return -1;
    memset(identity, 0, sizeof(*identity));
    identity->pid = (uint32_t)pid; identity->uniqueid = started;
    return 0;
}
#else
static int kernel_identity(pid_t pid, struct process_identity *identity, int *zombie) {
    (void)pid; (void)identity; (void)zombie; return -1;
}
#endif
/* 1 means this lifetime has exited; -1 means its status is unproven. */
static int observe_identity(const struct process_identity *expected, struct process_identity *current) {
#ifdef __APPLE__
    struct meecp_uniqidentifierinfo lifetime;
    int observed = unique_info((pid_t)expected->pid, &lifetime);
    if (observed) return observed;
    /* Another lifetime at this lookup key proves the persisted one ended,
     * regardless of whether its replacement permits UID/audit inspection. */
    if (lifetime.p_uniqueid != expected->uniqueid) return 1;
#endif
    int zombie = 0;
    int result = kernel_identity((pid_t)expected->pid, current, &zombie);
#ifdef __APPLE__
    if (result < 0) {
        observed = unique_info((pid_t)expected->pid, &lifetime);
        if (observed == 1 || (!observed && lifetime.p_uniqueid != expected->uniqueid)) return 1;
    }
#endif
    if (result) return result;
    return zombie || current->uniqueid != expected->uniqueid ? 1 : 0;
}
static int signal_identity(const struct process_identity *expected, int signum) {
    struct process_identity current;
    int observed = observe_identity(expected, &current);
    if (observed) return observed;
#ifdef __APPLE__
    audit_token_t token;
    memcpy(token.val, current.audit, sizeof(current.audit));
    if (!proc_signal_with_audittoken(&token, signum)) return 0;
    /* Exec can refresh pidversion between observation and the atomic signal.
     * A stale token rejects rather than signaling another incarnation. */
    if (errno == ESRCH) return observe_identity(expected, &current) == 1 ? 1 : 0;
    return -1;
#elif defined(__linux__) && defined(SYS_pidfd_open) && defined(SYS_pidfd_send_signal)
    int fd = (int)syscall(SYS_pidfd_open, (pid_t)expected->pid, 0);
    if (fd < 0) return errno == ESRCH ? 1 : -1;
    observed = observe_identity(expected, &current);
    if (observed) { close(fd); return observed; }
    int result = (int)syscall(SYS_pidfd_send_signal, fd, signum, NULL, 0);
    int failure = errno;
    close(fd);
    if (!result) return 0;
    return failure == ESRCH ? 1 : -1;
#else
    (void)signum; return -1;
#endif
}
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
static int receipt_capability(int fd, int initial) {
    struct stat info;
    if (fstat(fd, &info) || !S_ISREG(info.st_mode) || info.st_uid != getuid() ||
        (info.st_mode & 07777) != 0600 || info.st_nlink != 1 ||
        (info.st_size != 105 && info.st_size != (off_t)sizeof(receipt)) ||
        (fcntl(fd, F_GETFL) & O_ACCMODE) != O_RDWR) return -1;
    receipt_size = (size_t)info.st_size;
    receipt_v2 = receipt_size == sizeof(receipt);
    ssize_t count;
    do { count = pread(fd, receipt, receipt_size, 0); } while (count < 0 && errno == EINTR);
    receipt_group = receipt_v2 && !memcmp(receipt, "MCPMEOG2", 8);
    if (count != (ssize_t)receipt_size ||
        (!receipt_group && memcmp(receipt, receipt_v2 ? "MCPMEOR2" : "MCPMEOR1", 8)) ||
        !lower_hex(receipt + 8, 32) || !lower_hex(receipt + 40, 64) ||
        (initial ? receipt[104] != 'P' : (receipt[104] != 'P' && receipt[104] != 'R' && receipt[104] != 'C'))) return -1;
    if (!receipt_v2 && receipt[104] == 'R') return -1;
    if (initial && receipt_v2) {
        for (size_t i = 105; i < 193; ++i) if (receipt[i]) return -1;
        if (!receipt_crc_valid()) return -1;
    }
    return 0;
}
static int durable_write(int fd, const void *value, size_t size, off_t offset) {
    ssize_t count;
    do { count = pwrite(fd, value, size, offset); } while (count < 0 && errno == EINTR);
    if (count != (ssize_t)size) return -1;
    int result;
    do { result = fsync(fd); } while (result < 0 && errno == EINTR);
    return result;
}
static int persist_identity(pid_t pid, off_t offset) {
    struct process_identity identity;
    int zombie = 0;
    if (kernel_identity(pid, &identity, &zombie) || zombie || !identity_valid(&identity)) return -1;
    unsigned char encoded[44];
    encode_identity(encoded, &identity);
    if (durable_write(completion, encoded, sizeof(encoded), offset)) return -1;
    memcpy(receipt + offset, encoded, sizeof(encoded));
    encode32((unsigned char *)receipt + 193, receipt_crc());
    return durable_write(completion, receipt + 193, 4, 193);
}
static int complete_unexecuted(int fd) {
    memset(receipt + 105, 0, 88);
    encode32((unsigned char *)receipt + 193, receipt_crc());
    if (durable_write(fd, receipt + 105, 92, 105)) return -1;
    return durable_write(fd, "C", 1, 104);
}
static int completed_receipt(void) {
    if (completion < 0) return 0;
    /* Completion follows held-child waitpid proof or confirmed no fork. P
     * never authorized exec and can canonicalize any interrupted identity
     * write; R retains its durable identities and changes only the marker. */
    if (receipt_v2) {
        if (receipt_capability(completion, 0)) return -1;
        if (receipt[104] == 'P') return complete_unexecuted(completion);
        if (!receipt_crc_valid()) return -1;
    }
    return durable_write(completion, "C", 1, 104);
}
static int settle_identity(const struct process_identity *identity) {
    double began = now();
    for (;;) {
        struct process_identity current;
        int observed = observe_identity(identity, &current);
        if (observed == 1) return 0;
        if (observed < 0) return -1;
        int signum = now() - began < 0.25 ? SIGTERM : SIGKILL;
        if (signal_identity(identity, signum) < 0) return -1;
        if (now() - began >= 3.0) return -1;
        (void)poll(NULL, 0, 10);
    }
}
static int recover_group_receipt(int fd) {
    char expected[104];
    memcpy(expected, receipt, sizeof(expected));
    double deadline = now() + 35.0;
    for (;;) {
        if (receipt_capability(fd, 0) || !receipt_group || memcmp(expected, receipt, sizeof(expected))) return 64;
        int valid_crc = receipt_crc_valid();
        if (receipt[104] == 'C') return valid_crc ? 0 : 64;
        if (receipt[104] == 'R' && !valid_crc) return 64;
        struct process_identity reaper, target;
        decode_identity((const unsigned char *)receipt + 149, &reaper);
        decode_identity((const unsigned char *)receipt + 105, &target);
        if (valid_crc && ((!identity_empty(&reaper) && !identity_valid(&reaper)) ||
            (!identity_empty(&target) && !identity_valid(&target)) ||
            (receipt[104] == 'R' && (!identity_valid(&reaper) || !identity_valid(&target))))) return 64;
        if (receipt[104] == 'P' && !flock(fd, LOCK_EX | LOCK_NB)) {
            /* The group owner holds this lock until the exec gate has been
             * released or its P launch has ended. P under an exclusive lock
             * therefore proves no package code can have executed. */
            int result = receipt_capability(fd, 0);
            if (!result && (!receipt_group || memcmp(expected, receipt, sizeof(expected)))) result = -1;
            if (!result && receipt[104] == 'P') result = complete_unexecuted(fd);
            else if (!result && receipt[104] == 'C' && receipt_crc_valid()) result = 0;
            else if (!result) result = 1;
            (void)flock(fd, LOCK_UN);
            if (result <= 0) return result ? 64 : 0;
            continue;
        }
        if (valid_crc && !identity_empty(&reaper)) {
            /* Only the surviving inner owner can prove descendants settled.
             * Killing that sole owner, or observing its exit without C, would
             * lose the unreaped group leader and cannot authorize replacement. */
            int signaled = signal_identity(&reaper, SIGCONT);
            if (!signaled) signaled = signal_identity(&reaper, SIGTERM);
            if (signaled < 0) return 75;
            if (signaled == 1 && receipt[104] == 'R') {
                /* C can become durable between our frame read and observing
                 * the owner exit. Read it again before reporting no proof. */
                if (receipt_capability(fd, 0) || !receipt_group || memcmp(expected, receipt, sizeof(expected))) return 64;
                return receipt[104] == 'C' && receipt_crc_valid() ? 0 : 75;
            }
        }
        if (now() >= deadline) return 75;
        (void)poll(NULL, 0, 20);
    }
}
static int recover_receipt(int fd) {
    if (receipt_capability(fd, 0) || !receipt_v2) return 64;
    if (receipt_group) return recover_group_receipt(fd);
    int valid_crc = receipt_crc_valid();
    if (receipt[104] == 'C') return valid_crc ? 0 : 64;
    if (!valid_crc && receipt[104] != 'P') return 64;
    struct process_identity reaper, target;
    decode_identity((const unsigned char *)receipt + 149, &reaper);
    decode_identity((const unsigned char *)receipt + 105, &target);
    if (valid_crc && ((!identity_empty(&reaper) && !identity_valid(&reaper)) ||
        (!identity_empty(&target) && !identity_valid(&target)) ||
        (receipt[104] == 'R' && (!identity_valid(&reaper) || !identity_valid(&target))))) return 64;
    /* Stop the exact old writer before rereading P/R. This fences a racing
     * gate release and lets its child observe EOF if exec was not authorized. */
    if (valid_crc && !identity_empty(&reaper) && settle_identity(&reaper)) return 75;
    double lock_until = now() + 1.0;
    while (flock(fd, LOCK_EX | LOCK_NB)) {
        if ((errno != EWOULDBLOCK && errno != EAGAIN && errno != EINTR) || now() >= lock_until) return 75;
        (void)poll(NULL, 0, 10);
    }
    char previous[104];
    memcpy(previous, receipt, sizeof(previous));
    if (receipt_capability(fd, 0) || !receipt_v2 || memcmp(previous, receipt, sizeof(previous))) {
        (void)flock(fd, LOCK_UN); return 64;
    }
    valid_crc = receipt_crc_valid();
    if (receipt[104] == 'C') { (void)flock(fd, LOCK_UN); return valid_crc ? 0 : 64; }
    if (!valid_crc) {
        int result = receipt[104] == 'P' ? complete_unexecuted(fd) : -1;
        (void)flock(fd, LOCK_UN);
        return result ? 64 : 0;
    }
    decode_identity((const unsigned char *)receipt + 149, &reaper);
    decode_identity((const unsigned char *)receipt + 105, &target);
    if ((!identity_empty(&reaper) && (!identity_valid(&reaper) || settle_identity(&reaper))) ||
        (!identity_empty(&target) && (!identity_valid(&target) || settle_identity(&target))) ||
        (receipt[104] == 'R' && (!identity_valid(&reaper) || !identity_valid(&target)))) {
        (void)flock(fd, LOCK_UN); return 75;
    }
    /* P cannot execute the target: only a durable R frame opens its private
     * gate. A missing reaper identity is safe under this lock; any delayed
     * initial writer must acquire the lock and reject the now-complete frame. */
    int result = durable_write(fd, "C", 1, 104);
    (void)flock(fd, LOCK_UN);
    return result ? 74 : 0;
}
static int supervise_group_owner(pid_t child, rlim_t descriptor_limit) {
    for (int fd = 0; fd < (int)descriptor_limit; ++fd) close(fd);
    int requested = 0;
    for (;;) {
        int status;
        pid_t result = waitpid(child, &status, WNOHANG);
        if (result == child) return WIFEXITED(status) ? WEXITSTATUS(status) : 70;
        if (result < 0 && errno != EINTR) return 70;
        if (!parent_alive() && !requested) {
            /* This is our held child, never a PID recovered from a journal. */
            (void)kill(child, SIGTERM);
            requested = 1;
        }
        (void)poll(NULL, 0, 20);
    }
}
int main(int argc, char **argv) {
    if (argc == 3 && !strcmp(argv[1], "--recover-receipt")) {
        char *end;
        errno = 0;
        long supplied = strtol(argv[2], &end, 10);
        if (errno || *end || supplied < 3 || supplied > INT_MAX) return 64;
        return recover_receipt((int)supplied);
    }
    if (argc != 4 && argc != 5) return 64;
    int spec = atoi(argv[1]);
    control = atoi(argv[2]); owner = (pid_t)atoi(argv[3]);
    if (spec < 3 || control < 3 || spec == control || owner <= 1) return 64;
    signal(SIGPIPE, SIG_IGN);
    signal(SIGTERM, stop_signal); signal(SIGINT, stop_signal);
    if (argc == 5) {
        int supplied = atoi(argv[4]);
        if (supplied < 3 || supplied == spec || supplied == control ||
            flock(supplied, LOCK_EX) || receipt_capability(supplied, 1)) return 64;
        completion = supplied;
        if (receipt_group) {
            if (!parent_alive()) goto invalid;
            struct rlimit descriptors;
            if (getrlimit(RLIMIT_NOFILE, &descriptors) || descriptors.rlim_cur > INT_MAX) goto invalid;
            pid_t outer = getpid();
            pid_t inner = fork();
            if (inner < 0) goto invalid;
            if (inner) return supervise_group_owner(inner, descriptors.rlim_cur);
            /* Capture the expected outer before fork. getppid() here could
             * already be launchd if the outer wrapper was abruptly killed. */
            owner = outer;
        }
        if (receipt_v2 && persist_identity(getpid(), 149)) goto invalid;
        if (!receipt_v2) (void)flock(completion, LOCK_UN);
    }
    char magic[8];
    int owned_group = 0;
    uint32_t grace, input, output, extra_count, arg_count, env_count;
    if (read_exact(spec, magic, 8)) goto invalid;
    owned_group = !memcmp(magic, "MCPME003", 8) || !memcmp(magic, "MCPME004", 8);
    if (receipt_v2 && owned_group != receipt_group) goto invalid;
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
    int execution_gate[2] = {-1, -1};
    if (receipt_v2 && pipe(execution_gate)) goto invalid;
    struct rlimit limit;
    if (getrlimit(RLIMIT_NOFILE, &limit) || limit.rlim_cur > INT_MAX) goto invalid;
    pid_t child = fork();
    if (child < 0) goto invalid;
    if (!child) {
        close(exec_status[0]);
        signal(SIGTERM, SIG_DFL); signal(SIGINT, SIG_DFL); signal(SIGPIPE, SIG_DFL);
        if (receipt_v2) {
            close(execution_gate[1]);
            close(completion);
            char permission;
            ssize_t count;
            do { count = read(execution_gate[0], &permission, 1); } while (count < 0 && errno == EINTR);
            close(execution_gate[0]);
            if (count != 1 || permission != 'E') { errno = ECANCELED; goto exec_failed; }
        }
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
    if (receipt_v2) {
        close(execution_gate[0]);
        /* The child cannot execute package code before both its lifetime and
         * the R transition are durable. Parent death or a failed write closes
         * the gate without authorization, and the held child is reaped below. */
        if (!parent_alive() || persist_identity(child, 105) ||
            durable_write(completion, "R", 1, 104) || !parent_alive()) stopping = 1;
        else if (write(execution_gate[1], "E", 1) != 1) stopping = 1;
        close(execution_gate[1]);
        (void)flock(completion, LOCK_UN);
    }
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
