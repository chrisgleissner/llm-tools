---
title: Connection saturation tests need completed handshakes
author: Christian
date: 2026-11-07
branch: feat/distributed-auth
id: l-1540b5eb
scope: repo
status: active
src: agent
touches:
  - coordinator/tests/hardening.rs
---

## Decided
Use a completed TLS handshake to establish that the coordinator accepted a connection and assigned it a connection slot. A successful TCP connect only shows that the operating system accepted the socket into its listener queue; a fixed delay does not prove the application accept loop has processed it.

## Tried and ruled out
The worker-reserve test held six raw TCP sockets and slept 300 ms before probing. A full coordinator run reported a TLS EOF in the reserve test while the focused test passed, consistent with load-dependent timing at the listener. Re-running a sleep or increasing it would still rely on a guessed delay rather than observing admission.

## Watch out
Keep the test assertions for both the browser refusal and the valid worker admission. The completed handshakes should occupy the intended general slots, and the worker probe must still prove reserve capacity remains available.

In the code:
`coordinator/tests/hardening.rs::worker_reserve_admits_workers_when_general_slots_are_full` holds six `tls_connect` results before making either probe.
