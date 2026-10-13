# Lean verification

Run from the repository root:

```bash
python -m pip install -r requirements-test.txt
python tools/check_lean.py
```

Use the repository virtual environment if it is already configured. Python
3.12 or later, libsodium, and [elan](https://github.com/leanprover/elan) are
required. On macOS, `brew install elan libsodium` provides the native tools;
on Ubuntu, install `libsodium-dev` and elan. The runner finds `lake` on PATH
or in `~/.elan/bin`. Elan downloads the version pinned in `lean-toolchain`
on first use. Subsequent runs work offline with that toolchain installed.

The runner builds the proofs and executable model through
[Lake](https://lean-lang.org/doc/reference/latest/Build-Tools-and-Distribution/Lake/),
then checks 10,278 deterministic vectors against the actual Python functions
in `superlink.decoder` and `superlink.appmsg`. Missing tools, failed proofs,
malformed vectors, missing vector categories, and mismatches fail the command.
The CI job runs both the Python suite and this command on pull requests and
pushes to `main`. Lean warnings are errors, including unfinished `sorry` proofs.
There are no mathlib or other third-party Lean dependencies.

## Proven properties

`Superlink.lean` proves these properties of its pure model for all typed inputs:

- A header is exactly 10 bytes and parsing recovers every field and the tail.
- Frames round-trip when the opaque encrypted body has at least four bytes;
  shorter bodies following a complete header are rejected.
- A nonce is exactly 24 bytes: the original 10-byte header, 13 zero bytes,
  and the wrapped counter byte. Adding 256 to an integer preserves that byte,
  including for negative integers. Distinct headers cannot produce equal nonces.
- Application-message encoding preserves ID, wrapped tag, and payload, and
  adds exactly two bytes to the payload length.
- Property requests reject out-of-range integers and preserve the complete
  list of valid byte IDs, including undefined IDs, duplicates, and ordering.

## Python correspondence

`Main.lean` emits vectors directly from the same definitions used in the proofs.
`tools/check_lean.py` compares them to production code:

| Area | Coverage |
| --- | --- |
| Nonces and frame building | 3,586 cases: every byte value in each header position; counters -513 through 512; real encryption/decryption with the model nonce |
| Frame parsing | 2,563 cases: every header byte value, all 256 Dctrl classifications, varied opaque bodies, and larger lengths |
| Short frames | Every raw length from 0 through 13 |
| Application messages | 3,850 cases: ping, reboot, factory reset, locate, and device-info request; tags -257 through 512 |
| Property requests | 265 cases: all 256 byte IDs, empty/duplicate/undefined IDs, negative and overflowing IDs |

To extend coverage, add the model and relevant theorems, emit vectors from that
model, then compare them with the production function in the Python runner.
Update its expected category counts intentionally when adding cases.

## Scope and limits

These are proofs of the Lean model plus finite differential tests of Python,
not a formal proof of the Python interpreter or the complete gateway. Header
fields are bytes and MAC addresses are exactly six bytes in this model;
malformed Python argument types and MAC lengths are outside its domain.
Frame ciphertext is opaque. The encryption correspondence check uses libsodium
with synthetic data and does not prove cryptographic security or MIC validation.

Gateway/session transitions, replay protection, counter uniqueness across
sessions, property-report parsing, adoption, MQTT delivery, persistence, timing,
RF, firmware, and live-device interoperability remain outside this initial
verification layer. Factory-reset encoding tests cover wire bytes only; they
do not establish that a device accepts a zero tag. No packets are transmitted.
