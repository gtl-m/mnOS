"""Read GGUF metadata and tensors for the bundled MiniMind2 model."""

import struct
import numpy as np

GGML_TYPE = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 6: "Q5_0", 7: "Q5_1",
    8: "Q8_0", 9: "Q8_1", 10: "Q2_K", 11: "Q3_K", 12: "Q4_K",
    13: "Q5_K", 14: "Q6_K", 15: "Q8_K",
}
SZ = {0: 4, 1: 2}

def load_gguf(path):
    """Return (metadata, tensors, tensor types, tensor dimensions) from a GGUF file."""
    with open(path, "rb") as f:
        fb = f.read()
    p = 0
    def take(n):
        nonlocal p
        b = fb[p:p+n]; p += n
        return b
    def u8(): return take(1)[0]
    def i8(): return struct.unpack("<b", take(1))[0]
    def u16(): return struct.unpack("<H", take(2))[0]
    def i16(): return struct.unpack("<h", take(2))[0]
    def u32(): return struct.unpack("<I", take(4))[0]
    def i32(): return struct.unpack("<i", take(4))[0]
    def u64(): return struct.unpack("<Q", take(8))[0]
    def f32(): return struct.unpack("<f", take(4))[0]
    def f16(): return struct.unpack("<e", take(2))[0]
    magic = take(4)
    ver = u32()
    u64len = ver >= 2
    def LEN():
        return u64() if u64len else u32()
    def rstr():
        return take(LEN()).decode("utf-8", "replace")
    def rstr_u32():
        return take(u32()).decode("utf-8", "replace")
    # Metadata entries and tensor descriptors precede the tensor data.
    n_tensors = LEN()
    n_kv = LEN()
    info = {}
    for _ in range(n_kv):
        k = rstr()
        t = u32()
        if t == 9:
            at = u32(); n = LEN()
            vals = []
            for _ in range(n):
                if at == 6: vals.append(f32())
                elif at == 13: vals.append(f16())
                elif at == 8: vals.append(rstr())
                elif at == 4: vals.append(u32())
                elif at == 5: vals.append(i32())
                elif at == 0: vals.append(u8())
                elif at == 1: vals.append(i8())
                elif at == 2: vals.append(u16())
                elif at == 3: vals.append(i16())
                elif at == 12: vals.append(struct.unpack("<d", take(8))[0])
                else: raise NotImplementedError("arr elem %d" % at)
            info[k] = vals
        elif t == 6: info[k] = f32()
        elif t == 13: info[k] = f16()
        elif t == 14: info[k] = take(2)  # bf16, kept raw
        elif t == 8: info[k] = rstr()
        elif t == 0: info[k] = u8()
        elif t == 1: info[k] = i8()
        elif t == 2: info[k] = u16()
        elif t == 3: info[k] = i16()
        elif t == 4: info[k] = u32()
        elif t == 5: info[k] = i32()
        elif t == 7: info[k] = bool(u8())
        elif t == 10: info[k] = u64()
        elif t == 11: info[k] = struct.unpack("<q", take(8))[0]
        elif t == 12: info[k] = struct.unpack("<d", take(8))[0]
        else: raise NotImplementedError("kv type %d" % t)
    # Tensor dimension widths differ between GGUF v3 and earlier versions.
    dw = 8 if ver >= 3 else 4
    tinf = []
    for _ in range(n_tensors):
        name = rstr()
        nd = u32()
        dims = tuple((u64() if dw == 8 else u32()) for _ in range(nd))
        typ = u32()
        off = u64()
        tinf.append((name, dims, typ, off))
    # This reader assumes the default 32-byte alignment for tensor data.
    data_start = (p + 31) & ~31
    # Storage bytes per element, including packed block-quantized formats.
    QBYTE = {0: 4, 1: 2, 6: 22 / 32, 7: 24 / 32, 8: 34 / 32, 9: 36 / 32,
             10: 72 / 256, 11: 100 / 256, 12: 144 / 256, 13: 176 / 256,
             14: 210 / 256, 15: 290 / 256, 18: 19 / 16}
    ttypes = {}
    tdims = {}
    tensors = {}
    for name, dims, typ, off in tinf:
        ttypes[name] = typ
        tdims[name] = dims
        nelem = 1
        for d in dims: nelem *= d
        qn = QBYTE.get(typ)
        if qn is None:
            raise NotImplementedError("tensor type %d" % typ)
        nbytes = int(round(nelem * qn))
        if typ == 0:
            arr = np.frombuffer(fb, dtype=np.float32, count=nelem,
                                offset=data_start + off).reshape(dims)
        elif typ == 1:
            arr = np.frombuffer(fb, dtype=np.float16, count=nelem,
                                offset=data_start + off).reshape(dims).astype(np.float32)
        else:
            # Quantized tensors remain packed for the model loader to decode.
            arr = np.frombuffer(fb, dtype=np.uint8, count=nbytes,
                                offset=data_start + off)
        tensors[name] = arr
    return info, tensors, ttypes, tdims

if __name__ == "__main__":
    import sys
    # The CLI prints two values; model callers also need type and shape metadata.
    info, tensors, _, _ = load_gguf(sys.argv[1])
    print("--- meta ---")
    for k in sorted(info):
        v = info[k]
        if isinstance(v, list) and len(v) > 20:
            print("  %s: list[%d]" % (k, len(v)))
        else:
            print("  %s: %s" % (k, v if not isinstance(v, str) else v[:80]))
    print("--- tensors ---")
    for name, arr in tensors.items():
        print("  %-26s %s %s" % (name, arr.shape, arr.dtype))