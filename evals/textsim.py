"""Text similarity for grading descriptions: token F1 and qwen3-embedding-8b cosine on Voyager."""
import json
import math
import re
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def toks(s):
    return re.findall(r"[a-z0-9]+", s.lower())


def token_f1(a, b):
    ca, cb = Counter(toks(a)), Counter(toks(b))
    common = sum((ca & cb).values())
    if not common:
        return 0.0
    p, r = common / sum(ca.values()), common / sum(cb.values())
    return 2 * p * r / (p + r)


KEY = next(l.split("=", 1)[1].strip() for l in (ROOT / ".env").read_text().splitlines() if l.startswith("VOYAGER_API_KEY="))


def _embed_batch(batch):
    body = {"model": "qwen3-embedding-8b", "input": batch}
    req = urllib.request.Request("https://openai.rc.asu.edu/v1/embeddings", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return [d["embedding"] for d in sorted(json.loads(r.read())["data"], key=lambda d: d["index"])]


def embed(texts):
    """One vector per text. The gateway sometimes returns a batch with vectors swapped or copied between
    texts (seen 2026-10-24), so each batch is fetched twice and kept only when both copies agree."""
    uniq = sorted({t or " " for t in texts})
    vec = {}
    for i in range(0, len(uniq), 32):
        batch = uniq[i:i + 32]
        for _ in range(6):
            a, b = _embed_batch(batch), _embed_batch(batch)
            if all(cos(x, y) > 0.999 for x, y in zip(a, b)) and len(a) == len(batch):
                break
        else:
            raise RuntimeError("embeddings kept disagreeing between two identical requests")
        vec.update(zip(batch, a))
    return [vec[t or " "] for t in texts]


def cos(a, b):
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def _mean(xs):
    xs = [float(x) for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None
