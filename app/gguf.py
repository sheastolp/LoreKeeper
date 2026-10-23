"""Minimal GGUF header reader: enough metadata to label, pair and validate model files.

GGUF layout: magic "GGUF", u32 version, u64 tensor_count, u64 kv_count, then kv_count
key/value pairs (key = u64 length + utf-8 bytes, value = u32 type tag + payload).
The tokenizer vocab arrays come last and can hold 250k strings, so reading stops at
the first "tokenizer." key — everything we need sits before it.
"""
from __future__ import annotations

import re
import struct
from pathlib import Path

_SCALAR = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
_STRING, _ARRAY = 8, 9

FILE_TYPES = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1", 10: "Q2_K", 11: "Q3_K_S",
    12: "Q3_K_M", 13: "Q3_K_L", 14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K",
    19: "IQ2_XXS", 20: "IQ2_XS", 21: "Q2_K_S", 22: "IQ3_XS", 23: "IQ3_XXS", 24: "IQ1_S", 25: "IQ4_NL",
    26: "IQ3_S", 27: "IQ3_M", 28: "IQ2_S", 29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16",
    36: "TQ1_0", 37: "TQ2_0", 38: "MXFP4",
}


class GGUFError(ValueError):
    pass


def _read(fh, fmt):
    size = struct.calcsize(fmt)
    data = fh.read(size)
    if len(data) != size:
        raise GGUFError("truncated header")
    return struct.unpack(fmt, data)[0]


def _string(fh) -> str:
    n = _read(fh, "<Q")
    if n > 1 << 24:
        raise GGUFError("implausible string length")
    return fh.read(n).decode("utf-8", errors="replace")


def _value(fh, vtype, keep: bool):
    if vtype in _SCALAR:
        return _read(fh, _SCALAR[vtype])
    if vtype == _STRING:
        return _string(fh)
    if vtype == _ARRAY:
        etype, count = _read(fh, "<I"), _read(fh, "<Q")
        if etype in _SCALAR and not keep:
            fh.seek(struct.calcsize(_SCALAR[etype]) * count, 1)
            return None
        items = [_value(fh, etype, keep) for _ in range(count)]
        return items if keep and count <= 64 else None
    raise GGUFError(f"unknown value type {vtype}")


def read_metadata(path: str | Path) -> dict:
    meta: dict = {}
    with open(path, "rb") as fh:
        if fh.read(4) != b"GGUF":
            raise GGUFError("not a GGUF file")
        version = _read(fh, "<I")
        if version < 2:
            raise GGUFError(f"GGUF v{version} is too old")
        meta["gguf.version"] = version
        meta["gguf.tensor_count"] = _read(fh, "<Q")
        n_kv = _read(fh, "<Q")
        for _ in range(n_kv):
            key = _string(fh)
            if key.startswith("tokenizer."):
                break
            vtype = _read(fh, "<I")
            meta[key] = _value(fh, vtype, keep=not key.startswith(("tokenizer.", "clip.vision.image_")))
    return meta


def describe(path: str | Path) -> dict:
    """Summarise a GGUF for the UI and for role detection."""
    p = Path(path)
    m = read_metadata(p)
    arch = m.get("general.architecture", "?")
    gtype = m.get("general.type", "")
    is_mmproj = gtype == "mmproj" or arch == "clip" or p.name.lower().startswith("mmproj")
    pooling = m.get(f"{arch}.pooling_type")
    name_l = p.name.lower()
    is_embed = (pooling is not None and pooling != 0) or "embed" in name_l or arch in ("bert", "nomic-bert", "jina-bert-v2")
    if is_mmproj:
        role = "mmproj"
    elif is_embed:
        role = "embedding"
    else:
        role = "chat"
    ft = m.get("general.file_type")
    quant = FILE_TYPES.get(ft) if ft is not None else None
    if not quant:
        hit = re.search(r"(?i)\b(I?Q\d_[A-Z0-9_]+|Q\d_\d|BF16|F16|F32|MXFP4)\b", p.stem.replace("-", " ").replace(".", " "))
        quant = hit.group(1).upper() if hit else None
    return {
        "path": str(p),
        "file": p.name,
        "size": p.stat().st_size,
        "arch": arch,
        "name": m.get("general.name") or p.stem,
        "size_label": m.get("general.size_label"),
        "quant": quant,
        "context": m.get(f"{arch}.context_length"),
        "embedding_dim": m.get(f"{arch}.embedding_length_out") or m.get(f"{arch}.embedding_length"),
        "role": role,
        "vision": bool(m.get("clip.has_vision_encoder")) if is_mmproj else None,
        "audio": bool(m.get("clip.has_audio_encoder")) if is_mmproj else None,
    }
