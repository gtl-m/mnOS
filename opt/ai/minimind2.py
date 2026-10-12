import sys, math, os, argparse, codecs
import numpy as np
import regex
import gguf_reader

GGUF = "q4-minimind2-small.gguf"
DIM = 512
NHEAD = 8
NKV = 2
NLAYER = 8
HEAD_DIM = 64
THETA = 1e6
EPS = 1e-5
MAXCTX = 8192


def bytes_to_unicode():
    bs = list(range(ord("!"), ord("~") + 1)) + list(range(0xA1, 0xAC + 1)) + list(range(0xAE, 0xFF + 1))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return dict(zip(bs, [chr(c) for c in cs]))


def f16_to_f32(raw):
    raw = np.ascontiguousarray(raw).view(np.uint16).astype(np.uint16).view(np.float16)
    return raw.astype(np.float32).reshape(-1)


def dequant(a, typ):
    """Decode supported GGUF tensor encodings to flattened float32 values."""
    if typ == 0:
        return a.astype(np.float32).reshape(-1)
    if typ == 8:  # Q8_0: 32/block
        nb = a.shape[0] // 34
        b = a[: nb * 34].reshape(nb, 34)
        d = f16_to_f32(b[:, 0:2])
        return (b[:, 2:].astype(np.int8) * d[:, None]).astype(np.float32).reshape(-1)
    if typ == 6:  # Q5_0: 32/block
        nb = a.shape[0] // 22
        b = a[: nb * 22].reshape(nb, 22)
        d = f16_to_f32(b[:, 0:2])
        qh = np.ascontiguousarray(b[:, 2:6]).view(np.uint32).reshape(-1)
        qs = b[:, 6:22]
        j = np.arange(16)
        xh0 = ((qh >> j[:, None]) << 4) & 0x10   # (16, nb)
        xh1 = (qh >> (j[:, None] + 12)) & 0x10
        x0 = ((qs & 0x0F).astype(np.int32).T + xh0) - 16
        x1 = ((qs >> 4).astype(np.int32).T + xh1) - 16
        out = np.empty((nb, 32), np.float32)
        out[:, :16] = x0.T * d[:, None]
        out[:, 16:] = x1.T * d[:, None]
        return out.reshape(-1)
    if typ == 12:  # Q4_K: 256/block, 144B
        nb = a.shape[0] // 144
        b = a[: nb * 144].reshape(nb, 144)
        d = f16_to_f32(b[:, 0:2])
        dmin = f16_to_f32(b[:, 2:4])
        scales = b[:, 4:16]
        qs = b[:, 16:144]
        g = np.arange(256) // 32
        pos32 = np.arange(256) & 31
        byte = (np.arange(256) // 64) * 32 + pos32
        isl = g
        sc = np.where(isl < 4,
                      (scales[:, isl] & 63).astype(np.int16),
                      ((scales[:, isl + 4] & 0xF) | ((scales[:, isl - 4] >> 6) << 4)).astype(np.int16))
        m = np.where(isl < 4,
                     (scales[:, isl + 4] & 63).astype(np.int16),
                     ((scales[:, isl + 4] >> 4) | ((scales[:, isl] >> 6) << 4)).astype(np.int16))
        nib = np.where((isl % 2) == 0,
                       qs[:, byte] & 0xF,
                       qs[:, byte] >> 4).astype(np.int16)
        return ((d[:, None] * sc) * nib - (dmin[:, None] * m)).astype(np.float32).reshape(-1)
    if typ == 14:  # Q6_K: 256/block, 210B
        nb = a.shape[0] // 210
        b = a[: nb * 210].reshape(nb, 210)
        ql = b[:, 0:128]
        qh = b[:, 128:192]
        sc8 = b[:, 192:208].astype(np.int8)
        d = f16_to_f32(b[:, 208:210])
        n = np.arange(256) // 128
        l = np.arange(256) & 31
        q = (np.arange(256) & 127) // 32
        byteql = n * 64 + l + np.where((q == 1) | (q == 3), 32, 0)
        byteqh = n * 32 + l
        sc_idx = n * 8 + (l // 16) + q * 2
        nib = np.where(q < 2, ql[:, byteql] & 0xF, ql[:, byteql] >> 4)
        hi = (qh[:, byteqh] >> (q * 2)) & 3
        qv = (nib.astype(np.int16) | (hi * 16)) - 32
        return (d[:, None] * sc8[:, sc_idx] * qv).astype(np.float32).reshape(-1)
    raise NotImplementedError("dequant type %d" % typ)


class MiniMind:
    """Load GGUF model weights for the CPU inference implementation."""

    def __init__(self, path):
        # Tensor type and original shape metadata are needed to decode each weight.
        info, tens, ttypes, tdims = gguf_reader.load_gguf(path)
        self.info = info
        self.w = {}
        for name, raw in tens.items():
            dims = tdims[name]
            if ttypes[name] == 0:
                flat = raw.astype(np.float32).reshape(-1)
            elif ttypes[name] == 1:
                flat = raw.astype(np.float32).reshape(-1)
            else:
                flat = dequant(raw, ttypes[name])
            if len(dims) == 2:
                # Restore the matrix axis order expected by the inference matmuls.
                self.w[name] = flat.reshape((dims[1], dims[0]))   # (out, in)
            else:
                self.w[name] = flat.reshape(dims)
        self._build_tokenizer()
        self._prep_rope()

    def _build_tokenizer(self):
        tok = self.info["tokenizer.ggml.tokens"]
        self.vocab = tok
        self.id2tok = {i: t for i, t in enumerate(tok)}
        self.tok2id = {t: i for i, t in enumerate(tok)}
        self.byte_enc = bytes_to_unicode()
        self.byte_dec = {v: k for k, v in self.byte_enc.items()}
        self.merges = {}
        for rank, m in enumerate(self.info["tokenizer.ggml.merges"]):
            a, s = m.split(" ")[0], m.split(" ")[1]
            if a in self.tok2id and s in self.tok2id:
                self.merges[(a, s)] = rank
        self.cache = {}
        self.special = {"<|endoftext|>": 0, "<|im_start|>": 1, "<|im_end|>": 2}
        for st, sid in self.special.items():
            self.cache[tuple(st)] = (st,)

    def _prep_rope(self):
        D = HEAD_DIM
        inv = THETA ** (-np.arange(0, D, 2) / D)
        pos = np.arange(MAXCTX)
        freqs = np.outer(pos, inv)
        self.cos_c = np.cos(freqs).astype(np.float32)
        self.sin_c = np.sin(freqs).astype(np.float32)

    def encode(self, text):
        ids = []
        # split special tokens out (in order of appearance)
        pats = re = regex.compile(r"<\|(?:endoftext|im_start|im_end)\|>")
        positions = []
        for m in pats.finditer(text):
            positions.append((m.start(), m.end(), m.group()))
        last = 0
        for a, b, sp in positions:
            if a > last:
                ids.extend(self._bpe(text[last:a]))
            ids.append(self.special[sp])
            last = b
        if last < len(text):
            ids.extend(self._bpe(text[last:]))
        return ids

    def _bpe(self, text):
        chars = []
        for byte in text.encode("utf-8"):
            chars.append(self.byte_enc[byte])
        word = tuple(chars)
        key = word
        if key in self.cache:
            return [self.tok2id[t] for t in self.cache[key] if t in self.tok2id]
        pairs = self._pairs(word)
        while pairs:
            bigram = min(pairs, key=lambda p: self.merges.get(p, 1 << 30))
            if bigram not in self.merges:
                break
            first, second = bigram
            new_word = []
            i = 0
            while i < len(word):
                try:
                    j = word.index(first, i)
                except ValueError:
                    new_word.extend(word[i:])
                    break
                new_word.extend(word[i:j])
                i = j
                if i < len(word) - 1 and word[i] == first and word[i + 1] == second:
                    new_word.append(first + second)
                    i += 2
                else:
                    new_word.append(word[i])
                    i += 1
            word = tuple(new_word)
            if len(word) == 1:
                break
            pairs = self._pairs(word)
        self.cache[key] = word
        return [self.tok2id[t] for t in word if t in self.tok2id]

    @staticmethod
    def _pairs(word):
        return set((word[i], word[i + 1]) for i in range(len(word) - 1))

    def token_bytes(self, ids):
        out = bytearray()
        for i in ids:
            t = self.id2tok[int(i)]
            if t in self.special:
                continue
            for ch in t:
                out.append(self.byte_dec[ch])
        return bytes(out)

    def decode(self, ids):
        return self.token_bytes(ids).decode("utf-8", "replace")

    def rmsnorm(self, x, w):
        return x * (w / np.sqrt((x * x).mean(axis=-1, keepdims=True) + EPS))

    def apply_rope(self, x, c, s):
        half = x.shape[-1] // 2
        c = c[..., :half]
        s = s[..., :half]
        x1 = x[..., 0::2]
        x2 = x[..., 1::2]
        o = np.empty_like(x)
        o[..., 0::2] = x1 * c - x2 * s
        o[..., 1::2] = x1 * s + x2 * c
        return o

    def forward(self, seq, pos0, kv):
        T = len(seq)
        emb = self.w["token_embd.weight"]            # (VOCAB, DIM)
        x = emb[list(seq)].copy()                     # (T, DIM)
        W = self.w
        for L in range(NLAYER):
            ln = self.rmsnorm(x, W["blk.%d.attn_norm.weight" % L])
            q = (ln @ W["blk.%d.attn_q.weight" % L].T).reshape(T, NHEAD, HEAD_DIM)
            k = (ln @ W["blk.%d.attn_k.weight" % L].T).reshape(T, NKV, HEAD_DIM)
            v = (ln @ W["blk.%d.attn_v.weight" % L].T).reshape(T, NKV, HEAD_DIM)
            pos = np.arange(pos0, pos0 + T)
            c = self.cos_c[pos]
            s = self.sin_c[pos]
            q = self.apply_rope(q, c[:, None, :], s[:, None, :])
            k = self.apply_rope(k, c[:, None, :], s[:, None, :])
            for t in range(T):
                kv[L][0][pos0 + t] = k[t]
                kv[L][1][pos0 + t] = v[t]
            kvT = pos0 + T
            kk = kv[L][0][:kvT].repeat(NHEAD // NKV, axis=1)   # (kvT, 8, 64)
            vv = kv[L][1][:kvT].repeat(NHEAD // NKV, axis=1)
            scores = np.einsum("thd,mhd->thm", q, kk) / math.sqrt(HEAD_DIM)
            if T > 1 or pos0 > 0:
                tidx = np.arange(T) + pos0
                midx = np.arange(kvT)
                mask = midx[None, :] > tidx[:, None]      # (T, kvT)
                scores = np.where(mask[:, None, :], -np.inf, scores)
            pr = np.exp(scores - scores.max(-1, keepdims=True))
            pr /= pr.sum(-1, keepdims=True)
            ctx = np.einsum("thm,mhd->thd", pr, vv)
            x = x + (ctx.reshape(T, NHEAD * HEAD_DIM) @ W["blk.%d.attn_output.weight" % L].T)
            ln = self.rmsnorm(x, W["blk.%d.ffn_norm.weight" % L])
            up = ln @ W["blk.%d.ffn_up.weight" % L].T
            gate = ln @ W["blk.%d.ffn_gate.weight" % L].T
            h = gate * (1.0 / (1.0 + np.exp(-gate))) * up
            x = x + (h @ W["blk.%d.ffn_down.weight" % L].T)
        x = self.rmsnorm(x, W["output_norm.weight"])
        return x @ emb.T


def sample(logits, temp, topk, prev=None, penalty=1.0):
    logits = logits.astype(np.float64)
    if penalty != 1.0 and prev:
        for t in set(int(x) for x in prev):
            if logits[t] > 0:
                logits[t] /= penalty
            else:
                logits[t] *= penalty
    if temp <= 0:
        return int(np.argmax(logits))
    p = logits / temp
    e = np.exp(p - p.max())
    e /= e.sum()
    k = min(topk, e.shape[0])
    idx = np.argpartition(e, -k)[-k:]
    eidx = e[idx]
    eidx /= eidx.sum()
    return int(np.random.choice(idx, p=eidx))


def build_prompt(messages):
    lines = []
    for role, content in messages:
        lines.append("<|im_start|>%s\n%s<|im_end|>\n" % (role, content))
    lines.append("<|im_start|>assistant\n")
    return "".join(lines)


def generate(model, prompt_ids, max_n, temp, topk, kv, visit=None, penalty=1.15):
    # batched first forward over the prompt
    logits = model.forward(prompt_ids, 0, kv)
    cache_len = len(prompt_ids)
    cur = sample(logits[-1], temp, topk, None, penalty)
    out = []
    while len(out) < max_n:
        if cur in (0, 1, 2):
            out.append(cur)
            break
        out.append(cur)
        if visit:
            visit([cur])
        logits = model.forward([cur], cache_len, kv)
        cache_len += 1
        cur = sample(logits[0], temp, topk, out[-64:], penalty)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--model", default=GGUF)
    ap.add_argument("-p", "--prompt", default="")
    ap.add_argument("-n", "--num", type=int, default=200)
    ap.add_argument("-t", "--temp", type=float, default=0.0)
    ap.add_argument("-k", "--topk", type=int, default=40)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--penalty", type=float, default=1.15)
    ap.add_argument("--history", action="store_true",
                    help="multi-turn history (model is weak; later turns get worse)")
    args = ap.parse_args()
    if args.seed:
        np.random.seed(args.seed)
    print("loading %s..." % args.model, file=sys.stderr)
    model = MiniMind(args.model)
    print("loaded.", file=sys.stderr)

    def fresh_kv():
        return [[np.zeros((MAXCTX, NKV, HEAD_DIM), np.float32),
                 np.zeros((MAXCTX, NKV, HEAD_DIM), np.float32)] for _ in range(NLAYER)]

    if args.prompt:
        prompt = build_prompt([("user", args.prompt)])
        ids = model.encode(prompt)
        kv = fresh_kv()
        out = generate(model, ids, args.num, args.temp, args.topk, kv, penalty=args.penalty)
        print(model.decode(out))
        return

    print("===AI===")
    history = []
    while True:
        try:
            q = input("You> ")
        except (EOFError, KeyboardInterrupt):
            break
        q = q.strip()
        if not q:
            continue
        history.append(("user", q))
        msgs = history if args.history else [("user", q)]
        prompt = build_prompt(msgs)
        ids = model.encode(prompt)
        kv = fresh_kv()
        sys.stdout.write("AI> ")
        sys.stdout.flush()
        dec = codecs.getincrementaldecoder("utf-8")("replace")

        def visit(toks):
            sys.stdout.write(dec.decode(model.token_bytes(toks)))
            sys.stdout.flush()

        out = generate(model, ids, args.num, args.temp, args.topk, kv, visit=visit, penalty=args.penalty)
        sys.stdout.write("\n\n")
        sys.stdout.flush()
        history.append(("assistant", model.decode([t for t in out if t not in (0, 1, 2)])))


if __name__ == "__main__":
    main()