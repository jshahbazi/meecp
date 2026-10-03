/* Demonstrate the unconfined group-escape boundary, then exit by a held pipe
 * capability. The harness never signals an adopted/discovered target PID. */
#define _DARWIN_C_SOURCE 1
#include <stdio.h>
#include <unistd.h>

int main(void) {
    int ready[2];
    if (pipe(ready)) return 64;
    pid_t child = fork();
    if (child < 0) return 64;
    if (!child) {
        close(ready[0]);
        if (setsid() < 0) return 71;
        close(STDOUT_FILENO); close(STDERR_FILENO);
        if (write(ready[1], "R", 1) != 1) return 71;
        close(ready[1]);
        char stop;
        while (read(STDIN_FILENO, &stop, 1) > 0) if (stop == 'X') break;
        return 0;
    }
    close(ready[1]);
    char ack;
    if (read(ready[0], &ack, 1) != 1 || ack != 'R') return 71;
    close(ready[0]);
    printf("{\"escaped_child\":%d}\n", (int)child);
    fflush(stdout);
    return 0;
}
