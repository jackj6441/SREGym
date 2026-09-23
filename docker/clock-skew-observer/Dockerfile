FROM alpine:3.22 AS build
RUN apk add --no-cache build-base
WORKDIR /src
COPY clock_skew_observer.c .
RUN gcc -O2 -Wall -Wextra -Werror -static -o clock-skew-observer clock_skew_observer.c \
    && install -D -m 0755 clock-skew-observer /out/usr/local/bin/clock-skew-observer

FROM scratch
COPY --from=build /out/usr/local/bin/clock-skew-observer /usr/local/bin/clock-skew-observer
ENTRYPOINT ["/usr/local/bin/clock-skew-observer"]
