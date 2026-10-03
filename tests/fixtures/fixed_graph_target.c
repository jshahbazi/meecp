#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
int main(void) {
    alarm(2);
    volatile unsigned char *bytes = malloc(2 * 1024 * 1024);
    if (!bytes) return 20;
    for (size_t i = 0; i < 2 * 1024 * 1024; i += 4096) bytes[i] = 1;
    puts("ready"); fflush(stdout);
    char input;
    (void)read(0, &input, 1);
    free((void *)bytes);
    return 0;
}
