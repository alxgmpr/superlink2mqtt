import Std

/-!
Pure wire-format model for decoder.py and appmsg.py. Ciphertext is opaque:
these proofs do not model libsodium, MIC authentication, or radio behavior.
-/
namespace Superlink

structure Header where
  mctrl : UInt8
  dctrl : UInt8
  mac0 : UInt8
  mac1 : UInt8
  mac2 : UInt8
  mac3 : UInt8
  mac4 : UInt8
  mac5 : UInt8
  seqHi : UInt8
  seqLo : UInt8
  deriving DecidableEq, Repr

def Header.bytes (h : Header) : List UInt8 :=
  [h.mctrl, h.dctrl, h.mac0, h.mac1, h.mac2, h.mac3, h.mac4, h.mac5,
   h.seqHi, h.seqLo]

def parseHeader : List UInt8 → Option (Header × List UInt8)
  | m :: d :: a :: b :: c :: e :: f :: g :: hi :: lo :: tail =>
    some (⟨m, d, a, b, c, e, f, g, hi, lo⟩, tail)
  | _ => none

def parseFrame (raw : List UInt8) : Option (Header × List UInt8) := do
  let (header, encrypted) ← parseHeader raw
  if encrypted.length ≥ 4 then some (header, encrypted) else none

theorem header_length (h : Header) : h.bytes.length = 10 := by
  rfl

theorem header_roundtrip (h : Header) (tail : List UInt8) :
    parseHeader (h.bytes ++ tail) = some (h, tail) := by
  cases h
  rfl

theorem header_bytes_injective {a b : Header} (same : a.bytes = b.bytes) :
    a = b := by
  have parsed := congrArg (fun bytes => parseHeader (bytes ++ [])) same
  simp only [header_roundtrip] at parsed
  exact congrArg Prod.fst (Option.some.inj parsed)

theorem frame_roundtrip (h : Header) (encrypted : List UInt8)
    (enough : encrypted.length ≥ 4) :
    parseFrame (h.bytes ++ encrypted) = some (h, encrypted) := by
  simp [parseFrame, header_roundtrip, enough]

theorem frame_rejects_short_body (h : Header) (encrypted : List UInt8)
    (short : encrypted.length < 4) :
    parseFrame (h.bytes ++ encrypted) = none := by
  simp [parseFrame, header_roundtrip, show ¬ encrypted.length ≥ 4 by omega]

-- Python's `n & 0xff` also wraps negative integers.
def byte (n : Int) : UInt8 := UInt8.ofNat (n % 256).toNat

def nonce (h : Header) (counter : Int) : List UInt8 :=
  h.bytes ++ List.replicate 13 0 ++ [byte counter]

theorem nonce_length (h : Header) (counter : Int) :
    (nonce h counter).length = 24 := by
  simp [nonce, header_length]

theorem nonce_preserves_header (h : Header) (counter : Int) :
    (nonce h counter).take 10 = h.bytes := by
  simp [nonce, Header.bytes]

theorem nonce_padding (h : Header) (counter : Int) :
    ((nonce h counter).drop 10).take 13 = List.replicate 13 0 := by
  simp [nonce, Header.bytes]

theorem nonce_counter (h : Header) (counter : Int) :
    (nonce h counter).drop 23 = [byte counter] := by
  simp [nonce, Header.bytes, List.replicate]

theorem nonce_equal_implies_header_equal (a b : Header) (ca cb : Int)
    (same : nonce a ca = nonce b cb) : a = b := by
  apply header_bytes_injective
  have prefixes := congrArg (List.take 10) same
  simpa only [nonce_preserves_header] using prefixes

theorem byte_periodic (n : Int) : byte (n + 256) = byte n := by
  simp [byte]

def classify (dctrl : UInt8) : String × String :=
  match dctrl.toNat with
  | 0x40 => ("UL", "discovery")
  | 0x42 => ("UL", "conn")
  | 0x43 => ("DL", "mgmt-ack")
  | 0x44 => ("UL", "setup")
  | 0x53 => ("DL", "mgmt-dl")
  | 0x54 => ("UL", "data")
  | 0x62 => ("DL", "conn-rsp")
  | 0x63 => ("DL", "data")
  | 0x74 => ("DL", "setup-rsp")
  | _ => ("??", "unknown")

def message (id : UInt8) (tag : Int) (body : List UInt8) : List UInt8 :=
  id :: byte tag :: body

def parseMessage : List UInt8 → Option (UInt8 × UInt8 × List UInt8)
  | id :: tag :: body => some (id, tag, body)
  | _ => none

theorem message_roundtrip (id : UInt8) (tag : Int) (body : List UInt8) :
    parseMessage (message id tag body) = some (id, byte tag, body) := by
  rfl

theorem message_length (id : UInt8) (tag : Int) (body : List UInt8) :
    (message id tag body).length = body.length + 2 := by
  simp [message]

-- Undefined property IDs are intentionally allowed; only byte range is checked.
def propertyRequest (ids : List Int) (tag : Int) : Option (List UInt8) :=
  if ids.all (fun i => 0 ≤ i && i < 256) then
    some (message 11 tag (ids.map byte))
  else none

theorem property_request_rejects_out_of_range (ids : List Int) (tag : Int)
    (invalid : ids.all (fun i => 0 ≤ i && i < 256) = false) :
    propertyRequest ids tag = none := by
  simp [propertyRequest, invalid]

theorem property_request_roundtrip (ids : List Int) (tag : Int)
    (valid : ids.all (fun i => 0 ≤ i && i < 256) = true) :
    (propertyRequest ids tag).bind parseMessage =
      some (11, byte tag, ids.map byte) := by
  simp [propertyRequest, valid, message_roundtrip]

end Superlink
