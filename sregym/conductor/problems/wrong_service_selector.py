from sregym.conductor.oracles.llm_as_a_judge.llm_as_a_judge_oracle import LLMAsAJudgeOracle
from sregym.conductor.oracles.service_endpoint_mitigation import ServiceEndpointMitigationOracle
from sregym.conductor.problems.base import Problem
from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector
from sregym.service.apps.astronomy_shop import AstronomyShop
from sregym.service.apps.hotel_reservation import HotelReservation
from sregym.service.apps.social_network import SocialNetwork
from sregym.service.kubectl import KubeCtl
from sregym.utils.decorators import mark_fault_injected


class WrongServiceSelector(Problem):
    def __init__(self, app_name="astronomy_shop", faulty_service="frontend"):
        self.app_name = app_name
        self.faulty_service = faulty_service

        if app_name == "social_network":
            app = SocialNetwork()
        elif app_name == "hotel_reservation":
            app = HotelReservation()
        elif app_name == "astronomy_shop":
            app = AstronomyShop()
        else:
            raise ValueError(f"Unsupported app name: {app_name}")
        self.expected_service_port = 9090 if app_name == "social_network" else app.frontend_port
        super().__init__(app=app)
        self.kubectl = KubeCtl()
        self.root_cause = self.build_structured_root_cause(
            component=self.faulty_service,
            namespace=self.namespace,
            description=(
                f"The service `{self.faulty_service}` has a misconfigured selector that adds an incorrect label, "
                "so it no longer matches the intended backing pods. The service has zero or insufficient endpoints "
                "despite healthy-looking deployments, causing routing failures at the service layer. Users observe "
                "connection resets/timeouts and partial outages when requests are sent through this service."
            ),
        )

        self.diagnosis_oracle = LLMAsAJudgeOracle(problem=self, expected=self.root_cause)

        self.app.create_workload()
        self.mitigation_oracle = ServiceEndpointMitigationOracle(problem=self)

    @mark_fault_injected
    def inject_fault(self):
        print("== Fault Injection ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._inject(
            fault_type="wrong_service_selector",
            microservices=[self.faulty_service],
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")

    def validate_fault_preflight(self):
        """Fail closed unless the injected selector has produced zero endpoints.

        This check runs after fault injection and before any agent session.  It
        deliberately returns no diagnostic detail to the agent; its purpose is
        to prevent a failed injection from being counted as an agent result.
        """
        core_v1 = self.kubectl.core_v1_api
        service = core_v1.read_namespaced_service(name=self.faulty_service, namespace=self.namespace)
        selector = dict(service.spec.selector or {})
        injected_key = "current_service_name"
        if selector.get(injected_key) != self.faulty_service:
            raise RuntimeError(
                f"wrong_service_selector preflight failed: {self.namespace}/{self.faulty_service} "
                "does not contain the injected selector"
            )

        label_selector = ",".join(f"{key}={value}" for key, value in sorted(selector.items()))
        matching_pods = core_v1.list_namespaced_pod(
            namespace=self.namespace,
            label_selector=label_selector,
        ).items
        if matching_pods:
            raise RuntimeError(
                f"wrong_service_selector preflight failed: {self.namespace}/{self.faulty_service} still selects Pod(s)"
            )

        endpoints = core_v1.read_namespaced_endpoints(name=self.faulty_service, namespace=self.namespace)
        ready_addresses = [address for subset in (endpoints.subsets or []) for address in (subset.addresses or [])]
        if ready_addresses:
            raise RuntimeError(
                f"wrong_service_selector preflight failed: {self.namespace}/{self.faulty_service} "
                "still has ready endpoints"
            )

    @mark_fault_injected
    def recover_fault(self):
        print("== Fault Recovery ==")
        injector = VirtualizationFaultInjector(namespace=self.namespace)
        injector._recover(
            fault_type="wrong_service_selector",
            microservices=[self.faulty_service],
        )
        print(f"Service: {self.faulty_service} | Namespace: {self.namespace}\n")
