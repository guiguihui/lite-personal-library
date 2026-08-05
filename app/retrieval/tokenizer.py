"""Tokenizer + query expansion (port of retrieval.js L21-149).

严格对齐 retrieval.js 行为：

- ``tokenize_raw``：英文单词 ``[a-zA-Z][a-zA-Z0-9]{1,}`` (lower) + 纯数字 ``\\d{2,}``
  + CJK ``[一-鿿]+`` 做 2-gram（不保留单字）。
- ``tokenize`` = ``tokenize_raw``（保留重复，BM25 TF 用）。
- ``tokenize_unique``：去重保序（DF / coverage / postings 候选收集用）。
- ``expand_query`` / ``expand_query_weighted``：手写同义词表，原始 token 1.0，
  同义词 0.6（``SYNONYM_WEIGHT``），同义词值也走 ``tokenize_unique`` 拆多词短语。

对齐 pitfalls（已处理）：

- CJK 范围 ``[一-鿿]`` 是 U+4E00–U+9FFF 基本区，不含扩展 A/B。
  Python ``re`` 支持 ``[一-鿿]`` 字面量（源文件 UTF-8）。
- JS ``String.match(regex)`` 返回 ``null`` 时用 ``|| []`` 兜底；
  Python ``re.findall`` 返回 ``[]``，无需兜底。但 ``re.match`` 只匹配开头，
  语义不同——必须用 ``re.findall``。
- 单个字母（如 ``'a'``）不会被英文正则匹配（要求总长≥2）。
  单个数字（如 ``'7'``）也不会被匹配。
- CJK 单字段被丢弃（设计决策：单字噪声大、IDF 失效），不要改成保留单字。
- ``tokenize_unique`` 用 ``list(dict.fromkeys(toks))`` 保序去重，
  不要用 ``set``（无序）——Python 3.7+ ``dict`` 保序。
"""

from __future__ import annotations

import re

__all__ = [
    "tokenize_raw",
    "tokenize",
    "tokenize_unique",
    "SYNONYMS",
    "SYNONYM_WEIGHT",
    "expand_query",
    "expand_query_weighted",
]

# ── 正则（与 retrieval.js 完全一致）──────────────────────────────────────
# 英文单词：首字符必须是字母，后跟≥1 个字母或数字（总长≥2）。
_EN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9]{1,}")
# 纯数字：≥2 位连续数字串。
_NUM_RE = re.compile(r"\d{2,}")
# CJK 基本区（U+4E00–U+9FFF），不含扩展 A/B。
_CJK_RE = re.compile(r"[一-鿿]+")


def tokenize_raw(text: str | None) -> list[str]:
    """Tokenize raw text (with duplicates, for BM25 TF).

    与 retrieval.js ``tokenizeRaw`` (L21-33) 逐行对齐。三步顺序固定：
    (1) 英文单词 lower；(2) 纯数字（不 lower）；(3) CJK 2-gram 滑窗。

    边界：``text`` 为 None/空串/纯标点 → ``[]``。CJK 单字（如 ``'光'``）被丢弃。
    """
    if not text:
        return []
    tokens: list[str] = []
    # (1) 英文单词（长度 ≥2，含术语缩写如 SPT/Rabi），toLowerCase
    for w in _EN_RE.findall(text):
        tokens.append(w.lower())
    # (2) 纯数字 ≥2（不 toLowerCase）
    for w in _NUM_RE.findall(text):
        tokens.append(w)
    # (3) 中文：仅 2-gram。"相变""临界""金融"等二字词才是有效检索单元。
    for seg in _CJK_RE.findall(text):
        # i 从 0 到 len(seg)-2（含），seg[i:i+2] 是 2-gram。
        # 若 len(seg)<2 则不生成 2-gram（循环条件 i < len(seg)-1 即 i <= len(seg)-2）。
        for i in range(len(seg) - 1):
            tokens.append(seg[i : i + 2])
    return tokens


# tokenize：保留重复（BM25 真实 TF 用）。别名 tokenizeWithFrequency。
tokenize = tokenize_raw


def tokenize_unique(text: str | None) -> list[str]:
    """Tokenize and dedupe preserving first-seen order.

    与 retrieval.js ``tokenizeUnique`` (L39-42) 对齐。
    用于 DF / coverage / postings 候选收集。

    用 ``dict.fromkeys`` 保序去重（Python 3.7+ dict 保序），不要用 ``set``（无序）。
    """
    toks = tokenize_raw(text)
    return list(dict.fromkeys(toks))


# ══════════════════════════════════════════════════════════════════════════
# Query expansion：手写同义词表（retrieval.js L48-149）
# ══════════════════════════════════════════════════════════════════════════
# 覆盖常见物理/ML/金融术语的中英互译与缩写。运行时表在索引加载后构建（此处为静态）。
SYNONYMS: dict[str, list[str]] = {
    "超辐射相变": ["superradiant phase transition", "SPT", "superradiant"],
    "相变": ["phase transition", "critical", "临界"],
    "临界": ["critical", "相变", "phase transition"],
    "berry": ["berry phase", "贝里相位", "几何相位"],
    "贝里": ["berry phase", "geometric phase", "几何相位"],
    "rabi": ["拉比", "jaynes-cummings", "JC"],
    "拉比": ["rabi", "jaynes-cummings"],
    "dicke": ["迪克", "superradiant"],
    "线性响应": ["linear response", "kubo", "久保"],
    "格林函数": ["green function", "propagator", "传播子"],
    "路径积分": ["path integral", "feynman"],
    "机器学习": ["machine learning", "ML", "deep learning"],
    "神经网络": ["neural network", "NN", "deep learning"],
    "量子蒙特卡洛": ["quantum monte carlo", "QMC"],
    "蒙特卡洛": ["monte carlo", "MC"],
    "密度矩阵重整化群": ["density matrix renormalization group", "DMRG"],
    "张量网络": ["tensor network", "MPS", "MPO"],
    "矩阵乘积态": ["matrix product state", "MPS"],
    "贝里相位": ["berry phase", "geometric phase"],
    "几何相位": ["geometric phase", "berry phase", "贝里相位"],
    "耗散": ["dissipation", "dissipative"],
    "相干态": ["coherent state"],
    "压缩态": ["squeezed state"],
    "自旋": ["spin"],
    "玻色": ["boson", "bose"],
    "玻色子": ["boson", "bose"],
    "费米": ["fermi", "fermion"],
    "费米子": ["fermi", "fermion"],
    "哈密顿": ["hamiltonian", "hamilton"],
    "拉格朗日": ["lagrangian"],
    "配分函数": ["partition function"],
    "基态": ["ground state"],
    "激发态": ["excited state"],
    "绝热": ["adiabatic"],
    "厄米": ["hermitian"],
    "幺正": ["unitary"],
    "统计力学": ["statistical mechanics"],
    "量子场论": ["quantum field theory", "QFT"],
    "规范场": ["gauge field"],
    "对称性": ["symmetry"],
    "拓扑": ["topological", "topology"],
    "纠缠": ["entanglement", "entangled"],
    "退相干": ["decoherence"],
    "量子比特": ["qubit", "quantum bit"],
    "量子门": ["quantum gate"],
    "量子算法": ["quantum algorithm"],
    "变分": ["variational"],
    "微扰": ["perturbation", "perturbative"],
    "关联函数": ["correlation function"],
    "谱函数": ["spectral function"],
    "响应函数": ["response function"],
    "极化率": ["polarizability", "susceptibility"],
    "磁化率": ["susceptibility", "magnetic susceptibility"],
    "期权": ["option"],
    "期货": ["future", "futures"],
    "对冲": ["hedge", "hedging"],
    "波动率": ["volatility"],
    "套利": ["arbitrage", "arb"],
    "回撤": ["drawdown"],
    "动量": ["momentum"],
    "均值回复": ["mean reversion"],
    "协整": ["cointegration"],
    "交叉验证": ["cross validation"],
    "过拟合": ["overfitting", "overfit"],
    "正则化": ["regularization"],
    "梯度": ["gradient"],
    "反向传播": ["backpropagation", "backprop"],
    "激活函数": ["activation function"],
    "损失函数": ["loss function"],
    "优化器": ["optimizer"],
}

# 权重配置：原始 token 1.0，同义词扩展 0.6（避免同义词稀释精确匹配）。
SYNONYM_WEIGHT = 0.6


def expand_query_weighted(
    tokens: list[str], raw_query: str | None
) -> tuple[list[str], dict[str, float]]:
    """Expand query with synonyms (weighted).

    与 retrieval.js ``expandQueryWeighted`` (L132-149) 对齐。

    - 原始 token 权重 1.0；同义词扩展 0.6。
    - 同义词值也走 ``tokenize_unique``（修复 "linear response" 当单 token 的 bug）。
    - 权重取最大值（原始优先）：先设原始 1.0，再对同义词 token
      ``if t not in weights: weights[t] = 0.6``。
    - 匹配条件：``raw.includes(lk) or tokens.includes(lk)``。

    返回 ``(tokens, weights)``，tokens 为 ``list(weights.keys())`` 保序。
    """
    weights: dict[str, float] = {}
    for t in tokens:
        weights[t] = 1.0
    raw = (raw_query or "").lower()
    for key, syns in SYNONYMS.items():
        lk = key.lower()
        if lk in raw or lk in tokens:
            for s in syns:
                # 关键修复：同义词也走 tokenizer，多词短语拆成可匹配的 token
                for t in tokenize_unique(s):
                    # 原始 token 权重保留（不被同义词稀释）；新 token 给 SYNONYM_WEIGHT
                    if t not in weights:
                        weights[t] = SYNONYM_WEIGHT
    return list(weights.keys()), weights


def expand_query(tokens: list[str], raw_query: str | None) -> list[str]:
    """Expand query (tokens only). 简写：``expand_query_weighted(...)[0]``."""
    return expand_query_weighted(tokens, raw_query)[0]
