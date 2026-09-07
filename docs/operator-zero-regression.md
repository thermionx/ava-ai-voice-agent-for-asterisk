# Operator Zero regression baseline

## Scope and architecture

Operator Zero spans two repositories. This AVA repository owns ARI/Stasis call setup, media bridges, realtime AI providers, tool execution, deferred transfer, private announcement playback, bridge finalization, failure handling, and cleanup. The companion `operator-zero` repository owns the Asterisk routing contexts, the checked-in agent template, caller-memory HTTP service, directions/search service, and MiniPC deployment instructions. The live agent records are stored in AVA's SQLite agent store and are deployment state rather than source-controlled configuration.

The highest-risk path is concentrated in `src/engine.py`: provider lifecycle, ARI events, media ownership, playback completion, transfer state, private announcements, trust updates, and terminal cleanup share mutable `CallSession` fields and several engine-owned dictionaries and sets. The transfer tool builds and stores a deferred action; the engine later commits it after caller-facing playback. Caller memory is reached over HTTP, while trusted-caller bypass and Dial 0 are controlled by Asterisk dialplan contexts.

## Important call states

External calls follow `incoming -> screening -> transfer armed -> household predial -> private announcement -> bridged`, with timeout/failure routing to voicemail or deterministic teardown. Internal Dial 0 follows `from-house/0 -> Stasis -> internal agent`, and voicemail retrieval exits Stasis into `operator-zero-voicemail-main`. Every active state can receive caller hangup, provider failure, tool failure, timeout, or restart; these must converge on resource cleanup unless Asterisk has explicitly taken ownership through a successful dialplan continuation.

Transfer ownership is fail-closed: answering the inside leg is not acceptance. The private announcement must complete before the outside and inside legs are bridged, and trust is updated only after both events. A failed announcement, bridge, or continuation must not leave `transfer_active` masking cleanup indefinitely.

## Baseline (2026-09-04)

Before hardening changes, the complete suite in an isolated copy of the production image reported **2325 passed, 22 skipped, 10 failed**. Seven failures were test-environment limitations (the minimal production image lacks Git and/or a `python3` command used by update-script tests). Three were pre-existing implementation/expectation mismatches: OpenAI greeting input gating and two deferred outbound-dial tests. None was introduced by this hardening work.

The first attempted baseline used the container's system Python and produced collection errors from missing runtime packages. That run is invalid and is not counted as a repository baseline.

## Regression risks and test layers

- Fast unit tests cover screening evidence and a deterministic call-event contract.
- Existing mocked tool tests cover blind transfer, voicemail, hangup, HTTP lookup, recent/manage caller plumbing, and transfer failure.
- Existing simulated ARI tests cover predial answer, private announcement, bridge success/failure, concurrent finalization, unbridged-leg cleanup, provider failure, playback, and bidirectional media.
- `./test-operator-zero` intentionally runs the complete pytest suite so unrelated previously working behavior is part of the gate.

Implicit and timing-sensitive state remains the main architectural risk. Playback waiter results, `current_action`, `transfer_active`, provider task maps, bridge membership, and ARI channel destruction can change concurrently. AI instructions still influence identity collection, exact phrasing, language, tool choice, and the first goodbye; deterministic admission validation, transfer ownership, timeout, and hangup must remain in call-control code rather than relying on prompt compliance.

The predial handoff now has a production `OperatorZeroTransferState` controller with explicit dialing, answered, ready, announcing, announced, bridged, voicemail, and failed phases. It retains legacy action fields for compatibility. Announcement ownership is serialized per call, and an announcement claim is no longer represented by the same flag as successful playback; bridging and automatic trust therefore fail closed until playback actually completes.

## Manual/hardware validation

Real phones remain necessary to validate analog/SIP endpoint behavior, Twilio ingress, carrier hangup propagation, audible barge-in, acoustic echo and clipping, bidirectional audio after the private announcement, real Asterisk sound-file permissions, voicemail audio/recording, busy/reorder tones, and restart recovery while a call is active. Prompt/model compliance (English-only speech, identity questions, exact greeting and one-time transfer phrase) also needs a small live-call smoke test because deterministic tests cannot prove a hosted model's generated speech.

## Running the gate

Create `.venv`, install both `requirements.txt` and `requirements-dev.txt`, then run:

```sh
./test-operator-zero
```

The command returns pytest's nonzero status on any failure. Arguments are forwarded, so a focused diagnostic run such as `./test-operator-zero tests/operator_zero` is also available, but it does not replace the complete gate.

### Isolated MiniPC test environment

`scripts/Dockerfile.operator-zero-test` extends the local runtime image with the
development requirements, Git, and system Python. The update-script tests use a
restricted PATH and need `/usr/bin/python3`; the virtualenv interpreter alone is
insufficient. The secret-scan test also requires a Git index containing the source
files being tested.

Build from the source snapshot to test:

```sh
docker build -f scripts/Dockerfile.operator-zero-test -t operator-zero-regression:local .
```

Run against a disposable checkout (including the intended uncommitted changes),
with no production credentials or data. For an exported source snapshot, initialize
a temporary Git repository and stage its source files first. Do not mount the live
checkout: tests create and modify files. Substitute its absolute path below:

```sh
docker run --rm --network none --cpus=2 --memory=3g \
  -v /absolute/path/to/disposable-checkout:/app \
  operator-zero-regression:local
```

### Corrected regression expectations (2026-09-05)

The greeting-input test now asserts byte-for-byte preservation of caller speech,
matching the intentional behavior introduced in `bc4a7d0c`; separate existing
tests still require protected greetings to resist cancellation and playback
flushes. This corrects the old silence expectation without changing production
behavior. The outbound-handoff test now requires the exact stored action plus
`armed_user_turn_count=2`, preserving the existing stale-transfer cancellation
contract. Profile-lookup tests exercise the real SQLite lookup instead of the
tool fixture's stub, including rejection of absent numbers and inactive agents.
The ARI startup/reconnect test uses its own temporary agent database so repeat
runs cannot read a malformed database left by unrelated tests. The test image
trusts only `/app` for Git ownership checks on the host-mounted disposable source.

Validation on the MiniPC against the current working-tree source:

- Initial runtime-image run: **2370 passed, 22 skipped, 10 failed**.
- Corrected expectations and system tools: **2379 passed, 22 skipped, 1 failed**
  (Git checkout ownership).
- Scoped Git trust: **2379 passed, 22 skipped, 1 failed** (ARI startup test read
  a database left by the previous run).
- Isolated startup-test database: **2380 passed, 22 skipped, 0 failed**, with
  81 warnings in 63.01 seconds; `./test-operator-zero` exited successfully.

Tests ran with networking disabled in a disposable container. Live services were
not changed. The 22 skipped tests and real-phone checks remain outside this pass.


## Private-announcement cutoff and delayed transcripts

The incoming Operator Zero predial flow now treats a request to play the private
announcement as a terminal handoff boundary. If the inside party hangs up during
playback, playback fails, or the bridge fails afterward, the outside caller is
hung up without a return to the AI agent or voicemail. Unanswered calls before
that boundary retain the existing voicemail route. Trust still requires both
completed playback and successful bridging. This is an intentional behavior
change requested after the September 5 evening call test.

OpenAI can emit a transfer tool request before the corresponding caller
transcript. The provider now waits up to three seconds for outstanding input
transcripts to reach engine history before an incoming screening transfer is
validated and armed. This preserves the screening evidence check and prevents
that same delayed transcript from being counted as a newer caller request.
Transcription errors/timeouts fail closed; a fresh caller turn can retry. Real
new caller turns still invalidate stale pending transfers. Shutdown/reconnect
invalidate the pending transcript waiters.

Regression cases cover delayed transcription through validation and deferred
commit, missing/failed/empty transcripts, retry after timeout, changed turns,
wait cancellation, household hangup after playback starts, playback failure,
late playback success after hangup, and bridge failure. Real-phone validation
must check first-attempt transfer, interrupted private announcements, completed
handoffs, and unanswered-call voicemail.

The isolated MiniPC full-suite runs for this change passed first with **2389
passed, 22 skipped**, then with **2391 passed, 22 skipped, 85 warnings** in
63.08 seconds after adding timeout recovery and cancellation coverage. Python
compilation and `git diff --check` also passed. The engine and Realtime provider
files were deployed after an idle-call check; a rollback copy was retained and
startup health confirmed ARI connectivity and the AudioSocket listener. The
real-phone checks above remain outstanding after deployment.

## Source synchronization (2026-09-07)

Before publishing the outstanding hardening changes, the full
`./test-operator-zero` gate ran again in the isolated MiniPC test image,
with networking disabled and no production credentials or runtime data:
**2391 passed, 22 skipped, 85 warnings in 63.53 seconds**. The first
container invocation failed before pytest because of duplicated shell
arguments; the corrected invocation above completed successfully.

A file-hash comparison confirmed that the deployed AVA runtime source
already matched this working copy. Synchronization publishes that source
and brings deployment documentation and tests up to date; it introduces
no additional call-control behavior change. Real-phone validation and the
22 skipped tests remain outside this automated result.
