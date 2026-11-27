"""Lane w24-rest (GEM-056): the finite cause of a failed ComfyUI image run, read from its /history entry.

ComfyUI writes the error of a failed prompt into ``status.messages`` as ``["execution_error", {node_type,
exception_type, exception_message, ...}]``. A run that ran out of memory (torch.OutOfMemoryError on the GPU, or the
CPU allocator's "not enough memory" in VAEDecode) is named ``out_of_memory``; every other error stays
``execution_failed``. Only the finite code leaves this module, never the exception text (it may hold paths).
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

OUT_OF_MEMORY = "out_of_memory"
# w28-fix item 3 (IMG-220): "hostbuf_file_reader_read failed" is ComfyUI's dynamic-VRAM weight prefetch running out of
# pinned host memory (maintainer window: P-H3-Image beside a 26 GB model already loaded, 10 GB RAM free; /free fixed it).
_MEMORY = re.compile(r"out ?of ?memory|not enough memory|outofmemory|failed to allocate|cannot allocate|defaultcpuallocator|hostbuf_file_reader", re.I)


def failure_reason(entry: Any, default: str = "execution_failed") -> str:
    status = entry.get("status") if isinstance(entry, Mapping) else None
    messages = status.get("messages") if isinstance(status, Mapping) else None
    for message in messages if isinstance(messages, list) else ():
        if isinstance(message, (list, tuple)) and len(message) >= 2 and message[0] == "execution_error" and isinstance(message[1], Mapping):
            text = " ".join(str(message[1].get(key) or "") for key in ("exception_type", "exception_message"))
            if _MEMORY.search(text):
                return OUT_OF_MEMORY
    return default


__all__ = ["OUT_OF_MEMORY", "failure_reason"]
