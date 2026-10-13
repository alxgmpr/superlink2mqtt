import Superlink

open Superlink

def csv (bytes : List UInt8) : String :=
  String.intercalate "," (bytes.map (toString ∘ UInt8.toNat))

def integers (values : List Int) : String :=
  String.intercalate "," (values.map toString)

-- Each header position independently takes every byte value. The other
-- positions stay distinct to expose swapped fields and incorrect slices.
def sampleHeader (position value : Nat) : Header :=
  let fields := (List.range 10).map fun i =>
    UInt8.ofNat (if i = position then value else i * 23)
  match parseHeader fields with
  | some (h, _) => h
  | none => ⟨0, 0, 0, 0, 0, 0, 0, 0, 0, 0⟩

def emitNonce (out : IO.FS.Stream) (h : Header) (counter : Int) : IO Unit :=
  out.putStrLn s!"nonce\t{csv h.bytes}\t{counter}\t{csv (nonce h counter)}"

def emitFrame (out : IO.FS.Stream) (raw : List UInt8) : IO Unit := do
  match parseFrame raw with
  | none => out.putStrLn s!"short\t{csv raw}"
  | some (h, encrypted) =>
    let (direction, kind) := classify h.dctrl
    out.putStrLn s!"frame\t{csv raw}\t{csv h.bytes}\t{csv encrypted}\t{direction}\t{kind}"

def emitProperty (out : IO.FS.Stream) (ids : List Int) (tag : Int) : IO Unit := do
  let expected := match propertyRequest ids tag with
    | some wire => csv wire
    | none => "reject"
  out.putStrLn s!"property\t{integers ids}\t{tag}\t{expected}"

/-- Deterministic model vectors, consumed by tools/check_lean.py. -/
def main : IO Unit := do
  let out ← IO.getStdout
  for position in [:10] do
    for value in [:256] do
      let h := sampleHeader position value
      emitNonce out h (Int.ofNat value - 128)
      let encrypted := (List.range (4 + value % 33)).map fun i =>
        UInt8.ofNat (i * 73 + value)
      emitFrame out (h.bytes ++ encrypted)
  for n in [:1026] do
    emitNonce out (sampleHeader 0 224) (Int.ofNat n - 513)
  for n in [:14] do
    emitFrame out ((List.range n).map UInt8.ofNat)
  -- Include payload lengths beyond the small boundary sweep.
  for n in [255, 256, 1024] do
    emitFrame out ((sampleHeader 1 84).bytes ++
      (List.range n).map UInt8.ofNat)
  for id in [4, 6, 7, 8, 9] do
    for n in [:770] do
      let tag := Int.ofNat n - 257
      let body := if id = 4 then
        (List.range (n % 33)).map UInt8.ofNat else []
      out.putStrLn s!"message\t{id}\t{tag}\t{csv body}\t{csv (message id tag body)}"
  for id in [:256] do
    emitProperty out [Int.ofNat id] (Int.ofNat id - 128)
  for ids in [[], [0, 18, 43, 255], [1, 1, 255, 0], [-1], [256],
      [1, -1, 2], [1, 256, 2], [-257], [65536]] do
    emitProperty out ids 513
