#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t running = 1;

static void stop(int signal_number) {
    (void)signal_number;
    running = 0;
}

static void sleep_millis(long millis) {
    struct timespec delay = {.tv_sec = millis / 1000, .tv_nsec = (millis % 1000) * 1000000L};
    while (nanosleep(&delay, &delay) == -1 && errno == EINTR) {
    }
}

static int current_epoch(long long *epoch) {
    struct timespec now;
    if (clock_gettime(CLOCK_REALTIME, &now) != 0) {
        perror("clock_gettime");
        return -1;
    }
    *epoch = (long long)now.tv_sec;
    return 0;
}

static int write_epoch(const char *path, long long epoch) {
    char temporary[PATH_MAX];
    char contents[64];
    int path_size = snprintf(temporary, sizeof(temporary), "%s.tmp", path);
    int contents_size = snprintf(contents, sizeof(contents), "%lld\n", epoch);
    if (path_size < 0 || path_size >= (int)sizeof(temporary) || contents_size < 0) {
        fprintf(stderr, "epoch path is too long\n");
        return -1;
    }

    int descriptor = open(temporary, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (descriptor < 0) {
        perror("open epoch temporary file");
        return -1;
    }
    ssize_t written = write(descriptor, contents, (size_t)contents_size);
    int close_result = close(descriptor);
    if (written != contents_size || close_result != 0) {
        perror("write epoch");
        unlink(temporary);
        return -1;
    }
    if (rename(temporary, path) != 0) {
        perror("rename epoch");
        unlink(temporary);
        return -1;
    }
    return 0;
}

static int read_epoch(const char *path, long long *epoch) {
    char contents[64];
    int descriptor = open(path, O_RDONLY);
    if (descriptor < 0) {
        return errno == ENOENT ? 1 : -1;
    }
    ssize_t count = read(descriptor, contents, sizeof(contents) - 1);
    int close_result = close(descriptor);
    if (count < 1 || close_result != 0) {
        return -1;
    }
    contents[count] = '\0';
    char *end = NULL;
    errno = 0;
    long long value = strtoll(contents, &end, 10);
    if (errno != 0 || end == contents) {
        return -1;
    }
    *epoch = value;
    return 0;
}

static int create_marker(const char *path) {
    int descriptor = open(path, O_WRONLY | O_CREAT, 0644);
    if (descriptor < 0) {
        perror("create fault marker");
        return -1;
    }
    if (close(descriptor) != 0) {
        perror("close fault marker");
        return -1;
    }
    return 0;
}

static void clear_marker(const char *path) {
    if (unlink(path) != 0 && errno != ENOENT) {
        perror("clear fault marker");
    }
}

static int run_reference(const char *epoch_path) {
    while (running) {
        long long epoch = 0;
        if (current_epoch(&epoch) == 0) {
            write_epoch(epoch_path, epoch);
        }
        sleep_millis(250);
    }
    return 0;
}

static int run_observer(const char *epoch_path, const char *marker_path, long long threshold_seconds) {
    int consecutive = 0;
    bool reported = false;
    while (running) {
        long long reference = 0;
        long long observed = 0;
        int reference_result = read_epoch(epoch_path, &reference);
        if (reference_result == 0 && current_epoch(&observed) == 0) {
            long long delta = observed - reference;
            long long absolute = delta < 0 ? -delta : delta;
            if (absolute >= threshold_seconds) {
                consecutive++;
                if (consecutive >= 3 && create_marker(marker_path) == 0 && !reported) {
                    printf("CLOCK_SKEW_FAULT offset_seconds=%lld\n", delta);
                    fflush(stdout);
                    reported = true;
                }
            } else {
                consecutive = 0;
                reported = false;
                clear_marker(marker_path);
            }
        }
        sleep_millis(250);
    }
    return 0;
}

static int healthcheck(const char *marker_path) {
    if (access(marker_path, F_OK) != 0 && errno == ENOENT) {
        return 0;
    }
    return 1;
}

int main(int argc, char **argv) {
    signal(SIGTERM, stop);
    signal(SIGINT, stop);
    if (argc == 3 && strcmp(argv[1], "reference") == 0) {
        return run_reference(argv[2]);
    }
    if (argc == 5 && strcmp(argv[1], "observe") == 0) {
        char *end = NULL;
        errno = 0;
        long long threshold = strtoll(argv[4], &end, 10);
        if (errno == 0 && end != argv[4] && *end == '\0' && threshold > 0) {
            return run_observer(argv[2], argv[3], threshold);
        }
    }
    if (argc == 3 && strcmp(argv[1], "healthcheck") == 0) {
        return healthcheck(argv[2]);
    }
    fprintf(stderr, "usage: %s reference EPOCH_PATH | observe EPOCH_PATH MARKER_PATH THRESHOLD | healthcheck MARKER_PATH\n", argv[0]);
    return 2;
}
