FROM alpine:3.22 AS build
RUN apk add --no-cache build-base
WORKDIR /src
COPY clock_skew_observer.c .
RUN gcc -O2 -Wall -Wextra -Werror -static -o analytics-time-indexer clock_skew_observer.c \
    && install -D -m 0755 analytics-time-indexer /out/usr/local/bin/analytics-time-indexer

FROM scratch
COPY --from=build /out/usr/local/bin/analytics-time-indexer /usr/local/bin/analytics-time-indexer
ENTRYPOINT ["/usr/local/bin/analytics-time-indexer"]
