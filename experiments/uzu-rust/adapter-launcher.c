/* Relocatable exec-only launcher; the host retains the adapter process PID. */
#include <mach-o/dyld.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <stdio.h>

int main(int argc, char **argv) {
    char raw[PATH_MAX], root[PATH_MAX], python[PATH_MAX], script[PATH_MAX];
    uint32_t length = sizeof(raw);
    if (_NSGetExecutablePath(raw, &length) || !realpath(raw, root)) return 2;
    char *end = strrchr(root, '/');
    if (!end) return 2;
    *end = 0;
    end = strrchr(root, '/');
    if (!end) return 2;
    *end = 0;
    if (snprintf(python, sizeof(python), "%s/python/bin/python3.12", root) >= sizeof(python)
        || snprintf(script, sizeof(script), "%s/adapter.py", root) >= sizeof(script)) return 2;
    char **arguments = calloc((size_t)argc + 5, sizeof(char *));
    if (!arguments) return 2;
    arguments[0] = python;
    arguments[1] = "-I";
    arguments[2] = "-B";
    arguments[3] = script;
    for (int i = 1; i < argc; i++) arguments[i + 3] = argv[i];
    setenv("RUST_LOG", "off", 1);
    execv(python, arguments);
    return 2;
}
