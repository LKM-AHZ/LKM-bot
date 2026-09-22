"""Minimal type stubs for faiss used in this project.

This file only exposes a small subset of the faiss API that the
project uses, including the runtime-monkeypatched signatures such as
`Index.add_with_ids` so Pyright/Pylance stops reporting false positives.
"""

from typing import Any, overload

import numpy as np

class Index:
    d: int
    ntotal: int
    # 不声明 code_size / nprobe：它们不是 faiss.Index 基类的成员（code_size 只在 coded index、
    # nprobe 只在 IndexIVF* 上有）。写在这里会让类型检查放过 `index.nprobe` 这类运行期 AttributeError。

    def add(self, x: np.ndarray) -> None: ...
    def add_with_ids(self, x: np.ndarray, ids: np.ndarray) -> None: ...
    def search(
        self,
        x: np.ndarray,
        k: int,
        *,
        params: Any = ...,
        D: np.ndarray | None = ...,
        I: np.ndarray | None = ...,
    ) -> tuple[np.ndarray, np.ndarray]: ...
    def remove_ids(self, x: np.ndarray) -> int: ...
    # stub 里 @overload 即公开契约：不再补一个「实现签名」（它永远不会被调用，
    # 而且默认参数比两个 overload 宽松得多，反而会成为第三个可用签名）
    @overload
    def reconstruct(self, key: int) -> np.ndarray: ...
    @overload
    def reconstruct(self, key: int, x: np.ndarray) -> None: ...
    @overload
    def reconstruct_n(self, n0: int, ni: int) -> np.ndarray: ...
    @overload
    def reconstruct_n(self, n0: int, ni: int, x: np.ndarray) -> None: ...
    def range_search(
        self, x: np.ndarray, thresh: float, *, params: Any = ...
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]: ...
    def add_sa_codes(self, codes: np.ndarray, ids: np.ndarray | None = ...) -> None: ...
    def sa_encode(self, x: np.ndarray) -> np.ndarray: ...
    def sa_decode(self, codes: np.ndarray) -> np.ndarray: ...

class IndexFlatL2(Index):
    def __init__(self, d: int) -> None: ...

class IndexIDMap(Index):
    index: Index

    def __init__(self, index: Index) -> None: ...

def read_index(path: str) -> Index: ...
# path 必填：faiss 没有「省略目标路径」的重载，写成可选会让 write_index(idx) 过类型检查、
# 运行时才抛 TypeError（本项目的两个调用点也都传了具体路径）
def write_index(index: Index, path: str) -> None: ...
def normalize_L2(x: np.ndarray) -> None: ...

# Additional concrete-ish classes exposed by some faiss builds (SWIG helpers
# expose `downcast_*` helpers to convert generic objects to these concrete
# types). We keep these minimal — only the names are important for typing.
class IndexBinary:
    # 不是 Index 的子类：faiss 里二进制索引是独立层级（只有 d/ntotal/add/search/range_search
    # 等，没有 Index 的 add_with_ids/remove_ids/sa_encode）。继承 Index 会让二进制索引
    # 被当成浮点索引使用（可赋值给 Index 形参），放过运行期 AttributeError。
    d: int
    ntotal: int

    def __init__(self, d: int) -> None: ...
    def add(self, x: np.ndarray) -> None: ...
    def search(
        self, x: np.ndarray, k: int
    ) -> tuple[np.ndarray, np.ndarray]: ...

class InvertedLists:
    def __len__(self) -> int: ...

class AdditiveQuantizer:
    pass

class Quantizer:
    pass

class VectorTransform:
    pass

# SWIG-provided downcast helpers (present in some faiss Python builds).
def downcast_IndexBinary(obj: Any) -> IndexBinary: ...
def downcast_InvertedLists(obj: Any) -> InvertedLists: ...
def downcast_AdditiveQuantizer(obj: Any) -> AdditiveQuantizer: ...
def downcast_Quantizer(obj: Any) -> Quantizer: ...
def downcast_VectorTransform(obj: Any) -> VectorTransform: ...
def downcast_index(obj: Any) -> Index: ...

# version exposed by runtime
__version__: str
