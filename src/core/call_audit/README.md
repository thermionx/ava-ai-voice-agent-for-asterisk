# Inbound audit and history — Phases 2–4

This is a passive, opt-in backend. It is not deployed or enabled by default.
Recording, transcription persistence and `/history` integration are staged
locally. Retention execution and production installation remain Phase 5.

## Identity and lifecycle

The original inbound Asterisk UNIQUEID is the canonical call ID, matching AVA's
existing session identity. Positive inbound context evidence is required;
`PJSIP/twilio-` alone also matches outbound legs and is insufficient. Auxiliary
channels cannot create inbound records. The dialplan saves the called number
before DID aliases, plus LINKEDID, optional Twilio Call SID, and SIP Call-ID.
No external SID is synthesized when the SIP header is absent.

ARI observations are registered independently of AI-session initialization.
Channel creation, answer, original metadata, and destruction can therefore be
observed before Stasis or before a session exists. Leaving Stasis ends only the
AI session: a transferred telephone call continues until its original channel
ends. AudioSocket and inside-phone legs are linked to the original ID.

The durable native CEL JSON journal is the recovery source when AVA is down.
The worker imports `CHAN_START`, answer, original metadata markers, bridge and
hangup events. CEL must be enabled using the version-specific templates in the
Operator Zero repository. An existing simple CDR is not enough to reconstruct
all early unanswered calls. No additional SIP/RTP transport is introduced.

## Failure isolation

Engine hooks only put small observations on a bounded queue. A dedicated
thread writes and fsyncs a private journal; the separate worker performs SQLite
operations. Neither performs disk or network I/O on the telephone event loop.
Database errors leave ingestion checkpoints unchanged and are retried.
Disk failure retries off the call path; queue overflow drops observations and
logs a rate-limited warning. Native CEL provides independent lifecycle evidence,
but catastrophic disk failure or loss of an unflushed queue can leave gaps.
The backend does not claim lossless capture when every durable store fails.

Event IDs dedupe journal replay. Checkpoints follow files across rename rotation;
copied and gzip logs dedupe by event ID. Partial last records wait for completion.
Malformed records are logged and retained in their source file while later
records continue to ingest. Nothing automatically deletes journals or metadata.

## Persistence

`schema.sql` is migration version 1. Initialization uses additive `CREATE IF NOT
EXISTS` and records the version; newer schemas are rejected. The SQLite DB uses
WAL, short lock timeouts, foreign keys, and mode 600. No changes are made to the
existing Call History or household-memory databases. Directory creation and
permissions must be arranged before enabling the engine publisher.

Tables: `inbound_calls`, `call_events`, `call_channel_links`,
`call_transcript_segments`, `call_recordings`, `audit_jobs`,
`ingestion_checkpoints`, and `audit_schema_migrations`.

Phase 3 populates the existing transcript/recording/job tables without a schema
version change. Lifecycle projections can
be enriched after hangup, and late/out-of-order events do not reopen a call.

`caller_spoke` is nullable: unknown differs from an explicit no-speech result.
Durations stay null when the start is unavailable instead of inventing zero.
`duration_seconds` covers received-to-ended; `answered_duration_seconds` covers
answered-to-ended. All timestamps are UTC with microsecond representation.

## Attribution and classification

The audit never controls a conversation or invokes a telephony action.
`outcomes.py` classifies only recorded evidence. A hangup request alone is not
proof of termination. The ARI client records 2xx acceptance separately from
404 (already gone), preserving its existing return values and cleanup behavior.
The request timestamp is retained because the HTTP response can follow the
channel-destruction event. CEL hangup source identifies the caller-side channel
or a dialplan hangup where available. Cause 16 alone leaves initiator unknown.
Caller-side termination does not establish a human's intent or rule out an
upstream carrier issue. Remote SIP/network categories require explicit evidence.

The first provider audio and caller-facing greeting drain are passively observed.
Generated text is not proof that all of it was heard: a disconnect may truncate
playback. The original recording is the reference for what actually reached
the channel. Live greeting-boundary accuracy still requires deployment testing.

## Configuration and local verification

```
CALL_AUDIT_ENABLED=false
CALL_AUDIT_DB_PATH=/app/data/call_audit.db
CALL_AUDIT_JOURNAL_DIR=/app/data/call-audit-journal
CALL_AUDIT_CEL_PATH=/mnt/asterisk_cel/operator-zero.jsonl
CALL_RECORDING_ENABLED=false
CALL_RECORDING_DIR=/mnt/asterisk_recordings/operator-zero
CALL_TRANSCRIPTION_ENABLED=false
CALL_TRANSCRIPTION_PROVIDER=openai
CALL_RECORDING_RETENTION_DAYS=30
CALL_METADATA_RETENTION_DAYS=365
```

The engine enable flag and the native `OZ_CALL_AUDIT_ENABLED` dialplan global
must be enabled together during deployment. Worker paths can also be supplied
as CLI arguments. A worker can replay journals while the publisher is stopped.

From the AVA checkout, with synthetic journals in temporary directories:

```sh
python3 -m src.core.call_audit.worker --once \
  --db /tmp/oz-audit-check/audit.db \
  --journal-dir /tmp/oz-audit-check/journal \
  --cel-path /tmp/oz-audit-check/cel/operator-zero.jsonl
./test-operator-zero tests/test_call_audit.py
./test-operator-zero
```

The worker uses only Python's standard library. Production deployment will
provide a dedicated worker service, read-only access to CEL logs, access to the
engine journal, and write access to the separate audit database. It requires
no ARI/AMI credentials or call-control privileges. Do not add a public HTTP
listener or expose journal files as static content.

No retention timer or delete command is installed in Phases 2–3. Later phases
will add configurable recording/metadata retention with dry-run-first cleanup.

## Recording and transcript persistence

One MixMonitor attaches to the original inbound channel before routing, with
mixed, caller/receive, and sent/transmit mono WAV files. It does not answer the
phone or add another RTP/AudioSocket connection. The `b` option is deliberately
absent so recording includes unbridged audio; default receive/transmit silence
synchronization is retained. No command callback, shell, AGI or HTTP runs from
the recorder. A guarded hangup handler stops only its saved MixMonitor ID.
`TryExec` failures create CEL failure markers and return to normal routing.
Success means the application returned; the media worker separately checks files.

Configure the Asterisk globals `OZ_CALL_RECORDING_ENABLED` and
`OZ_CALL_RECORDING_DIR` alongside the matching AVA flags. Filenames contain only
the validated Asterisk ID, never caller names/numbers:
`<call_id>-mixed.wav`, `<call_id>-caller.wav`, `<call_id>-sent.wav`.
The transmit track can contain the inside phone, announcements or voicemail
after transfer; it must not be labeled exclusively as Operator Zero speech.

The media worker validates ended calls only. With a stop marker, files must be
unchanged for five seconds; without one, a stable complete WAV must be at least
30 seconds old. Truncated/empty/unsupported files remain unavailable and retry.
An Asterisk crash that leaves an invalid WAV header is recorded as a failure;
we preserve that file but do not claim it is playable or silently repair it.
Paths stored in `call_recordings` are relative names. Resolution requires the
configured root and rejects traversal/symlinks. Phase 4 adds authenticated
playback in the existing admin UI; this worker does not add a public endpoint.

The realtime observer snapshots caller partial/final text and exact generated
assistant text before normal provider event handling. Separate speaker/item
IDs and revisions allow late finals to replace partials without erasing other
speakers. Snapshots already enter the durable journal when received; session
shutdown neither waits for another conversational turn nor changes termination.
The provider is still closed promptly. Words for which no provider event arrived
are recovered from the recording, rather than keeping a terminated conversation
alive to flush transcription. This also covers greeting input intentionally
cleared by the existing greeting transport guard.

Realtime timings are approximate event-receipt bounds. Fallback transcription
uses recording segment timestamps plus the answer-time anchor; this assumes
recorded media starts at answer and must be verified against real Asterisk
pre-answer behavior in Phase 5. We retain the original recording and label that
timing source approximate. No model log-probability is invented as confidence;
confidence stays null unless a replacement adapter supplies it.

Finalized caller utterances with speech bounds are reused. Recovery covers
uncovered greeting, gaps, partials and the final tail, with padding at approximate
boundaries. It transcribes only receive audio, never the mixed/sent track.
Identical nearby recognized text is deduplicated; distinct partial hypotheses
remain identifiable as partials alongside recovered final text. Generated AI
words are preserved directly. Digital silence skips API work. An empty ASR
result on nonzero audio does not prove the caller never spoke.

`transcription.Transcriber` is the replaceable interface. The initial adapter
uses the existing `openai` dependency, `OPENAI_API_KEY`, and `whisper-1` with
verbose segment timestamps. Requests have a 60-second timeout and no SDK retry;
durable jobs retry with backoff from 10 seconds to one hour. Five-minute chunks
bound memory/uploads; completed chunks and their transcripts commit atomically,
so restarting resumes unfinished work. Run one media worker per audit database.
An API request completed immediately before a database failure can be repeated;
there is no distributed exactly-once billing guarantee. No live API calls are
made by the automated tests.

Run the media worker as a **separate process** from lifecycle ingestion:

```sh
python3 -m src.core.call_audit.media --once
python3 -m src.core.call_audit.media
./test-operator-zero tests/test_call_audit.py tests/test_call_audit_media.py
```

Both audit and recording flags must be enabled for this CLI to do work.
Disabling transcription stops new text snapshots and post-call API work; it
does not delete existing transcripts. Existing completed recordings remain
available. Recording disablement must also be applied to the Asterisk global;
changing a container variable alone cannot stop a native Asterisk recorder.
Retry state is visible in `audit_jobs`; failures appear separately in events/logs.
No file deletion, cleanup timer or retention enforcement runs yet.

Deployment must provide: a private Asterisk-writable recording directory;
read-only access for the media worker and authenticated admin UI; writable
audit DB access for the worker; the existing Python/OpenAI requirements and
secret injection; and separate lifecycle/media services. Recording files and
production databases must stay outside Git. See the Operator Zero deployment
document for the staged host paths and mandatory live validation checklist.

References: [Asterisk 22 MixMonitor](https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/Dialplan_Applications/MixMonitor/),
[OpenAI audio API](https://developers.openai.com/api/reference/resources/audio),
[realtime transcription](https://developers.openai.com/api/docs/guides/realtime-transcription).

## Changed files through Phase 3

All changes are local, uncommitted and undeployed at this review gate. The
private configuration repository is untouched. No UI or routing specification
has changed; deployment and real-call verification remain Phase 5.

AVA repository (paths relative to the checkout):

| File | Purpose |
| --- | --- |
| `.env.example` | Opt-in audit/media flags, paths, provider and reserved retention settings. |
| `.gitignore` | Keep journals, production DBs and recordings out of Git. |
| `src/ari_client.py` | Phase 2 passive hangup request/acceptance evidence; unchanged in Phase 3. |
| `src/engine.py` | Phase 2 lifecycle hooks plus confirmed greeting-drain observation. |
| `src/providers/openai_realtime.py` | Observe text before processing and release local snapshot state at shutdown. |
| `src/core/call_audit/__init__.py` | Isolated audit package; unchanged in Phase 3. |
| `src/core/call_audit/events.py` | Versioned envelopes, now including media/transcript events. |
| `src/core/call_audit/ari.py` | Phase 2 inbound ARI normalization; unchanged in Phase 3. |
| `src/core/call_audit/cel.py` | Native lifecycle and recording-marker normalization. |
| `src/core/call_audit/outcomes.py` | Phase 2 evidence-based outcomes; unchanged in Phase 3. |
| `src/core/call_audit/publisher.py` | Phase 2 bounded asynchronous journal; unchanged in Phase 3. |
| `src/core/call_audit/schema.sql` | Phase 2 version-1 schema, reused without migration in Phase 3. |
| `src/core/call_audit/store.py` | Lifecycle plus speaker-separated transcript projections and recording queries. |
| `src/core/call_audit/worker.py` | Phase 2 independent journal replay; unchanged in Phase 3. |
| `src/core/call_audit/realtime.py` | New passive caller/assistant partial/final snapshots and speech observations. |
| `src/core/call_audit/transcription.py` | New replaceable caller-recording transcription adapter. |
| `src/core/call_audit/media.py` | New stable-file validation and durable post-call recovery jobs. |
| `src/core/call_audit/README.md` | Architecture, configuration, limitations and this file inventory. |
| `tests/test_call_audit.py` | 39 Phase 2 lifecycle/failure tests; unchanged in Phase 3. |
| `tests/test_call_audit_media.py` | Phase 3 short-call, transcript, recording, retry and isolation tests. |

Operator Zero repository:

| File | Purpose |
| --- | --- |
| `asterisk/extensions-operator-zero.conf` | Disabled audit/recording Gosub and recorder-specific hangup handler. |
| `asterisk/cel-operator-zero.conf.example` | Phase 2 native event selection; unchanged in Phase 3. |
| `asterisk/cel-custom-operator-zero.conf.example` | Phase 2 JSON serializer mapping; unchanged in Phase 3. |
| `docs/inbound-call-audit.md` | Reproducible deployment requirements and mandatory live validation. |

## Phase 3 validation result

Workstation Python 3.14, synthetic/anonymized fixtures and mocked transcription
API responses; no production audio or paid API requests were used.

| Check | Result |
| --- | --- |
| Initial sandboxed Phase 2 tests | Stopped after local socket restrictions prevented valid async execution. |
| Audit lifecycle/media focused suite | 65 passed before the final additional recovery cases. |
| First full suite | 2,479 passed, 11 skipped, 1 baseline failure. |
| Full suite after recovery cases | 2,486 passed, 11 skipped, 1 baseline failure. |
| Final full suite after drain-evidence fix | 2,488 passed, 11 skipped, 1 baseline failure; all 74 audit tests passed. |
| `git diff --check`, both changed repositories | Passed. |

The unchanged failing test is
`tests/test_local_ai_output_generation.py::test_session_response_task_can_be_cancelled_without_waiting_for_work`.
Phase 2 reproduced that same failure in an untouched HEAD checkout with this
Python environment. It concerns cleanup of a cancelled local-AI response task;
this change does not edit that implementation or weaken its test.

Final full output is saved on the workstation at
`/tmp/operator-zero-audit-phase3-regression-final.log`.
Real recognition quality, Asterisk pre-answer audio attachment/timing, track
direction, trusted/transfer/voicemail behavior, permissions and live early-hangup
capture still require Phase 5 mini PC testing. No Asterisk validation/reload,
service restart, UI change, push or deployment occurred in this phase.

## Phase 4: existing `/history` integration

The existing AVA history route hosts two views:

- **Inbound calls**: the complete audit, including calls that never established
  an AI session. This becomes the default when audit is enabled or its DB exists.
- **AI sessions**: the existing diagnostics, statistics, exports and tools view.
  Existing `/history?id=<record_id>` links retain their behavior. If audit status
  cannot be read, the existing view stays available.

Inbound links use `/history?view=inbound`; each call links to
`/history?view=inbound&call_id=<original_asterisk_uniqueid>`. The main view shows
newest calls first, caller number/name, original called number, duration, outcome
and a caller-first transcript preview. Search includes numbers, names and
transcript text. Outcome filters and pagination are server-side. Visible pages
refresh every 15 seconds and have a manual refresh button.

Call details show answer/start/end times, AI-session and speech evidence,
disconnect initiator/cause, external IDs, speaker-separated partial/final
transcripts, a millisecond event timeline and mixed/caller/sent recording controls.
Unknown speech/duration remain unknown. Generated assistant text is labeled as
potentially truncated in playback; partial hypotheses remain visibly partial.
The caller hangup annotation requires explicit caller-side evidence.

New routes under the existing `/api` mount:

```
GET /call-audit/status
GET /call-audit/calls?page=1&page_size=20&search=...&outcome=...
GET /call-audit/calls/{call_id}
GET /call-audit/calls/{call_id}/recordings/{mixed|caller|sent}
```

The router directly requires the existing JWT/current-user dependency, including
the first-login password-change gate; mounting it independently does not bypass
authentication. The UI uses Axios with its existing bearer header and a temporary
blob URL for the audio control. It aborts pending audio requests and revokes blob
URLs on track changes/navigation. Tokens never enter recording URLs.

The API opens SQLite in read-only/query-only mode and never initializes the DB.
Missing/unavailable databases return a generic 503 without filesystem paths.
JSON and audio use private/no-store caching. Audio is streamed from an open
regular-file descriptor; symlinks, FIFOs, mismatched DB filenames, unknown tracks
and traversal are rejected. Single HTTP byte ranges support seeking. The browser
gets no host path, including through the legacy recording-info endpoint (its
compatible `file_path` field is now null). Timeline details use an allowlist rather
than exposing arbitrary journal payloads.

Deployment adds **no new package dependency or web service** for Phase 4. The
existing admin UI needs read access to `CALL_AUDIT_DB_PATH`, its SQLite WAL/SHM
siblings and `CALL_RECORDING_DIR`, and the matching environment values. It needs
neither transcription credentials nor recording write access for these routes.
Render/build and recreate only the existing admin UI when Phase 5 deploys it.
Live browser audio, host permissions and mini PC integration remain unverified
until that deployment gate; automated playback checks use synthetic WAV data.

### Phase 4 changed files

The Phase 2–3 inventory above remains applicable. This phase changes/adds only:

| File | Purpose |
| --- | --- |
| `admin_ui/backend/api/call_audit.py` | New authenticated, read-only audit queries and controlled recording streaming. |
| `admin_ui/backend/api/calls.py` | Mount audit routes and stop returning legacy recording host paths. |
| `admin_ui/frontend/src/components/calls/InboundCallHistory.tsx` | Inbound list, details, timeline and recording controls. |
| `admin_ui/frontend/src/components/calls/InboundCallHistory.test.tsx` | Search/filter, short-call, deep-link, playback and failure-state UI coverage. |
| `admin_ui/frontend/src/pages/CallHistoryPage.tsx` | Host inbound/AI-session views and remove the host-path tooltip. |
| `tests/test_admin_call_audit.py` | Authentication, read-only storage, short-call queries, ranges and path-safety tests. |
| `src/core/call_audit/README.md` | Routes, deployment requirements, inventory and validation notes. |
| Operator Zero `docs/inbound-call-audit.md` | Existing-UI deployment/access requirements. |

### Phase 4 validation results

| Check | Result |
| --- | --- |
| Initial audit API tests | 14 passed. |
| Focused inbound/existing history UI tests | 13 passed (8 new, 5 existing). |
| Full UI suite, before and after final adjustments | 318 passed across 44 files, both runs. |
| Production UI builds | Passed before and after final adjustments; existing bundle-size advisory remains. |
| First full backend suite | 2,502 passed, 11 skipped, 1 baseline cancellation failure. |
| Final full backend suite | 2,503 passed, 11 skipped, 1 baseline cancellation failure; all 15 audit API tests passed. |
| Final TypeScript `--noEmit` | 57 errors, matching untouched HEAD exactly after normalizing paths, line numbers and union-member order; no new diagnostics. |
| Changed UI files ESLint | Zero errors; 13 warnings in existing history code, none in new components/tests. |
| Both repositories `git diff --check` | Passed. |

The baseline backend failure is the same local-AI task-cancellation test named
in Phase 3 above. No tests were weakened or existing production diagnostics
refactored to hide baseline failures. Two new TypeScript compatibility errors
and one new lint warning found during development were fixed before the final
checks. Node 20.19.0 was downloaded from the official distribution, its SHA256
verified, and used only from `/tmp`; `npm ci` used the existing lockfile unchanged.
No dependency versions, service configuration or call-handling code changed in
Phase 4. No interactive browser listening test or mini PC validation was run.

Workstation logs:

- `/tmp/operator-zero-audit-phase4-regression-final.log`
- `/tmp/operator-zero-history-ui-full-tests-final.log`
- `/tmp/operator-zero-history-ui-build-final.log`
- `/tmp/operator-zero-history-ui-types-final.log`
- `/tmp/operator-zero-history-ui-types-baseline.log`
- `/tmp/operator-zero-history-ui-lint-final.log`
