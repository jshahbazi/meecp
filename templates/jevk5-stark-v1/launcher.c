/* Fixed relocatable entrypoints; exec preserves the host-owned child PID. */
#include <mach-o/dyld.h>
#include <limits.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <unistd.h>

int main(int argc, char **argv) {
    char path[PATH_MAX], root[PATH_MAX], python[PATH_MAX], search[PATH_MAX * 3];
    uint32_t length = sizeof(path);
    if (argc != 3 || strcmp(argv[1], "--meecp-config-fd") != 0) return 2;
    if (!argv[2][0] || strlen(argv[2]) > 10 || strspn(argv[2], "0123456789") != strlen(argv[2])) return 2;
    char *end = NULL;
    long descriptor = strtol(argv[2], &end, 10);
    if (!end || *end || descriptor < 3 || descriptor > INT_MAX) return 2;
    if (_NSGetExecutablePath(path, &length) != 0 || !realpath(path, root)) return 2;
    char *name = strrchr(root, '/');
    if (!name) return 2;
    const char *module = NULL;
    const char *role = NULL;
    if (strcmp(name + 1, "jevk5-stark-engine") == 0) {
        module = "meecp_stark"; role = "engine";
    } else if (strcmp(name + 1, "jevk5-stark-adapter") == 0) {
        module = "meecp_jevk5"; role = "adapter";
    } else return 2;
    *name = '\0';
    char *parent = strrchr(root, '/');
    if (!parent) return 2;
    *parent = '\0';
    if (snprintf(python, sizeof(python), "%s/python/bin/python3.12", root) >= (int)sizeof(python)) return 2;
    if (snprintf(search, sizeof(search), "%s/backend:%s/vendor:%s/python/site-packages", root, root, root) >= (int)sizeof(search)) return 2;
    unsetenv("PYTHONHOME"); unsetenv("PYTHONUSERBASE"); unsetenv("PYTHONSTARTUP"); unsetenv("PYTHONINSPECT");
    if (setenv("PYTHONPATH", search, 1) || setenv("PYTHONNOUSERSITE", "1", 1) || setenv("PYTHONDONTWRITEBYTECODE", "1", 1)) return 2;
    char *args[] = {python, "-P", "-m", (char *)module, (char *)role,
                    "--meecp-config-fd", argv[2], NULL};
    execv(python, args);
    return 2;
}
