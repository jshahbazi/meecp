/* The held foreground child installs a macOS no-fork profile, then execs
 * exactly one package executable without a shell. No payload diagnostics. */
#define _DARWIN_C_SOURCE 1
#include <errno.h>
#include <limits.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#ifdef __APPLE__
#include <sandbox.h>
#endif

extern char **environ;

static char *quoted(const char *value) {
    size_t size = strlen(value);
    if (size > PATH_MAX) return NULL;
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
    result[offset++] = '"';
    return result;
}

int main(int argc, char **argv) {
    if (argc < 3 || argc > 132) return 64;
#ifndef __APPLE__
    (void)argv;
    return 78;
#else
    char root[PATH_MAX], executable[PATH_MAX];
    if (!realpath(argv[1], root) || !realpath(argv[2], executable)) return 64;
    size_t root_size = strlen(root);
    if (strncmp(root, executable, root_size) || executable[root_size] != '/') return 64;
    char *root_literal = quoted(root), *executable_literal = quoted(executable);
    if (!root_literal || !executable_literal) return 64;
    const char *format =
        "(version 1) (deny default (with no-log)) "
        "(allow sysctl-read) (allow file-read-metadata) "
        "(allow file-read* (literal \"/\") (subpath \"/System\") "
        "(subpath \"/usr/lib\") (subpath \"/usr/share\") "
        "(subpath \"/private/var/db/dyld\") (literal \"/dev/null\") "
        "(literal \"/dev/urandom\") (literal \"/dev/random\") (subpath %s)) "
        "(allow file-write* (literal \"/dev/null\")) "
        "(allow process-exec (literal %s))";
    int size = snprintf(NULL, 0, format, root_literal, executable_literal);
    if (size < 0 || size > PATH_MAX * 4 + 2048) return 64;
    char *profile = calloc((size_t)size + 1, 1);
    if (!profile) return 70;
    (void)snprintf(profile, (size_t)size + 1, format, root_literal, executable_literal);
    char *error = NULL;
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
    int failure = sandbox_init(profile, 0, &error);
    if (error) sandbox_free_error(error);
#pragma clang diagnostic pop
    free(profile); free(root_literal); free(executable_literal);
    if (failure) return 78;
    execve(executable, &argv[2], environ);
    return errno == ENOENT ? 127 : 126;
#endif
}
