/* Dependency-free IPv6 UDP probe. Per-second payload counters, fixed epoch.
 * Build statically so this also runs in the original task-1/3 nodes without Python.
 */
#define _POSIX_C_SOURCE 200809L
#include <arpa/inet.h>
#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

static double now(void) {
    struct timespec t; clock_gettime(CLOCK_REALTIME, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}
static void until(double timestamp) {
    double remaining;
    while ((remaining = timestamp - now()) > 0) {
        if (remaining > .1) remaining = .1;
        struct timespec t = {(time_t)remaining, (long)((remaining - (time_t)remaining) * 1e9)};
        nanosleep(&t, NULL);
    }
}
static void die(const char *what) { perror(what); exit(1); }
int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "udp6-probe server|client --port N --duration S --start-at EPOCH --output FILE [--dst IPv6 --rate-kbps K --source-port N --payload N]\n"); return 2; }
    int server = !strcmp(argv[1], "server"), duration = 0, port = 0, source_port = 0, payload = 1000;
    double start = 0, rate = 0;
    const char *destination = NULL, *output = NULL;
    for (int i = 2; i < argc; i += 2) {
        if (i + 1 == argc) { fprintf(stderr, "Missing option value\n"); return 2; }
        const char *key = argv[i], *value = argv[i + 1];
        if (!strcmp(key, "--port")) port = atoi(value);
        else if (!strcmp(key, "--source-port")) source_port = atoi(value);
        else if (!strcmp(key, "--duration")) duration = atoi(value);
        else if (!strcmp(key, "--start-at")) start = atof(value);
        else if (!strcmp(key, "--rate-kbps")) rate = atof(value);
        else if (!strcmp(key, "--payload")) payload = atoi(value);
        else if (!strcmp(key, "--dst")) destination = value;
        else if (!strcmp(key, "--output")) output = value;
        else { fprintf(stderr, "Unknown option: %s\n", key); return 2; }
    }
    if (duration < 1 || duration > 3600 || port < 1 || port > 65535 || start <= 0 || !output ||
        (!server && (strcmp(argv[1], "client") || !destination || rate <= 0 || payload < 1 || payload > 1200))) {
        fprintf(stderr, "Invalid probe arguments\n"); return 2;
    }
    uint64_t *bytes = calloc(duration, sizeof(uint64_t)), *packets = calloc(duration, sizeof(uint64_t));
    if (!bytes || !packets) die("calloc");
    int fd = socket(AF_INET6, SOCK_DGRAM, 0); if (fd < 0) die("socket");
    int buffer = 4 * 1024 * 1024, yes = 1;
    setsockopt(fd, SOL_SOCKET, server ? SO_RCVBUF : SO_SNDBUF, &buffer, sizeof(buffer));
    setsockopt(fd, IPPROTO_IPV6, IPV6_V6ONLY, &yes, sizeof(yes));
    struct sockaddr_in6 local = {.sin6_family = AF_INET6, .sin6_addr = IN6ADDR_ANY_INIT,
                               .sin6_port = htons(server ? port : source_port)};
    if (bind(fd, (struct sockaddr *)&local, sizeof(local))) die("bind");
    uint64_t sent = 0, target = 0, errors = 0;
    char data[65536] = {0};
    if (server) {
        struct timeval timeout = {.tv_sec = 0, .tv_usec = 100000};
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        until(start);
        while (now() < start + duration) {
            ssize_t size = recv(fd, data, sizeof(data), 0);
            if (size < 0) { if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) continue; die("recv"); }
            int index = (int)floor(now() - start);
            if (index >= 0 && index < duration) { bytes[index] += size; packets[index]++; }
        }
    } else {
        struct sockaddr_in6 remote = {.sin6_family = AF_INET6, .sin6_port = htons(port)};
        if (inet_pton(AF_INET6, destination, &remote.sin6_addr) != 1) { fprintf(stderr, "Invalid IPv6\n"); return 2; }
        double pps = rate * 1000.0 / (8.0 * payload);
        target = (uint64_t)llround(pps * duration);
        int batch = (int)llround(pps / 100); if (batch < 1) batch = 1; if (batch > 16) batch = 16;
        uint64_t attempted = 0;
        while (attempted < target) {
            until(start + attempted / pps);
            if (now() >= start + duration) break;
            for (int i = 0; i < batch && attempted < target; i++, attempted++) {
                ssize_t size = sendto(fd, data, payload, MSG_DONTWAIT, (struct sockaddr *)&remote, sizeof(remote));
                int index = (int)floor(now() - start);
                if (size == payload) {
                    sent++;
                    if (index >= 0 && index < duration) { bytes[index] += size; packets[index]++; }
                } else errors++;
            }
        }
    }
    close(fd);
    FILE *stream = fopen(output, "w"); if (!stream) die("output");
    fprintf(stream, "{\"role\":\"%s\",\"start_at\":%.6f,\"duration\":%d,\"sent_packets\":%llu,\"target_packets\":%llu,\"send_errors\":%llu,\"bytes_per_second\":[",
        server ? "server" : "client", start, duration, (unsigned long long)sent,
        (unsigned long long)target, (unsigned long long)errors);
    for (int i = 0; i < duration; i++) fprintf(stream, "%s%llu", i ? "," : "", (unsigned long long)bytes[i]);
    fprintf(stream, "],\"packets_per_second\":[");
    for (int i = 0; i < duration; i++) fprintf(stream, "%s%llu", i ? "," : "", (unsigned long long)packets[i]);
    fprintf(stream, "]}\n"); fclose(stream); free(bytes); free(packets);
    return 0;
}
