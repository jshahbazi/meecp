/* Windowed observations only. No telemetry-derived PID is a signal target. */
#include <mach/mach_time.h>
#include <sys/proc.h>
#include <sys/proc_info.h>
#include <sys/wait.h>

#define RESOURCE_POLICY_FIELDS 8
#define RESOURCE_REPORT_FIELDS 16
#define RESOURCE_REPORT_SIZE (RESOURCE_REPORT_FIELDS * 8)
#define RESOURCE_MEMBERS 64
#define RESOURCE_IDENTITIES 1024
enum { RR_EXITED, RR_FOOTPRINT, RR_PROCESSES, RR_CPU, RR_MEMBERSHIP, RR_METRIC,
    RR_IDENTITY, RR_OVERFLOW, RR_WINDOW, RR_GAP, RR_IDENTITIES, RR_SAMPLES,
    RR_CONTROL, RR_DEADLINE, RR_POLICY, RR_NO_SAMPLE };
struct resource_policy {
    uint64_t footprint, members, cpu, interval, window, gap, identities, samples;
};
struct resource_report {
    uint64_t reason, valid, samples, attempts, group_footprint, helper_footprint,
        scope_footprint, members, cpu, identities, window, gap, numer, denom,
        lifetime_complete, hard_enforced;
};
struct resource_identity { pid_t pid; uint64_t birth, cpu; };
struct resource_monitor {
    struct resource_policy policy;
    struct resource_report report;
    struct resource_identity identities[RESOURCE_IDENTITIES];
    uint64_t last_begin;
};
static uint64_t resource_wire64(uint64_t value) {
    return ((uint64_t)htonl((uint32_t)value) << 32) | htonl((uint32_t)(value >> 32));
}
static int resource_add(uint64_t left, uint64_t right, uint64_t *out) {
    if (UINT64_MAX - left < right) { *out = UINT64_MAX; return -1; }
    *out = left + right; return 0;
}
static int resource_ns(uint64_t ticks, uint64_t numer, uint64_t denom, uint64_t *out) {
    if (!numer || !denom) return -1;
    __uint128_t wide = ((__uint128_t)ticks * numer + denom - 1) / denom;
    if (wide > UINT64_MAX) { *out = UINT64_MAX; return -1; }
    *out = (uint64_t)wide; return 0;
}
static int resource_clock(struct resource_monitor *m, uint64_t *out) {
    return resource_ns(mach_absolute_time(), m->report.numer, m->report.denom, out);
}
static int resource_fail(struct resource_monitor *m, unsigned reason) {
    if (!m->report.reason) m->report.reason = reason;
    m->report.valid = 0;
    return -1;
}
static int resource_init(struct resource_monitor *m) {
    const struct resource_policy *p = &m->policy;
    m->report.reason = RR_POLICY;
    if (!p->footprint || p->footprint > UINT64_C(4294967296) || !p->members || p->members > RESOURCE_MEMBERS ||
        !p->cpu || p->cpu > UINT64_C(3600000000000) || p->interval < 1000000 || p->interval > 100000000 ||
        !p->window || p->window > 100000000 || p->gap < p->interval || p->gap > 1000000000 ||
        p->identities < p->members + 1 || p->identities > RESOURCE_IDENTITIES || !p->samples || p->samples > 1000000) return -1;
    mach_timebase_info_data_t timebase;
    if (mach_timebase_info(&timebase) != KERN_SUCCESS || !timebase.numer || !timebase.denom) return -1;
    m->report.numer = timebase.numer; m->report.denom = timebase.denom;
    m->report.reason = RR_EXITED; m->report.valid = 1;
    return 0;
}
static int resource_pid_compare(const void *left, const void *right) {
    pid_t a = *(const pid_t *)left, b = *(const pid_t *)right;
    return (a > b) - (a < b);
}
static int resource_members(pid_t leader, pid_t *members) {
    int count = proc_listpgrppids(leader, members, sizeof(pid_t) * (RESOURCE_MEMBERS + 1));
    if (count < 1 || count > RESOURCE_MEMBERS + 1) return -1;
    qsort(members, (size_t)count, sizeof(pid_t), resource_pid_compare);
    int found = 0;
    for (int i = 0; i < count; ++i) {
        if (members[i] <= 1 || (i && members[i] == members[i - 1])) return -1;
        if (members[i] == leader) found = 1;
    }
    return found ? count : -1;
}
static int resource_observe(struct resource_monitor *m, pid_t pid, pid_t group, int held_exit, uint64_t *footprint) {
    struct proc_bsdinfo before, after;
    struct rusage_info_v1 first, second;
    memset(&before, 0, sizeof(before)); memset(&after, 0, sizeof(after));
    memset(&first, 0, sizeof(first)); memset(&second, 0, sizeof(second));
    if ((!held_exit && proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &before, sizeof(before)) != sizeof(before)) ||
        proc_pid_rusage(pid, RUSAGE_INFO_V1, (rusage_info_t *)&first) ||
        proc_pid_rusage(pid, RUSAGE_INFO_V1, (rusage_info_t *)&second) ||
        (!held_exit && proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &after, sizeof(after)) != sizeof(after))) return RR_METRIC;
    if (!held_exit && (before.pbi_pid != (uint32_t)pid || after.pbi_pid != (uint32_t)pid ||
        before.pbi_pgid != (uint32_t)group || after.pbi_pgid != (uint32_t)group ||
        before.pbi_uid != getuid() || after.pbi_uid != getuid() ||
        before.pbi_start_tvsec != after.pbi_start_tvsec || before.pbi_start_tvusec != after.pbi_start_tvusec ||
        ((before.pbi_status == SZOMB) != (after.pbi_status == SZOMB)))) return RR_IDENTITY;
    if (
        !first.ri_proc_start_abstime || first.ri_proc_start_abstime != second.ri_proc_start_abstime ||
        first.ri_proc_exit_abstime != second.ri_proc_exit_abstime ||
        first.ri_user_time > second.ri_user_time || first.ri_system_time > second.ri_system_time) return RR_IDENTITY;
    uint64_t ticks, cpu;
    if (resource_add(second.ri_user_time, second.ri_system_time, &ticks) ||
        resource_ns(ticks, m->report.numer, m->report.denom, &cpu)) return RR_OVERFLOW;
    uint64_t index = 0;
    while (index < m->report.identities && (m->identities[index].pid != pid ||
            m->identities[index].birth != second.ri_proc_start_abstime)) ++index;
    /* Only the original unreaped leader has positive waitid(WNOWAIT) identity.
     * Darwin's BSD-info call rejects zombies; cached usage is still available.
     * No unknown or merely enumerated descendant gets this exception. */
    if (held_exit && (pid != group || !second.ri_proc_exit_abstime || index == m->report.identities)) return RR_IDENTITY;
    if (index == m->report.identities) {
        if (index >= m->policy.identities) return RR_IDENTITIES;
        m->identities[index].pid = pid; m->identities[index].birth = second.ri_proc_start_abstime;
        ++m->report.identities;
    }
    if (cpu < m->identities[index].cpu) return RR_IDENTITY;
    if (resource_add(m->report.cpu, cpu - m->identities[index].cpu, &m->report.cpu)) return RR_OVERFLOW;
    m->identities[index].cpu = cpu;
    /* Zombie caches hold final counters, not current live physical allocation. */
    *footprint = (after.pbi_status == SZOMB || second.ri_proc_exit_abstime) ? 0 :
        (first.ri_phys_footprint > second.ri_phys_footprint ? first.ri_phys_footprint : second.ri_phys_footprint);
    return 0;
}
static int resource_sample(struct resource_monitor *m, pid_t leader, int leader_exited) {
    uint64_t begin, end, sum = 0, helper = 0, scope;
    if (resource_clock(m, &begin)) return resource_fail(m, RR_OVERFLOW);
    ++m->report.attempts;
    if (m->report.attempts > m->policy.samples) return resource_fail(m, RR_SAMPLES);
    if (m->last_begin) {
        if (begin < m->last_begin) return resource_fail(m, RR_OVERFLOW);
        uint64_t gap = begin - m->last_begin;
        if (gap > m->report.gap) m->report.gap = gap;
        if (gap > m->policy.gap) return resource_fail(m, RR_GAP);
    }
    m->last_begin = begin;
    pid_t first[RESOURCE_MEMBERS + 1] = {0}, second[RESOURCE_MEMBERS + 1] = {0};
    int count = resource_members(leader, first);
    if (count < 0) return resource_fail(m, RR_METRIC);
    if ((uint64_t)count > m->report.members) m->report.members = (uint64_t)count;
    if ((uint64_t)count > m->policy.members) return resource_fail(m, RR_PROCESSES);
    for (int i = 0; i < count; ++i) {
        uint64_t footprint = 0;
        int result = resource_observe(m, first[i], leader, leader_exited && first[i] == leader, &footprint);
        /* The original direct child can exit between outer waitid and its
         * live BSD reads. Re-read only that held identity with a fresh exit
         * proof. resource_observe still requires an already observed birth
         * and stable nonzero terminal timestamp. Descendants never qualify. */
        if ((result == RR_METRIC || result == RR_IDENTITY) && first[i] == leader && !leader_exited) {
            siginfo_t terminal;
            /* BSD visibility may disappear just before waitid becomes ready.
             * Wait only within this sample's existing read-window budget. */
            for (;;) {
                uint64_t current;
                if (resource_clock(m, &current) || current < begin || current - begin >= m->policy.window) break;
                memset(&terminal, 0, sizeof(terminal));
                if (waitid(P_PID, (id_t)leader, &terminal, WEXITED | WNOHANG | WNOWAIT)) break;
                if (terminal.si_pid == leader) {
                    result = resource_observe(m, leader, leader, 1, &footprint);
                    break;
                }
                usleep(100);
            }
        }
        if (result) return resource_fail(m, (unsigned)result);
        if (resource_add(sum, footprint, &sum)) return resource_fail(m, RR_OVERFLOW);
    }
    int result = resource_observe(m, getpid(), getpgrp(), 0, &helper);
    if (result) return resource_fail(m, (unsigned)result);
    int after = resource_members(leader, second);
    if (after != count || memcmp(first, second, (size_t)count * sizeof(pid_t))) return resource_fail(m, RR_MEMBERSHIP);
    if (resource_clock(m, &end) || end < begin || resource_add(sum, helper, &scope)) return resource_fail(m, RR_OVERFLOW);
    uint64_t window = end - begin;
    if (window > m->report.window) m->report.window = window;
    if (window > m->policy.window) return resource_fail(m, RR_WINDOW);
    ++m->report.samples;
    if (sum > m->report.group_footprint) m->report.group_footprint = sum;
    if (helper > m->report.helper_footprint) m->report.helper_footprint = helper;
    if (scope > m->report.scope_footprint) m->report.scope_footprint = scope;
    if (scope > m->policy.footprint) { m->report.reason = RR_FOOTPRINT; return -1; }
    if (m->report.cpu > m->policy.cpu) { m->report.reason = RR_CPU; return -1; }
    return 0;
}
static void resource_encode(const struct resource_monitor *m, void *destination) {
    const struct resource_report *r = &m->report;
    uint64_t values[RESOURCE_REPORT_FIELDS] = {r->reason, r->valid, r->samples, r->attempts,
        r->group_footprint, r->helper_footprint, r->scope_footprint, r->members, r->cpu, r->identities,
        r->window, r->gap, r->numer, r->denom, 0, 0};
    for (int i = 0; i < RESOURCE_REPORT_FIELDS; ++i) values[i] = resource_wire64(values[i]);
    memcpy(destination, values, sizeof(values));
}
