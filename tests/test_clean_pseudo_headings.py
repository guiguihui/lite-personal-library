"""pytest 单元测试:clean_markdown 伪标题识别。

覆盖 detect_pseudo_headings / _classify_headings_regex / _classify_headings_llm /
fix_pseudo_headings / fix_heading_hierarchy Pass 2 扩展 / clean() 集成。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_VENDOR = Path(__file__).resolve().parent.parent / "app" / "vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

import clean_markdown as cm  # type: ignore  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
# detect_pseudo_headings
# ══════════════════════════════════════════════════════════════════════════


class TestDetectPseudoHeadings:
    def test_finds_numbered_candidates(self) -> None:
        """数字编号伪标题行被识别，depth 按点数算。"""
        text = "1 前言\n\n1.1 文档说明\n正文\n\n1.1.1 编写目的\n"
        lines = text.split("\n")
        cands = cm.detect_pseudo_headings(lines)
        numbers = [c["number"] for c in cands]
        depths = {c["number"]: c["depth"] for c in cands}
        assert "1" in numbers and "1.1" in numbers and "1.1.1" in numbers
        assert depths["1"] == 1
        assert depths["1.1"] == 2
        assert depths["1.1.1"] == 3

    def test_skips_existing_hash_headings(self) -> None:
        """已有 # 前缀的行不被当候选。"""
        text = "## 1.1 已有标题\n1.2 无前缀\n"
        cands = cm.detect_pseudo_headings(text.split("\n"))
        assert len(cands) == 1
        assert cands[0]["number"] == "1.2"

    def test_skips_code_fences(self) -> None:
        """代码块内的数字编号行不被识别。"""
        text = "1.1 真标题\n```\n1.2 代码块内\n1.3 也是代码块\n```\n1.4 真标题2\n"
        cands = cm.detect_pseudo_headings(text.split("\n"))
        numbers = [c["number"] for c in cands]
        assert "1.2" not in numbers
        assert "1.3" not in numbers
        assert "1.1" in numbers and "1.4" in numbers

    def test_skips_non_numbered_lines(self) -> None:
        """非数字开头的行不被识别。"""
        text = "正文内容\nOpenAPI 对接指南\n![](img.png)\n"
        cands = cm.detect_pseudo_headings(text.split("\n"))
        assert cands == []

    def test_excludes_data_values(self) -> None:
        """纯数字数据值（如 1889）后跟文字也会被当候选——这是预期行为，
        LLM/正则兜底会判断。detect 阶段只做候选收集。"""
        text = "1889 中公布的。\n64 字节\n"
        cands = cm.detect_pseudo_headings(text.split("\n"))
        # 1889 和 64 都是数字开头，会进候选（depth=1）
        assert len(cands) == 2
        assert all(c["depth"] == 1 for c in cands)


# ══════════════════════════════════════════════════════════════════════════
# _classify_headings_regex
# ══════════════════════════════════════════════════════════════════════════


class TestClassifyRegex:
    def test_depth2_plus_is_heading(self) -> None:
        """depth≥2（有至少一个点）→ True。"""
        cands = [
            {"line_idx": 0, "number": "1", "depth": 1, "text": "1 前言"},
            {"line_idx": 1, "number": "1.1", "depth": 2, "text": "1.1 文档说明"},
            {"line_idx": 2, "number": "1.1.1", "depth": 3, "text": "1.1.1 编写"},
            {"line_idx": 3, "number": "4.4.2.5", "depth": 4, "text": "4.4.2.5 车位"},
        ]
        result = cm._classify_headings_regex(cands)
        assert result == {0: False, 1: True, 2: True, 3: True}

    def test_depth1_not_heading(self) -> None:
        """depth=1（无点）→ False（正则兜底保守不转）。"""
        cands = [
            {"line_idx": 5, "number": "1889", "depth": 1, "text": "1889 中公布的"},
            {"line_idx": 6, "number": "64", "depth": 1, "text": "64 字节"},
        ]
        result = cm._classify_headings_regex(cands)
        assert result == {5: False, 6: False}


# ══════════════════════════════════════════════════════════════════════════
# fix_pseudo_headings
# ══════════════════════════════════════════════════════════════════════════


class TestFixPseudoHeadings:
    def test_regex_fallback_adds_hash(self) -> None:
        """无 LLM 配置时降级正则，depth≥2 加 ## 前缀。"""
        text = "1 前言\n\n1.1 文档说明\n正文\n\n1.1.1 编写目的\n"
        result, stats = cm.fix_pseudo_headings(text)
        assert "## 1.1 文档说明" in result
        assert "## 1.1.1 编写目的" in result
        assert result.startswith("1 前言")  # depth=1 不转
        assert stats["pseudo_detected"] == 3
        assert stats["pseudo_promoted"] == 2
        assert stats["llm_failed"] is True
        assert stats["llm_used"] is False

    def test_no_candidates_unchanged(self) -> None:
        """无伪标题时返回原 text。"""
        text = "## 已有标题\n正文内容\n![](img.png)\n"
        result, stats = cm.fix_pseudo_headings(text)
        assert result == text
        assert stats["pseudo_detected"] == 0
        assert stats["pseudo_promoted"] == 0

    def test_preserves_original_number_text(self) -> None:
        """加 ## 前缀时保留原编号文字。"""
        text = "4.4.2.5 车位管理编程引导\n"
        result, _ = cm.fix_pseudo_headings(text)
        assert "## 4.4.2.5 车位管理编程引导" in result


# ══════════════════════════════════════════════════════════════════════════
# fix_heading_hierarchy Pass 2 扩展
# ══════════════════════════════════════════════════════════════════════════


class TestFixHeadingHierarchyPass2:
    def test_demote_by_dot_count(self) -> None:
        """## N.M → H3, ## N.M.K → H4, ## N.M.K.L → H5, 封顶 H6。"""
        text = (
            "## 1.1 二级\n"
            "## 1.1.1 三级\n"
            "## 1.1.1.1 四级\n"
            "## 1.1.1.1.1 五级\n"
            "## 1.1.1.1.1.1 六级\n"
            "## 1.1.1.1.1.1.1 七级（封顶 H6）\n"
        )
        result, stats = cm.fix_heading_hierarchy(text)
        lines = result.split("\n")
        assert lines[0] == "### 1.1 二级"  # 1点 → H3
        assert lines[1] == "#### 1.1.1 三级"  # 2点 → H4
        assert lines[2] == "##### 1.1.1.1 四级"  # 3点 → H5
        assert lines[3] == "###### 1.1.1.1.1 五级"  # 4点 → H6
        assert lines[4] == "###### 1.1.1.1.1.1 六级"  # 5点 → H6（封顶）
        assert lines[5] == "###### 1.1.1.1.1.1.1 七级（封顶 H6）"  # 6点 → H6
        assert stats["headings_demoted"] == 6

    def test_no_dot_stays_h2(self) -> None:
        """## 1 前言（无点）不匹配 Pass 2，停在 H2。"""
        text = "## 1 前言\n"
        result, stats = cm.fix_heading_hierarchy(text)
        assert result.rstrip() == "## 1 前言"
        assert stats["headings_demoted"] == 0


# ══════════════════════════════════════════════════════════════════════════
# clean() 集成
# ══════════════════════════════════════════════════════════════════════════


class TestCleanIntegration:
    def test_pseudo_headings_promoted_and_demoted(self) -> None:
        """端到端：伪标题 → ## → fix_heading_hierarchy 降级到正确层级。"""
        text = (
            "1 前言\n\n"
            "1.1 文档说明\n正文\n\n"
            "1.1.1 编写目的\n正文\n\n"
            "4.4.2.5 车位管理编程引导\n正文\n"
        )
        cleaned, stats = cm.clean(text)
        # depth=1 的 "1 前言" 不转（正则兜底）
        assert "1 前言" in cleaned
        assert not cleaned.split("\n")[0].startswith("#")
        # depth≥2 被转并降级
        assert "### 1.1 文档说明" in cleaned  # 2点 → H3
        assert "#### 1.1.1 编写目的" in cleaned  # 3点 → H4
        assert "##### 4.4.2.5 车位管理编程引导" in cleaned  # 4点 → H5
        assert stats["pseudo_promoted"] == 3
        assert stats["headings_demoted"] == 3

    def test_existing_headings_unaffected(self) -> None:
        """已有 # 标题不受伪标题逻辑影响。"""
        text = "## 第一章\n正文\n### 1.1 节\n"
        cleaned, _ = cm.clean(text)
        # ## 第一章 保持（非数字编号，Pass 2 不匹配）
        assert "## 第一章" in cleaned
