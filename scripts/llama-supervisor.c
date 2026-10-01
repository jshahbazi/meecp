/* Own the inference child and reap it when the backend dies, including SIGKILL.
 * No request data, arguments, or runtime output are logged. */
#include <errno.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t stopping = 0;
static void stop_signal(int signal_number) { (void)signal_number; stopping = 1; }
static double seconds(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return now.tv_sec + now.tv_nsec / 1e9;
}

int main(int argc, char **argv) {
    (void)argc;
    pid_t parent = getppid();
    if (parent <= 1) return 70;
    char executable[PATH_MAX];
    if (snprintf(executable, sizeof(executable), "%s.real", argv[0]) >= (int)sizeof(executable)) return 70;
    struct sigaction action = {0};
    action.sa_handler = stop_signal;
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGINT, &action, NULL);
    sigaction(SIGHUP, &action, NULL);
    pid_t child = fork();
    if (child < 0) return 70;
    if (child == 0) {
        signal(SIGTERM, SIG_DFL);
        signal(SIGINT, SIG_DFL);
        signal(SIGHUP, SIG_DFL);
        argv[0] = executable;
        execv(executable, argv);
        _exit(70);
    }
    double deadline = 0;
    while (1) {
        int status = 0;
        pid_t finished = waitpid(child, &status, WNOHANG);
        if (finished == child) return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
        if (finished < 0 && errno != EINTR) return 70;
        if (stopping || getppid() != parent) {
            if (deadline == 0) {
                kill(child, SIGTERM);
                deadline = seconds() + 1;
            } else if (seconds() >= deadline) {
                kill(child, SIGKILL);
                while (waitpid(child, &status, 0) < 0 && errno == EINTR) {}
                return 0;
            }
        }
        struct timespec interval = {0, 100000000};
        nanosleep(&interval, NULL);
    }
}
