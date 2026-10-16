# Python property tests

Install libsodium and the Python dependencies from `requirements-test.txt`, then
run the complete suite from the repository root:

```bash
python -m pytest tests/ -q
```

The property tests use [Hypothesis](https://hypothesis.readthedocs.io/en/latest/)
against production Python code. The small Lean foundation remains focused on
wire-format properties; see [Lean verification](../verification/lean/README.md).

## Coverage

| Area | Invariants checked |
| --- | --- |
| Radio frames | Short headers/bodies are rejected; accepted frames preserve every input byte |
| Application messages | Arbitrary byte strings produce a result or `ValueError`, without indexing failures |
| Device info | Counted message/property lists must be complete; truncations cannot produce partial metadata |
| Properties | Incomplete known values are not published as complete; unknown IDs retain their opaque tail |
| Factory pairing | v1/v2 discovery, repeated requests, real DH, challenge echo, adoption exchange, and deferred key rotation until commit |
| Invalid peer keys | Rejected challenges and adoption responses cannot crash or partially replace key material; valid retries can still complete |
| Command queues | FIFO order, one emitted command per valid window, and preservation across link loss and watchdog rearm |
| Invalid MIC | Corrupted active-session data and management frames cannot emit events, drain commands, or refresh liveness |
| Counters | Management frames use the MIC-matched counter; valid telemetry beyond the bounded scan can use the verified fallback |
| Reconnect storms | Discovery, short resume, and full challenges respect cooldown, including the exact deadline |
| Recovery | Link-loss grace periods, repeated resume attempts, watchdog boundaries, and reconnect from a `DeviceRecord` preserve operational keys |

`tests/test_packet_properties.py` generates byte strings and structured messages
with truncations. `tests/test_session_properties.py` generates pairing cases and
uses a `RuleBasedStateMachine` to interleave clock advances, queued commands,
valid traffic, corrupted packets, and reconnect attempts. A separate oracle
tracks expected queue contents, liveness, and cooldown state.

The synthetic peer in `tests/support/sensor_peer.py` constructs frames using
hashlib and libsodium independently of the gateway's encoder. All session
transitions under test go through `start`, `feed`, or `tick`; tests do not force
the session into ACTIVE. Cryptographic operations are real. Only entropy is
replaced with a repeatable test source so failure replay and shrinking remain
stable. That replacement is scoped to each test case and never used by runtime
code. Time advances explicitly; tests never sleep or access the radio.

## Profiles and reproducing failures

Profiles are defined in `tests/conftest.py`:

| Profile | Examples per property | Maximum steps per stateful example | Generation |
| --- | --- | --- | --- |
| `dev` (default) | 50 | 30 | Varied, with local failure database |
| `ci` | 100 | 50 | Deterministic for unchanged tests and Hypothesis version |
| `extended` | 500 | 100 | Larger varied run, with local failure database |

These are budgets, not guaranteed counts of distinct packets or transitions.
Hypothesis can finish early when a finite domain is exhausted. Wall-clock test
deadlines are disabled; protocol deadlines are asserted against virtual time.

```bash
HYPOTHESIS_PROFILE=ci python -m pytest tests/ -q --hypothesis-show-statistics
HYPOTHESIS_PROFILE=extended python -m pytest tests/test_packet_properties.py tests/test_session_properties.py -q
python -m pytest tests/test_session_properties.py --hypothesis-seed=12345 -q
```

On failure, Hypothesis prints a reduced input or action sequence. Re-run that
test with the same pinned dependencies; the local `.hypothesis/` database also
retains failing examples for non-deterministic profiles and is ignored by Git.
Promote important failures into explicit examples or focused regression tests
so they do not depend on a particular random run.

CI installs the pinned test dependencies, runs the full Python suite with the
`ci` profile and statistics, then runs the unchanged Lean verification command.

## Limits

These tests exercise software behavior with a synthetic peer. They do not prove
the reverse-engineered protocol matches every sensor firmware, establish radio
delivery, or qualify real reconnect timing. Restart coverage reconstructs from
an in-memory `DeviceRecord`; it does not test power-loss durability of JSON
storage. Command checks stop at emitted frames and do not establish device
acknowledgment, automatic resend after a failed radio TX, or exactly-once
execution. They also do not establish complete handshake authentication,
replay resistance, or nonce uniqueness across sessions.
