"""Real, fixed-rate recommendation RPC traffic and client-side telemetry."""

from __future__ import annotations

import contextlib
import struct
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import grpc
from prometheus_client import CollectorRegistry, Gauge, delete_from_gateway, push_to_gateway

from sregym.generators.workload.hotel_search import KubectlPortForward

RPC_METHOD = "/recommendation.Recommendation/GetRecommendations"
REQUIREMENTS = ("dis", "rate", "price")
PUSH_JOB = "hotel_recommendation_client"


def _request_payload(requirement: str) -> bytes:
    """Encode the three fields in the existing recommendation.proto Request."""
    encoded = requirement.encode("ascii")
    return (
        b"\x0a"
        + bytes((len(encoded),))
        + encoded
        + b"\x11"
        + struct.pack("<d", 38.0235)
        + b"\x19"
        + struct.pack("<d", -122.095)
    )


@dataclass(frozen=True)
class RpcWindow:
    total: int
    timeouts: int
    other_errors: int
    p99_seconds: float

    @property
    def timeout_ratio(self) -> float:
        return self.timeouts / self.total if self.total else 0.0


class RecommendationRpcLoad:
    """Host-side gRPC client; never creates a Kubernetes observer Pod."""

    def __init__(self, namespace: str, rate: float = 39.0):
        self.namespace = namespace
        self.rate = rate
        self.timeout_seconds = 10.0
        self.forward = KubectlPortForward(namespace, "recommendation", 8085)
        self.gateway = KubectlPortForward("observe", "prometheus-prometheus-pushgateway", 9091)
        self.channel = None
        self.call = None
        self.executor: ThreadPoolExecutor | None = None
        self.stop_event = threading.Event()
        self.scheduler: threading.Thread | None = None
        self.publisher: threading.Thread | None = None
        self.events: deque[tuple[float, str, float]] = deque(maxlen=100_000)
        self.lock = threading.Lock()
        self.registry = CollectorRegistry()
        self.timeout_metric = Gauge(
            "hotel_recommendation_rpc_timeout_ratio",
            "Client-side timeout fraction of real GetRecommendations calls in the last minute",
            ["namespace", "service"],
            registry=self.registry,
        )
        self.p99_metric = Gauge(
            "hotel_recommendation_rpc_p99_seconds",
            "Client-side p99 latency of successful GetRecommendations calls in the last minute",
            ["namespace", "service"],
            registry=self.registry,
        )
        self.requests_metric = Gauge(
            "hotel_recommendation_rpc_window_requests",
            "GetRecommendations calls observed in the last minute",
            ["namespace", "service"],
            registry=self.registry,
        )

    def start(self) -> None:
        port = self.forward.start()
        gateway_port = self.gateway.start()
        self.gateway_url = f"127.0.0.1:{gateway_port}"
        self.channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        grpc.channel_ready_future(self.channel).result(timeout=15)
        self.call = self.channel.unary_unary(
            RPC_METHOD, request_serializer=lambda value: value, response_deserializer=lambda value: value
        )
        self.executor = ThreadPoolExecutor(max_workers=128, thread_name_prefix="recommendation-client")
        self.stop_event.clear()
        self.scheduler = threading.Thread(target=self._schedule, daemon=True)
        self.publisher = threading.Thread(target=self._publish_loop, daemon=True)
        self.scheduler.start()
        self.publisher.start()

    def _request(self, index: int) -> None:
        started = time.monotonic()
        status = "ok"
        try:
            response = self.call(_request_payload(REQUIREMENTS[index % 3]), timeout=self.timeout_seconds)
            if not response:
                status = "error"
        except grpc.RpcError as exc:
            status = "timeout" if exc.code() == grpc.StatusCode.DEADLINE_EXCEEDED else "error"
        except Exception:
            status = "error"
        with self.lock:
            self.events.append((time.monotonic(), status, time.monotonic() - started))

    def _schedule(self) -> None:
        next_at = time.monotonic()
        index = 0
        while not self.stop_event.is_set():
            now = time.monotonic()
            if now < next_at:
                self.stop_event.wait(min(next_at - now, 0.1))
                continue
            if self.executor is not None:
                self.executor.submit(self._request, index)
            index += 1
            next_at += 1.0 / self.rate
            if next_at < now - 1.0 / self.rate:
                next_at = now + 1.0 / self.rate

    def window(self, seconds: float = 60) -> RpcWindow:
        cutoff = time.monotonic() - seconds
        with self.lock:
            events = [event for event in self.events if event[0] >= cutoff]
        latencies = sorted(latency for _, status, latency in events if status == "ok")
        p99 = latencies[min(len(latencies) - 1, int(0.99 * len(latencies)))] if latencies else 0.0
        return RpcWindow(
            total=len(events),
            timeouts=sum(status == "timeout" for _, status, _ in events),
            other_errors=sum(status == "error" for _, status, _ in events),
            p99_seconds=p99,
        )

    def measure(self, seconds: int = 60) -> RpcWindow:
        started = time.monotonic()
        if self.stop_event.wait(seconds):
            raise RuntimeError("recommendation RPC load stopped during measurement")
        cutoff = started
        with self.lock:
            events = [event for event in self.events if event[0] >= cutoff]
        latencies = sorted(latency for _, status, latency in events if status == "ok")
        p99 = latencies[min(len(latencies) - 1, int(0.99 * len(latencies)))] if latencies else 0.0
        return RpcWindow(
            total=len(events),
            timeouts=sum(status == "timeout" for _, status, _ in events),
            other_errors=sum(status == "error" for _, status, _ in events),
            p99_seconds=p99,
        )

    def _publish_loop(self) -> None:
        labels = (self.namespace, "recommendation")
        while not self.stop_event.is_set():
            window = self.window()
            self.timeout_metric.labels(*labels).set(window.timeout_ratio)
            self.p99_metric.labels(*labels).set(window.p99_seconds)
            self.requests_metric.labels(*labels).set(window.total)
            with contextlib.suppress(Exception):
                push_to_gateway(self.gateway_url, job=PUSH_JOB, registry=self.registry)
            # The preflight separately requires Prometheus visibility; one
            # transient push failure must not stop real application traffic.
            self.stop_event.wait(5)

    def stop(self) -> None:
        self.stop_event.set()
        if self.scheduler is not None:
            self.scheduler.join(timeout=5)
            self.scheduler = None
        if self.publisher is not None:
            self.publisher.join(timeout=5)
            self.publisher = None
        if self.executor is not None:
            # In-flight RPCs have a bounded deadline. Wait for them before
            # deleting telemetry or closing the port-forward.
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.executor = None
        try:
            if hasattr(self, "gateway_url"):
                port = self.gateway.start()
                delete_from_gateway(f"127.0.0.1:{port}", job=PUSH_JOB)
        finally:
            if self.channel is not None:
                self.channel.close()
                self.channel = None
            self.forward.stop()
            self.gateway.stop()
