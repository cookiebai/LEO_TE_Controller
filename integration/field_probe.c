/* Static IPv6 UDP probe with atomic live snapshots, for a single continuous
 * offered load across baseline -> algorithm -> TE. No Python needed in nodes. */
#define _POSIX_C_SOURCE 200809L
#include <arpa/inet.h>
#include <errno.h>
#include <math.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t stop_requested;
static void on_signal(int signum) { (void)signum; stop_requested = 1; }
static double wall_now(void) {
    struct timespec t; clock_gettime(CLOCK_REALTIME, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}
static void wait_until(double when) {
    while (!stop_requested) {
        double remain = when - wall_now();
        if (remain <= 0) break;
        if (remain > .05) remain = .05;
        struct timespec t = {(time_t)remain, (long)((remain-(time_t)remain)*1e9)};
        nanosleep(&t, NULL);
    }
}
static void die(const char *what) { perror(what); exit(1); }
static void snapshot(const char *output, const char *role, double start,
                     int duration, int finished, const uint64_t *bytes,
                     const uint64_t *packets, const uint64_t *errors) {
    char temporary[4096];
    if (snprintf(temporary, sizeof temporary, "%s.tmp", output) >= (int)sizeof temporary) {
        fprintf(stderr, "output path too long\n"); exit(2);
    }
    FILE *f = fopen(temporary, "w"); if (!f) die("fopen");
    double sampled = wall_now();
    int complete = (int)floor(sampled - start);
    if (complete < 0) complete = 0;
    if (complete > duration) complete = duration;
    uint64_t error_sum = 0;
    for (int i=0; i<duration; ++i) error_sum += errors[i];
    fprintf(f, "{\"role\":\"%s\",\"start_at\":%.6f,\"sampled_at\":%.6f,"
            "\"duration\":%d,\"completed_seconds\":%d,\"finished\":%s,\"send_errors\":%llu,",
            role, start, sampled, duration, complete, finished ? "true":"false",
            (unsigned long long)error_sum);
    const char *keys[] = {"bytes_per_second", "packets_per_second", "errors_per_second"};
    const uint64_t *arrays[] = {bytes, packets, errors};
    for (int k=0; k<3; ++k) {
        fprintf(f, "\"%s\":[", keys[k]);
        for (int i=0; i<duration; ++i)
            fprintf(f, "%s%llu", i ? ",":"", (unsigned long long)arrays[k][i]);
        fprintf(f, "]%s", k<2 ? ",":"");
    }
    fprintf(f, "}\n");
    if (fclose(f)) die("fclose");
    if (rename(temporary, output)) die("rename");
}
int main(int argc, char **argv) {
    if (argc<2 || (strcmp(argv[1],"server") && strcmp(argv[1],"client"))) return 2;
    int server=!strcmp(argv[1],"server"), duration=0, port=0, source_port=0, payload=1000;
    double start=0, rate=0;
    const char *dst=NULL, *output=NULL;
    for (int i=2;i<argc;i+=2) {
        if (i+1>=argc) return 2;
        const char *key=argv[i], *v=argv[i+1];
        if (!strcmp(key,"--port")) port=atoi(v);
        else if (!strcmp(key,"--source-port")) source_port=atoi(v);
        else if (!strcmp(key,"--duration")) duration=atoi(v);
        else if (!strcmp(key,"--start-at")) start=atof(v);
        else if (!strcmp(key,"--rate-kbps")) rate=atof(v);
        else if (!strcmp(key,"--payload")) payload=atoi(v);
        else if (!strcmp(key,"--dst")) dst=v;
        else if (!strcmp(key,"--output")) output=v;
        else return 2;
    }
    if (duration<1 || duration>3600 || port<1024 || port>65535 || !output ||
        start<=0 || source_port<0 || source_port>65535 ||
        (!server && (!dst || rate<=0 || payload<1 || payload>1000))) return 2;
    signal(SIGTERM,on_signal); signal(SIGINT,on_signal);
    uint64_t *bytes=calloc(duration,sizeof(uint64_t)), *packets=calloc(duration,sizeof(uint64_t)),
             *errors=calloc(duration,sizeof(uint64_t));
    if (!bytes || !packets || !errors) die("calloc");
    int fd=socket(AF_INET6,SOCK_DGRAM,0); if(fd<0) die("socket");
    int buffer=4*1024*1024, yes=1;
    setsockopt(fd,SOL_SOCKET,server ? SO_RCVBUF:SO_SNDBUF,&buffer,sizeof buffer);
    setsockopt(fd,IPPROTO_IPV6,IPV6_V6ONLY,&yes,sizeof yes);
    struct sockaddr_in6 local={.sin6_family=AF_INET6,.sin6_addr=IN6ADDR_ANY_INIT,
                              .sin6_port=htons(server ? port:source_port)};
    if(bind(fd,(struct sockaddr *)&local,sizeof local)) die("bind");
    snapshot(output,argv[1],start,duration,0,bytes,packets,errors);
    double next_snapshot=start+1;
    char data[65536]={0};
    if(server) {
        struct timeval timeout={.tv_sec=0,.tv_usec=100000};
        setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof timeout);
        wait_until(start);
        while(!stop_requested && wall_now()<start+duration) {
            ssize_t n=recv(fd,data,sizeof data,0);
            if(n<0 && errno!=EAGAIN && errno!=EWOULDBLOCK && errno!=EINTR) die("recv");
            double now=wall_now(); int idx=(int)floor(now-start);
            if(n>=0 && idx>=0 && idx<duration) {bytes[idx]+=n;packets[idx]++;}
            if(now>=next_snapshot) {
                snapshot(output,argv[1],start,duration,0,bytes,packets,errors);
                next_snapshot=floor(now-start)+start+1;
            }
        }
    } else {
        struct sockaddr_in6 remote={.sin6_family=AF_INET6,.sin6_port=htons(port)};
        if(inet_pton(AF_INET6,dst,&remote.sin6_addr)!=1) return 2;
        double pps=rate*1000/(8*payload);
        uint64_t attempted=0, target=(uint64_t)llround(pps*duration);
        int batch=(int)llround(pps/100); if(batch<1) batch=1; if(batch>16) batch=16;
        while(!stop_requested && attempted<target) {
            wait_until(start+attempted/pps);
            if(stop_requested || wall_now()>=start+duration) break;
            for(int i=0;i<batch && attempted<target;++i,++attempted) {
                ssize_t n=sendto(fd,data,payload,MSG_DONTWAIT,(struct sockaddr *)&remote,sizeof remote);
                double now=wall_now(); int idx=(int)floor(now-start);
                if(idx>=0 && idx<duration) {
                    if(n==payload) {bytes[idx]+=n;packets[idx]++;} else errors[idx]++;
                }
            }
            if(wall_now()>=next_snapshot) {
                snapshot(output,argv[1],start,duration,0,bytes,packets,errors);
                next_snapshot=floor(wall_now()-start)+start+1;
            }
        }
    }
    close(fd); snapshot(output,argv[1],start,duration,1,bytes,packets,errors);
    free(bytes);free(packets);free(errors);return 0;
}
