# Noise case design requirements

This is a mandatory design gate for every new or materially redesigned noise
case. Read it before proposing, implementing, testing, or reviewing a noise
profile. A case that does not satisfy this document is not ready to run with an
agent.

## Purpose

A noise case is an intentionally **real, observable, non-causal fault** that
coexists with a benchmark's primary fault. It should make diagnosis harder by
presenting a plausible secondary incident, not by breaking the application in a
second uncontrolled way or by exposing an artificial benchmark-only workload.

## Required evidence before implementation

Record the following in an app- and case-specific design note. Link to it from
the implementation and its user-facing documentation.

1. **Primary-fault boundary.** State the primary fault, its user-visible
   symptom, entrypoint, and every service on each affected transaction's
   critical call path.
2. **Topology evidence.** Use three sources together:
   - the application's authoritative architecture material (for example, its
     upstream repository or original paper);
   - the SREGym deployment manifests and source code (the source of truth for
     what this checkout actually deploys); and
   - runtime traffic or traces when available (supporting evidence only).
3. **Candidate service semantics.** Name the candidate service, explain its
   real business responsibility, and list the transactions that can reach it.
   Low traffic is evidence of lower exposure, not proof that a service is
   unimportant.
4. **Isolation argument.** Show why the candidate is outside every affected
   primary transaction, or strictly downstream of the primary fault boundary.
   A generic statement such as "it is downstream of frontend" is insufficient
   when frontend is the faulted service: the analysis must cover the workload's
   actual request types.
5. **Fault mechanism.** Specify a real failure mechanism that changes the
   selected service's own behavior or health. State the observable symptom,
   injection scope, expected lifetime, and cleanup order. The normal default is
   to inject into an existing application microservice, not to create a
   benchmark-only observer Pod. An exception requires a written explanation of
   the real component that the new workload represents.
6. **Non-interference validation.** Define automatic checks that prove both:
   - the noise target exhibits its intended real symptom; and
   - the primary user-facing probe and critical-path topology have the same
     primary-fault behavior with and without the noise. In particular, the
     noise must not change the primary service's endpoints, workload outcome,
     or fault oracle result.

## Selection rules

- Prefer an existing service outside the primary critical path.
- A service strictly downstream of the primary-fault boundary is a candidate,
  not an automatic approval. Validate it against all workload transactions.
- Do not choose a service based only on traffic volume. A low-traffic service
  can be critical for a rare operation.
- Do not target shared control-plane, discovery, ingress, node-wide, or shared
  storage components unless the isolation argument proves their impact is
  contained.
- Do not add agent instructions that identify a workload as noise or direct the
  agent to ignore it. The treatment must remain a blind, plausible distractor.
- Keep all agent-visible naming and metadata neutral. Internal preflight and
  cleanup evidence must not be exposed as a root-cause hint.

## Required implementation gates

Do not start an agent experiment until all gates pass:

1. Baseline deployment and the primary fault preflight pass.
2. The noise injection produces the documented symptom on the selected real
   service.
3. The primary user-facing probe still shows the same primary-fault symptom.
4. The primary service's endpoints and critical-path workload behavior are
   unchanged by the noise.
5. The noise remains stable through diagnosis, mitigation, and oracle
   validation; only final cleanup or abort removes it.
6. Cleanup restores the selected service and removes the treatment without
   leaving resources behind.

## Hotel Reservation reference

For Hotel Reservation work, read
[`hotel-reservation-topology.md`](hotel-reservation-topology.md) in addition to
this document. It records the topology and workload mix in the current SREGym
checkout. Revalidate it if manifests, workload scripts, or application source
change.

## Current clock-skew status

The observer-Pod design predates this standard and remains historical context.
The replacement targets the existing recommendation service; see
[`clock-skew-search-rate-design.md`](clock-skew-search-rate-design.md). Its code
and unit tests do not by themselves establish a valid experiment. The runtime
gates above, a matched no-noise run, and a real CloudLab TimeChaos smoke test
must pass before formal conclusions.
