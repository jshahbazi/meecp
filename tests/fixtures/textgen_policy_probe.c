/* Small owned policy control. Emits only numeric denial/success counts. */
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <poll.h>
#include <string.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

static int denied_open(const char *path, int flags) {
    int fd = open(path, flags, 0600);
    if (fd >= 0) { close(fd); return 0; }
    return errno == EPERM || errno == EACCES;
}
static int connect_port(int port, int type) {
    int fd = socket(AF_INET, type, 0);
    struct sockaddr_in address = {.sin_family=AF_INET, .sin_port=htons((unsigned short)port)};
    inet_pton(AF_INET,"127.0.0.1",&address.sin_addr);
    int result = connect(fd,(struct sockaddr*)&address,sizeof(address));
    close(fd); return result;
}
static int bind_port(int port, int wildcard) {
    int fd=socket(AF_INET,SOCK_STREAM,0);
    struct sockaddr_in address={.sin_family=AF_INET,.sin_port=htons((unsigned short)port)};
    address.sin_addr.s_addr=htonl(wildcard ? INADDR_ANY : INADDR_LOOPBACK);
    int result=bind(fd,(struct sockaddr*)&address,sizeof(address));
    if(result==0) result=listen(fd,1);
    close(fd); return result;
}
int main(int argc, char **argv) {
    if (argc == 4 && strcmp(argv[1],"inbound") == 0) {
        alarm(3);
        int fd=socket(AF_INET,SOCK_STREAM,0);
        struct sockaddr_in address={.sin_family=AF_INET,.sin_port=htons((unsigned short)atoi(argv[2]))};
        address.sin_addr.s_addr=htonl(atoi(argv[3]) ? INADDR_ANY : INADDR_LOOPBACK);
        if(bind(fd,(struct sockaddr*)&address,sizeof(address)) || listen(fd,1)) return 4;
        printf("ready\n");fflush(stdout);
        struct pollfd query={.fd=fd,.events=POLLIN};
        int accepted=0;
        if(poll(&query,1,1000)>0) {
            int child=accept(fd,NULL,NULL);
            accepted=child>=0;
            if(child>=0)close(child);
        }
        close(fd);printf("%d\n",accepted);return 0;
    }
    if (argc != 10) return 2;
    alarm(3);
    int denied=0, allowed=0, missing=0, index=0;
    { int ok=(denied_open(argv[1], O_RDONLY)); denied+=ok; missing|=(!ok << index++); }
    { int ok=(denied_open(argv[2], O_WRONLY|O_TRUNC)); denied+=ok; missing|=(!ok << index++); }
    { int ok=(denied_open(argv[9], O_RDONLY)); denied+=ok; missing|=(!ok << index++); }
    struct stat details;
    { int ok=(stat(argv[1], &details) < 0 && (errno == EPERM || errno == EACCES)); denied+=ok; missing|=(!ok << index++); }
    { int ok=(connect_port(atoi(argv[3]),SOCK_STREAM) < 0); denied+=ok; missing|=(!ok << index++); }
    { int ok=(connect_port(atoi(argv[3]),SOCK_DGRAM) < 0); denied+=ok; missing|=(!ok << index++); }
    int fd=socket(AF_UNIX,SOCK_STREAM,0);
    struct sockaddr_un unix_address={.sun_family=AF_UNIX};
    snprintf(unix_address.sun_path,sizeof(unix_address.sun_path),"%s",argv[4]);
    { int ok=(connect(fd,(struct sockaddr*)&unix_address,sizeof(unix_address)) < 0); denied+=ok; missing|=(!ok << index++); }
    close(fd);
    { int ok=(setsid() < 0 && errno == EPERM); denied+=ok; missing|=(!ok << index++); }
    { int ok=(setpgid(0,0) < 0 && errno == EPERM); denied+=ok; missing|=(!ok << index++); }
    int child=fork();
    if (child == 0) _exit(9);
    { int ok=(child < 0 && errno == EPERM); denied+=ok; missing|=(!ok << index++); }
    if (child > 0) waitpid(child,NULL,0);
    { int ok=(connect_port(atoi(argv[5]),SOCK_DGRAM) < 0); denied+=ok; missing|=(!ok << index++); }
    fd=socket(AF_INET6,SOCK_STREAM,0);
    struct sockaddr_in6 ipv6={.sin6_family=AF_INET6,.sin6_port=htons((unsigned short)atoi(argv[5]))};
    inet_pton(AF_INET6,"::1",&ipv6.sin6_addr);
    { int ok=(connect(fd,(struct sockaddr*)&ipv6,sizeof(ipv6)) < 0); denied+=ok; missing|=(!ok << index++); }
    close(fd);
    { int ok=(bind_port(0,0) < 0); denied+=ok; missing|=(!ok << index++); }
    /* Seatbelt localhost bind filtering permits ANY at the fixed port.
       The immutable production bootstrap independently chooses loopback. */
    allowed += bind_port(atoi(argv[7]),1) == 0;
    allowed += bind_port(atoi(argv[7]),0) == 0;
    int connected=connect_port(atoi(argv[5]),SOCK_STREAM)==0;
    allowed += connected == atoi(argv[8]);
    fd=open(argv[6],O_WRONLY|O_CREAT|O_EXCL,0600);
    allowed += fd >= 0;
    if(fd>=0)close(fd);
    printf("%d %d %d\n",denied,allowed,missing);
    return denied == 13 && allowed == 4 && missing == 0 ? 0 : 3;
}
