/* Relocatable package entrypoint. Every exec preserves the host-owned PID. */
#include <mach-o/dyld.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <unistd.h>

int main(int argc, char **argv) {
    char path[PATH_MAX], real[PATH_MAX], python[PATH_MAX], search[PATH_MAX * 3];
    uint32_t length = sizeof(path);
    if (_NSGetExecutablePath(path, &length) != 0 || !realpath(path, real)) return 2;
    char *name = strrchr(real, '/');
    if (!name) return 2;
    const char *role = strcmp(name + 1, "jevk5-engine") == 0 ? "engine" : "adapter";
    *name = '\0';
    char *parent = strrchr(real, '/');
    if (!parent) return 2;
    *parent = '\0';
    if (snprintf(python, sizeof(python), "%s/python/bin/python3.12", real) >= (int)sizeof(python)) return 2;
    if (snprintf(search, sizeof(search), "%s/backend:%s/vendor:%s/python/site-packages", real, real, real) >= (int)sizeof(search)) return 2;
    unsetenv("PYTHONHOME"); unsetenv("PYTHONUSERBASE"); unsetenv("PYTHONSTARTUP"); unsetenv("PYTHONINSPECT");
    setenv("PYTHONPATH", search, 1); setenv("PYTHONNOUSERSITE", "1", 1);
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);
    char **args = calloc((size_t)argc + 5, sizeof(char *));
    if (!args) return 2;
    args[0] = python; args[1] = "-m"; args[2] = "mcpme_jevk5"; args[3] = (char *)role;
    for (int i = 1; i < argc; i++) args[i + 3] = argv[i];
    execv(python, args);
    return 2;
}
