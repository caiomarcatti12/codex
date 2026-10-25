## Consolidated update: single-owner recovery, multiple clients, corrected build version, and tagged reference

Both the Windows and iOS endpoints now appear connected in my latest device test. This is an initial user observation; extended use and repeated real-device handoffs are still being evaluated. Independent checks against the running server also confirmed two protocol clients resuming the same active conversation, the corrected advertised version, and the expected socket permissions and owner lock.

This update consolidates the [original diagnosis](https://github.com/openai/codex/issues/56598#issuecomment-6399983889), the [endpoint-recovery follow-up](https://github.com/openai/codex/issues/56598#issuecomment-6400678508), and the subsequent build-version correction. Earlier evidence is identified separately from the latest binary and live-host checks.

### Reference code and version identity

- Signed reference tag: [0.164.0+issue-56598](https://github.com/caiomarcatti12/codex/tree/0.164.0%2Bissue-56598).
- Owner lock and socket cleanup: [6bd11e78](https://github.com/caiomarcatti12/codex/commit/6bd11e78e6a8a0ed6c4d84682ea002743c6964db).
- Missing-endpoint recovery and multi-client harness: [1661d031](https://github.com/caiomarcatti12/codex/commit/1661d031c7bd22bfe530da161ebc0c4021083d55).
- Compiled version and handshake assertions: [8cc184fb](https://github.com/caiomarcatti12/codex/commit/8cc184fbc4c7a61c2084be5a94d0580fe46db982).

The tag includes those code changes and this evidence document. The installed binary was built from `8cc184fbc4c7a61c2084be5a94d0580fe46db982`; the additional documentation commit does not change executable source. Its internal version is `0.164.0+local.1661d031`. The tag name `0.164.0+issue-56598` identifies this issue's reference snapshot, not a different compiled artifact or an official OpenAI release.

### Evidence before the changes

The original incident showed multiple live app-servers using the same Codex home while an older process retained thread writer locks. A reached replacement server could not resume the conversation:

```text
2026-10-25T14:20:32.115231Z ERROR codex_core::session::session:
failed to initialize thread persistence: thread-store conflict:
thread <redacted-thread-id> already has an active writer
```

The kernel lock belonged to another live process; this was not simply a stale lock filename. The original startup lock protected endpoint publication only, and pathname-based cleanup could mistake a successor's socket for its predecessor's socket.

After the initial owner-lock fix, a different availability gap appeared when Windows remained connected but iOS could not establish a new connection. Recorded host observations were:

```text
existing_app_server_alive=yes
owner_lock_held_by_existing_app_server=yes
existing_client_connections_seen=3
rendezvous_symlink_present=yes
physical_socket_path_present=no
kernel_still_has_live_listener_and_established_streams=yes
new_start_error=another app-server process already owns this Codex home
```

Accepted Unix streams can remain alive after the socket pathname is unlinked. Existing clients could therefore keep working while new clients had no usable endpoint. The owner lock correctly rejected another server, but the initial fix did not repair the existing owner's missing endpoint. The operation that removed the physical pathname has not been established.

A subsequent local packaging error produced another client-side blocker: the custom executable and its initialization user agent advertised `0.0.0`, despite the surrounding package manifest already being `0.164.0`. The client reported a minimum of `0.137.0-alpha.4`. This was an error in the custom build's version metadata, not evidence that the installed package was old:

```text
endpoint_recovery_smoke/0.0.0 (Ubuntu 26.4.0; x86_64) unknown (endpoint_recovery_smoke; 1)
```

### What changed in the project, step by step

1. **Retain one owning Unix app-server per Codex home.** `codex-rs/app-server-transport/src/transport/unix_socket.rs` adds a nonblocking OS lock on `app-server-owner.lock`. The app-server startup path retains its guard for the owning process's lifetime. A duplicate owner fails before replacing the endpoint; normal process exit releases the kernel lock. The transport exports and `codex-rs/app-server/src/lib.rs` connect this guard to startup.

2. **Make cleanup identify the socket object.** The Unix socket guard records the device and inode after binding. Cleanup checks that identity before unlinking the physical socket, so an old guard cannot delete a replacement merely because it reuses the same pathname. Rendezvous cleanup also respects ownership and the expected target.

3. **Repair missing endpoints in the existing owner.** The Unix accept loop checks its endpoint once per second. It recreates a missing rendezvous alias or binds a replacement for a missing physical socket inside the same app-server process. It preserves accepted streams, thread subscriptions, active work, and thread writer ownership. New clients connect to that same owner. This repairs availability without introducing multiple independent writers.

4. **Coordinate recovery without blocking existing clients.** Recovery tries the startup lock without waiting, revalidates paths and private-directory permissions, records the new socket identity, and safely transfers cleanup responsibility. If a path contains a replacement object, recovery refuses to overwrite it. A contended lock defers repair to a later check.

5. **Correct the custom build's advertised version.** `codex-rs/Cargo.toml` now sets the workspace package version to `0.164.0+local.1661d031`. `Cargo.lock` updates the 168 workspace package versions; the external dependency comparison found zero changes. The installed package manifest was aligned with the binary. This labels the recent source plus local changes explicitly; it does not disable or bypass a client's version check.

6. **Check version and behavior together.** `scripts/verify_endpoint_recovery.py` now verifies both `codex --version` and the actual `initialize` response, in addition to exercising concurrent clients, endpoint loss, handoff, and restart. The transport regression cases are in `codex-rs/app-server-transport/src/transport/unix_socket_tests.rs`.

The per-thread writer locks and the documented last-subscriber unload grace period remain unchanged. Multiple clients use one owning server; multiple independent processes do not gain permission to write the same conversation.

### Controlled reproduction and validation

The earlier endpoint-recovery regression failed against the initial owner-lock-only implementation:

```text
control_socket_recovers_missing_endpoint_without_disconnecting_clients ... FAILED
the live owner must restore its endpoint: Elapsed(())
```

The real-binary harness likewise established two connections before failing to reconnect after endpoint removal:

```text
two_clients_same_thread=PASS
TimeoutError: app-server endpoint did not become reachable
```

The harness uses an isolated temporary `CODEX_HOME` and a loopback mock model, without production credentials or external model requests. It performs these steps:

1. Start one server and attach two WebSocket clients to the same thread.
2. Hold the mock model response mid-turn.
3. Remove only the fixture's rendezvous and physical socket paths, simulating the observed missing-endpoint state.
4. Attach a third client through the repaired endpoint and resume the same active thread.
5. Attempt a duplicate app-server startup and assert that the owner lock rejects it.
6. Release the model response and require all three clients to receive the completed turn.
7. Close the first client, submit a further turn through the third, and require the second to receive its completion too.
8. Reconnect the original client, then gracefully restart the server and resume the same persisted thread.

Previously recorded transport validation, after the endpoint-recovery change:

```text
cargo test --locked -p codex-app-server-transport unix_socket_tests
test result: ok. 15 passed; 0 failed

cargo clippy --locked -p codex-app-server-transport --lib --tests -- -D warnings
Finished dev profile successfully
```

Those transport checks were not rerun for the metadata-only version correction. The final executable was rebuilt and the full real-binary harness was rerun, including against the stripped, installed artifact. The final build command was:

```sh
CARGO_BUILD_JOBS=4 \
CARGO_PROFILE_RELEASE_LTO=false \
CARGO_PROFILE_RELEASE_DEBUG=0 \
STABLE_GIT_COMMIT=8cc184fbc4c7a61c2084be5a94d0580fe46db982 \
cargo build --locked --release -p codex-cli --bin codex
```

The build completed successfully in 25m 29s, with unused-variable/import warnings. From the repository root, the harness command is:

```sh
python3 scripts/verify_endpoint_recovery.py /absolute/path/to/codex 0.164.0+local.1661d031
```

### Evidence after the changes

Output from the final installed artifact, with exit status 0:

```text
cli_version=0.164.0+local.1661d031
initialize_version=0.164.0+local.1661d031
two_clients_same_thread=PASS
endpoint_recovered_ms=593
second_writer_rejected=PASS
active_turn_survived_and_all_three_clients_received_completion=PASS
device_handoff_and_original_client_reconnect=PASS
graceful_restart_same_thread=PASS
```

The recovery measurement is from that test run, not a latency guarantee. Installed executable SHA-256:

```text
007cba931547be5f90ef6413455ea76055f852b7b5fea4caf6fd2f3f1e7ba222
```

The live server was subsequently checked independently, rather than inferring deployment from the build result:

```text
version=0.164.0+local.1661d031
two_independent_connections_same_thread=true
thread_status=active
socket_mode=0o600
running_binary_matches_tested_artifact=true
owner_kernel_lock_held=true
```

The initialization user agent from that live process was:

```text
endpoint_recovery_smoke/0.164.0+local.1661d031 (Ubuntu 26.4.0; x86_64) unknown (endpoint_recovery_smoke; 1)
```

The latest device observation is that both Windows and iOS now appear connected. That user observation complements the protocol checks; it is not a completed long-running reliability study or proof that every real-device reconnect scenario works.

### Deployment limitation observed

For transparency, the one-shot local deployment helper waited for zero active turns, gracefully stopped the old owner, and started the verified replacement. Its automatic conversation-restoration loop then encountered a thread with no persisted rollout and stopped:

```text
2026-10-25T18:00:40Z requesting_graceful_shutdown active_turns=0
2026-10-25T18:00:40Z old_owner_exited
2026-10-25T18:00:40Z endpoint_reachable
2026-10-25T18:00:42Z activation_failed
thread/resume: no rollout found for thread id <redacted-thread-id>
```

These timestamps are UTC. The helper failure did not terminate the replacement server. The live checks above verified that it remained running and that the current conversation could be resumed by two independent clients. I am not claiming that this helper automatically restored every loaded thread. The helper is a local deployment utility, not part of the proposed transport change; its restoration-loop handling is a separate limitation.

### Security assessment and compatibility boundaries

The changes preserve the single-writer invariant, private control-directory checks, and socket mode `0600`. They add no network listener, RPC method, authentication bypass, or privilege transition. Recovery only recreates missing endpoint objects and declines to overwrite replacements. Regression cases cover replacement preservation, unsafe directory permissions, and startup-lock contention.

These checks support the assessment that the change does not intentionally widen the existing access boundary. They are not a proof of absence of all security vulnerabilities. A same-UID process can still disrupt files and locks it already controls; this approach does not attempt to isolate mutually untrusted processes sharing that Unix identity.

Maintainer review is still needed on the lock's scope: one Unix app-server per `CODEX_HOME` also affects custom Unix socket paths. If multiple independent custom-path servers sharing one home are intentionally supported, an explicit ownership namespace or default-endpoint scope may be preferable. The owner-guard lifetime during managed forced shutdown also merits review, as noted in the original analysis.

### Why this deserves an upstream solution

The thread writer lock correctly protects persisted history. The defect is that endpoint publication, cleanup, and recovery can leave the reachable server different from the owner of ongoing work, or leave the owner unreachable to new clients. Retrying or forking does not restore control of the original active conversation.

The combined approach addresses integrity and availability at the same lifecycle boundary: keep one owner, preserve its accepted connections, repair a missing route to it, and avoid deleting a successor's socket. The controlled failure and passing recovery tests provide a reproducible basis for an upstream implementation. The custom build-version fix is packaging evidence for this local validation, rather than a request to adopt this fork's version string upstream.

### Contribution policy and request for maintainer analysis

I rechecked [`docs/contributing.md`](https://github.com/openai/codex/blob/main/docs/contributing.md): it welcomes detailed issue reports, reproductions, logs, root-cause analysis, and design discussion, and does not accept external code contributions or PRs. The signed fork/tag is a concrete reference for that discussion; no external PR is being submitted.

Could the team review the combined ownership, identity-safe cleanup, and in-process endpoint-recovery approach, especially the intended custom-socket ownership scope and reconnect lifecycle? I will continue testing the Windows/iOS workflow and report additional observations in this issue.
