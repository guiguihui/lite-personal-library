#!/usr/bin/env python3
"""Translate English markdown chapters to Chinese using LLM + glossary-driven consistency.

Two-phase workflow:
  Phase 1 (seed, serial): translate first N chapters â†’ build initial glossary
  Phase 2 (rest, parallel): translate remaining chapters WITH glossary â†’ collect new terms

Each chapter is validated after translation (via validate_book.py's validate_file).
If [E]-level errors or too many residual English words â†’ retry with feedback (max N times).

Usage:
    python3 translate_chapters.py <book_dir> [--concurrency 4] [--seed-chapters 2] [--retry 2]
    python3 translate_chapters.py <file.md>   # single file, no glossary

Requires .env with DEEPSEEK_API_KEY (or OPENAI_API_KEY) for the API key.
Model/base_url/pipeline toggles come from config.yaml (optional; falls back
to DEEPSEEK_MODEL / DEEPSEEK_BASE_URL env vars when absent).
"""
import argparse
import asyncio
import glob
import json
import os
import re
import sys

# â”€â”€ Path setup: import validate_book from sibling directory â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from validate_book import validate_file, ERR  # noqa: E402
from convert_xrefs import convert_file as convert_xrefs_file  # noqa: E402
from llm_config import get_tier, get_tier_full, get_pipeline_config, get_segment_config, has_config  # noqa: E402
from app.llm.nonstream import call_llm_once  # noqa: E402

# â”€â”€ Config â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Tiered model config from config.yaml (+ .env for keys). Falls back to legacy
# single-model behavior when config.yaml is absent.
_STRONG_KEY, _STRONG_URL, _STRONG_MODEL, _STRONG_MAX, _STRONG_PROTO, _STRONG_PATH, _STRONG_PROVIDER = get_tier_full("strong")
_PIPELINE = get_pipeline_config()
_SEGMENT = get_segment_config()

# Legacy module-level names (used by _translate_once and callers).
API_KEY = _STRONG_KEY
BASE_URL = _STRONG_URL
MODEL = _STRONG_MODEL
MAX_TOKENS = _STRONG_MAX
PROTOCOL = _STRONG_PROTO
PATH_MODE = _STRONG_PATH
PROVIDER = _STRONG_PROVIDER

GLOSSARY_MARKER = re.compile(r"<!--\s*glossary:\s*(.+?)\s*=\s*(.+?)\s*-->")
RESIDUAL_EN_RE = re.compile(r"[A-Z][a-z]{10,}")  # long English words = missed translation


# â”€â”€ System prompt (fixed rules, no cross-refs) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
SYSTEM_PROMPT = """ä½ æ˜¯ä¸“ä¸šç¿»è¯‘ã€‚å°†è‹±æ–‡ markdown æ­£æ–‡ç¿»è¯‘ä¸ºä¸­æ–‡ï¼Œä¸¥æ ¼éµå®ˆï¼š

1. LaTeX å…¬å¼ $...$ã€$$...$$ã€\\(...\\)ã€\\[...\\] å’Œ \\tag{} 100% åŸæ ·ä¸åŠ¨
2. äººåä¿ç•™è‹±æ–‡ï¼šHaoyu Guan, Wenxian Zhangï¼ˆä¸è¦ç¿»è¯‘äººåï¼‰
3. æœºæ„åä¿ç•™è‹±æ–‡åŸæ–‡ï¼šKey Laboratory of Artificial Micro- and Nano-structures...ï¼ˆä¸è¦ç¿»è¯‘æœºæ„åï¼‰
4. ä¹¦åè¯‘åé™„è‹±æ–‡ï¼šã€Šæ¼«æ­¥åå°”è¡—ã€‹ï¼ˆA Random Walk Down Wall Streetï¼‰
5. å›¾è¡¨ç¼–å·ä¿ç•™åŸæ ¼å¼ï¼šå›¾1.1ã€è¡¨2.3
6. Chapter N / Section N.M å¿ å®ç¿»è¯‘ä¸º"ç¬¬Nç« "/"ç¬¬N.MèŠ‚"ï¼ˆä¸åŠ  markdown é“¾æ¥ï¼‰
7. å…ƒç´ æ¨¡æ¿è½¬æ¢ï¼ˆç¿»è¯‘æ—¶åŒæ­¥å®Œæˆï¼‰ï¼š
   - å¼•ç”¨ â€”Author, *Book* â†’ {{< callout type="quote" >}}å¼•ç”¨å†…å®¹\\nAuthor, Book{{< /callout >}}
   - æ¥æº/å‡ºå¤„è¡Œ â†’ {{< caption >}}æ¥æºï¼š...{{< /caption >}}
   - å›¾æ³¨ å›¾N.N æè¿° â†’ {{< caption >}}å›¾N.N æè¿°{{< /caption >}}
8. ğŸ”´ å›¾ç‰‡å¼•ç”¨ 100% ä¿ç•™åŸæ ·ï¼š![](images/xxx.webp) è¿™ä¸€è¡Œå¿…é¡»åŸæ ·è¾“å‡ºï¼Œä¸è¦åˆ é™¤ã€åˆå¹¶æˆ–ç¿»è¯‘è·¯å¾„
9. ä¸è¦ä¿®æ”¹ front matterï¼ˆ--- ä¹‹é—´çš„å†…å®¹ï¼‰
10. ğŸ”´ æ¯ä¸ªæ®µè½åªè¾“å‡ºä¸€æ¬¡ä¸­æ–‡è¯‘æ–‡ï¼Œä¸¥ç¦ä¿ç•™è‹±æ–‡åŸæ–‡ã€‚æ­£ç¡®ç¤ºä¾‹ï¼šè¾“å…¥ "Hello world" â†’ è¾“å‡º "ä½ å¥½ä¸–ç•Œ"ã€‚é”™è¯¯ç¤ºä¾‹ï¼šè¾“å…¥ "Hello world" â†’ è¾“å‡º "Hello world\n\nä½ å¥½ä¸–ç•Œ"ï¼ˆä¸¥ç¦è¿™ç§åŒè¯­è¾“å‡ºï¼‰
11. è¾“å‡ºå®Œæ•´è¯‘æ–‡ï¼Œä¸è¦åŠ ä»»ä½•è§£é‡Šæˆ–æ³¨é‡Š"""


def build_user_prompt(body: str, glossary: dict, is_seed: bool) -> str:
    """Build user message with optional glossary context."""
    parts = [body]

    if glossary and not is_seed:
        terms = "\n".join(f"- {en} â†’ {zh}" for en, zh in sorted(glossary.items()))
        parts.append(
            f"\n---\nå·²æœ‰æœ¯è¯­è¡¨ï¼ˆå‰é¢ç« èŠ‚å·²å¼•å…¥ï¼Œç”¨æŒ‡å®šè¯‘åï¼Œ**ä¸è¦å†é™„è‹±æ–‡**ï¼‰ï¼š\n{terms}\n\n"
            "é‡åˆ°æ–°æœ¯è¯­ï¼ˆä¸åœ¨ä¸Šè¡¨é‡Œï¼‰ï¼Œé¦–æ¬¡å‡ºç°æ—¶é™„è‹±æ–‡åŸæ–‡ï¼Œå¦‚ï¼šç»çƒ­å®šç†ï¼ˆadiabatic theoremï¼‰ã€‚\n"
            "ç¿»è¯‘å®Œæˆåï¼Œåœ¨è¯‘æ–‡æœ€æœ«å°¾ç”¨ä»¥ä¸‹æ ¼å¼åˆ—å‡ºæœ¬ç« æ–°å¢çš„æœ¯è¯­ï¼ˆæ¯è¡Œä¸€ä¸ªï¼‰ï¼š\n"
            "<!-- glossary: English Term = ä¸­æ–‡è¯‘å -->"
        )
    elif is_seed:
        parts.append(
            "\n---\nè¿™æ˜¯å…¨ä¹¦å‰å‡ ç« ã€‚ä¸“ä¸šæœ¯è¯­é¦–æ¬¡å‡ºç°æ—¶é™„è‹±æ–‡åŸæ–‡ï¼Œå¦‚ï¼šæœ‰æ•ˆå¸‚åœºå‡è¯´ï¼ˆEfficient Market Hypothesisï¼‰ã€‚\n"
            "ç¿»è¯‘å®Œæˆåï¼Œåœ¨è¯‘æ–‡æœ€æœ«å°¾ç”¨ä»¥ä¸‹æ ¼å¼åˆ—å‡ºæœ¬ç« æ‰€æœ‰ä¸“ä¸šæœ¯è¯­ï¼ˆæ¯è¡Œä¸€ä¸ªï¼‰ï¼š\n"
            "<!-- glossary: English Term = ä¸­æ–‡è¯‘å -->"
        )
    return "\n".join(parts)


def split_front_matter(content: str):
    """Split into (front_matter_str, body_str)."""
    if not content.startswith("---"):
        return "", content
    end = content.find("\n---", 3)
    if end < 0:
        return "", content
    nl = content.find("\n", end + 4)
    if nl < 0:
        return content, ""
    return content[: nl + 1], content[nl + 1 :]


def extract_glossary(text: str):
    """Extract <!-- glossary: EN = ZH --> lines, return (cleaned_text, {EN: ZH})."""
    terms = {}
    for m in GLOSSARY_MARKER.finditer(text):
        en, zh = m.group(1).strip(), m.group(2).strip()
        if en and zh:
            terms[en] = zh
    cleaned = GLOSSARY_MARKER.sub("", text).rstrip() + "\n"
    return cleaned, terms


def check_quality(path: str, source_body: str = "", is_seed: bool = False):
    """Run validate_file + residual English + untranslated block + truncation check.

    When is_seed=True, skip the residual-English-word check â€” seed chapters
    intentionally keep English terms in parentheses (e.g. æ“ä½œæ¦‚ç‡ç†è®ºï¼ˆOperational
    Probabilistic Theories, OPTï¼‰), which would otherwise trigger false retries.
    """
    issues = validate_file(path)
    errors = [msg for level, msg in issues if level == ERR]

    with open(path, encoding="utf-8") as f:
        content = f.read()
    _, body = split_front_matter(content)
    residual = len(RESIDUAL_EN_RE.findall(body))  # body only, not front matter

    problems = []
    if errors:
        problems.extend(f"[E] {e}" for e in errors)
    # Seed chapters intentionally retain English terms â€” don't flag them.
    # Non-seed chapters should have terms translated; >8 long English words
    # suggests a missed paragraph.
    if not is_seed and residual > 8:
        problems.append(f"é—æ¼è‹±æ–‡é•¿è¯ {residual} å¤„ï¼ˆ>8ï¼Œå¯èƒ½æœªç¿»è¯‘å®Œæ•´æ®µè½ï¼‰")

    # Untranslated block detection: 3+ consecutive non-empty, non-heading,
    # non-math lines that are pure English â†’ likely a skipped section.
    untranslated = find_untranslated_blocks(body)
    if untranslated:
        locations = [f"Â§{h}" for h in untranslated[:3]]
        problems.append(f"å¯èƒ½æ¼ç¿» {len(untranslated)} å—ï¼š{', '.join(locations)}")

    # Truncation detection: Chinese is typically 30-50% shorter than English,
    # but the presence of LaTeX (kept verbatim) pushes the ratio up. A ratio
    # below 0.4 is suspicious and below 0.2 is almost certainly truncated.
    if source_body:
        ratio = len(body) / max(len(source_body), 1)
        if ratio < 0.2:
            problems.append(f"è¯‘æ–‡é•¿åº¦ä»…ä¸ºæºæ–‡çš„ {ratio:.0%}ï¼Œä¸¥é‡æˆªæ–­")
        elif ratio < 0.4:
            problems.append(f"è¯‘æ–‡é•¿åº¦ä»…ä¸ºæºæ–‡çš„ {ratio:.0%}ï¼Œå¯èƒ½éƒ¨åˆ†æˆªæ–­")

        # Paragraph-count alignment: more precise than char ratio for
        # formula-heavy chapters (LaTeX inflates source length but paragraphs
        # should map 1:1). A translation with <80% of source paragraphs
        # likely dropped content.
        src_paras = _count_content_paragraphs(source_body)
        dst_paras = _count_content_paragraphs(body)
        if src_paras >= 5 and dst_paras < src_paras * 0.8:
            problems.append(f"æ®µè½æ•°ä¸å¯¹é½ï¼ˆæº {src_paras} æ®µï¼Œè¯‘ {dst_paras} æ®µï¼‰")

    return len(problems) == 0, "; ".join(problems) if problems else "ok"


def _count_content_paragraphs(body: str) -> int:
    """Count substantive paragraphs (excludes pure-math and heading-only lines).

    A paragraph is a non-empty block separated by blank lines. We exclude
    blocks that are purely display math ($$...$$), pure headings, or pure
    image references â€” these survive translation verbatim and shouldn't
    inflate the count.
    """
    count = 0
    for block in re.split(r"\n\s*\n", body):
        block = block.strip()
        if not block:
            continue
        # Skip pure headings
        if re.match(r"^#{1,6}\s+", block) and "\n" not in block.strip():
            continue
        # Skip pure display math blocks
        if re.match(r"^\$\$.*\$\$$", block, re.DOTALL) and "$$" not in block[2:-2]:
            continue
        # Skip pure image references
        if re.match(r"^!\[.*\]\(.*\)$", block):
            continue
        count += 1
    return count


def find_untranslated_blocks(body: str):
    """Detect 3+ consecutive non-empty lines of pure English (likely untranslated).

    Returns list of preceding ##/### heading texts for each block, or ['<body start>'].
    """
    lines = body.split("\n")
    blocks = []
    current_block = []
    last_heading = ""
    prev_heading = ""

    for line in lines:
        stripped = line.strip()
        # Track headings for location context
        hm = re.match(r"^(#{1,3})\s+(.+)", stripped)
        if hm:
            prev_heading = hm.group(2).strip()[:40]
            if current_block and len(current_block) >= 3:
                blocks.append(last_heading or prev_heading)
            current_block = []
            last_heading = prev_heading
            continue

        if not stripped:
            if current_block and len(current_block) >= 3:
                blocks.append(last_heading or prev_heading)
            current_block = []
            continue

        # Skip math-only lines, front matter markers
        if re.match(r"^[\$\s\\\{\}_\^\[\]\d+\-*/=<>\(\),.]+$", stripped):
            continue

        # Skip lines that already have CJK
        if re.search(r"[ä¸€-é¿¿]", stripped):
            if current_block and len(current_block) >= 3:
                blocks.append(last_heading or prev_heading)
            current_block = []
            continue

        # Count English content: needs at least 5 alpha chars to count as "English line"
        if len(re.findall(r"[a-zA-Z]", stripped)) >= 5:
            current_block.append(stripped)
        else:
            if current_block and len(current_block) >= 3:
                blocks.append(last_heading or prev_heading)
            current_block = []

    if current_block and len(current_block) >= 3:
        blocks.append(last_heading or prev_heading)

    return blocks


def is_chinese_text(body: str) -> bool:
    """Detect if body is predominantly Chinese (skip LLM translation).

    Heuristic: count CJK chars vs ASCII letters. If CJK > 30% of (CJK+letters),
    treat as Chinese â€” don't send to LLM (would corrupt already-Chinese content).
    """
    cjk = len(re.findall(r"[ä¸€-é¿¿]", body))
    letters = len(re.findall(r"[a-zA-Z]", body))
    total = cjk + letters
    if total == 0:
        return False
    return cjk / total > 0.3


# â”€â”€ Reference section isolation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
REF_HEADING_RE = re.compile(r"^#+\s+(References|å‚è€ƒæ–‡çŒ®|Bibliography|æ–‡çŒ®)", re.MULTILINE | re.IGNORECASE)
NEXT_H2_RE = re.compile(r"^##\s+", re.MULTILINE)
# Bare reference entries at end of file: [N] Author, Title, Journal ...
# MinerU sometimes fails to extract the ## References heading, leaving raw
# [1] Author... lines at the very end with no heading at all.
BARE_REF_RE = re.compile(r"^\[\d+\]\s+")


def isolate_references(body: str):
    """Split body into (before_refs, ref_section, after_refs).

    Only the References section itself (## References / ## å‚è€ƒæ–‡çŒ® / ## Bibliography)
    is kept as-is â€” from its heading up to the next ## heading (or end of body).
    Authors, journal names, and titles stay in the original language.

    Any section after References (e.g. ## Appendix, ## Acknowledgements) is
    returned in after_refs so it gets translated normally.

    Fallback: if no ## References heading is found, detects bare [N] Author...
    lines at the end of the file (MinerU sometimes drops the heading).

    Returns (before_refs, ref_section_or_empty, after_refs_or_empty).
    """
    m = REF_HEADING_RE.search(body)
    if m:
        ref_start = m.start()
        # Find the next ## heading after the References heading (not ### or deeper)
        next_h2 = NEXT_H2_RE.search(body, m.end())
        ref_end = next_h2.start() if next_h2 else len(body)
        return body[:ref_start], body[ref_start:ref_end], body[ref_end:]

    # â”€â”€ Fallback: no heading â†’ detect bare [N] references at end of file â”€â”€
    lines = body.split("\n")
    # Scan backwards for consecutive bare reference lines
    ref_start_line = None
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if not stripped:
            continue
        if BARE_REF_RE.match(stripped):
            ref_start_line = i
        else:
            break

    if ref_start_line is not None and ref_start_line < len(lines) - 1:
        before = "\n".join(lines[:ref_start_line])
        ref_section = "## References\n\n" + "\n".join(lines[ref_start_line:])
        return before, ref_section, ""

    return body, "", ""


# â”€â”€ Echo stripping (deterministic safeguard against LLM echoing English originals) â”€
# Some models (DeepSeek) occasionally output the original English paragraph
# followed by its Chinese translation, instead of replacing it. Detect and strip.
_EN_SENTENCE_RE = re.compile(r"[A-Z][a-z].*[.?!]")  # English sentence (capital letter + period)
_CJK_RE = re.compile(r"[ä¸€-é¿¿ã€-ä¶¿]")  # CJK unified ideographs


def strip_echoed_english(text: str) -> tuple[str, int]:
    """Remove English paragraphs that are followed by a Chinese translation paragraph.

    Pattern detected: English-only para, blank line, Chinese para with similar meaning.
    The English para is the echoed original â€” remove it, keep the Chinese translation.

    Returns (cleaned_text, num_stripped).
    """
    paras = re.split(r"\n\n+", text)
    if len(paras) < 2:
        return text, 0

    stripped_count = 0
    result = []
    i = 0
    while i < len(paras):
        para = paras[i].strip()
 ïmµ¶‰Ëkºwµçq˜¹}±½…‘•€ô…±Í”((€€€€€€€¥˜™É•Í è(€€€€€€€€€€€É•ÑÕÉ¸€€Œ…±±•Èİ¥±°ÑÉ•…Ğ…Ì•µÁÑä((€€€€€€€Í•±˜¹}±½… ¤((€€€‘•˜}±½…¡Í•±˜¤è(€€€€€€€€ˆˆ‰1½…ÁÉ½É•ÍÌ¹©Í½¸…¹±½ÍÍ…Éä¹©Í½¸¸•É…‘”Ñ¼•µÁÑä½¸½ÉÉÕÁÑ¥½¸¸ˆˆˆ(€€€€€€€ÑÉäè(€€€€€€€€€€€¥˜½Ì¹Á…Ñ ¹•á¥ÍÑÌ¡Í•±˜¹ÁÉ½É•ÍÍ}Á…Ñ ¤è(€€€€€€€€€€€€€€€İ¥Ñ ½Á•¸¡Í•±˜¹ÁÉ½É•ÍÍ}Á…Ñ °•¹½‘¥¹œô‰ÕÑ˜´àˆ¤…Ì˜è(€€€€€€€€€€€€€€€€€€€Í•±˜¹}‘…Ñ„€ô©Í½¸¹±½…¡˜¤(€€€€€€€€€€€€€€€Í•±˜¹}±½…‘•€ôQÉÕ”(€€€€€€€•á•ÁĞ€¡©Í½¸¹)M=9•½‘•ÉÉ½È°=MÉÉ½È¤è(€€€€€€€€€€€Í•±˜¹}‘…Ñ„€ôíô€€Œ½ÉÉÕÁĞƒŠPÍÑ…ÉĞ™É•Í (€€€€€€€€€€€Í•±˜¹}±½…‘•€ô…±Í”((€€€€€€€€Œ1½…•á¥ÍÑ¥¹œ±½ÍÍ…ÉäÍ¼É•ÍÕµ”…¸É”µ¥¹©•ĞÑ•ÉµÌ¥¹Ñ¼Ñ¡”±¥Ù”‘¥Ğ(€€€€€€€ÑÉäè(€€€€€€€€€€€¥˜½Ì¹Á…Ñ ¹•á¥ÍÑÌ¡Í•±˜¹±½ÍÍ…Éå}Á…Ñ ¤è(€€€€€€€€€€€€€€€İ¥Ñ ½Á•¸¡Í•±˜¹±½ÍÍ…Éå}Á…Ñ °•¹½‘¥¹œô‰ÕÑ˜´àˆ¤…Ì˜è(€€€€€€€€€€€€€€€€€€€Í•±˜¹}±½ÍÍ…Éä€ô©Í½¸¹±½…¡˜¤(€€€€€€€•á•ÁĞ€¡©Í½¸¹)M=9•½‘•ÉÉ½È°=MÉÉ½È¤è(€€€€€€€€€€€Í•±˜¹}±½ÍÍ…Éä€ôíô((€€€€€€€€ŒI•‰Õ¥±±½ÍÍ…Éä™É½´Á•Èµ¡…ÁÑ•ÈÉ•½É‘Ì¥˜±½ÍÍ…Éä¹©Í½¸İ…Ìµ¥ÍÍ¥¹œ(€€€€€€€¥˜¹½ĞÍ•±˜¹}±½ÍÍ…Éä…¹Í•±˜¹}‘…Ñ„è(€€€€€€€€€€€™½ÈÉ•Œ¥¸Í•±˜¹}‘…Ñ„¹Ù…±Õ•Ì ¤è(€€€€€€€€€€€€€€€Í•±˜¹}±½ÍÍ…Éä¹ÕÁ‘…Ñ”¡É•Œ¹•Ğ ‰±½ÍÍ…Éå}Ñ•ÉµÌˆ°íô¤¤((€€€ÁÉ½Á•ÉÑä(€€€‘•˜±½ÍÍ…Éä¡Í•±˜¤€´ø‘¥Ğè(€€€€€€€É•ÑÕÉ¸‘¥Ğ¡Í•±˜¹}±½ÍÍ…Éä¤((€€€‘•˜Í¡½Õ±‘}Í­¥À¡Í•±˜°Á…Ñ èÍÑÈ¤€´ø‰½½°è(€€€€€€€€ˆˆ‰QÉÕ”¥˜¡…ÁÑ•Èİ…ÌÑÉ…¹Í±…Ñ•½¬…¹Í½ÕÉ”¥ÌÕ¹¡…¹•¸ˆˆˆ(€€€€€€€™¹…µ”€ô½Ì¹Á…Ñ ¹‰…Í•¹…µ”¡Á…Ñ ¤(€€€€€€€É•Œ€ôÍ•±˜¹}‘…Ñ„¹•Ğ¡™¹…µ”¤(€€€€€€€¥˜¹½ĞÉ•Œ½ÈÉ•Œ¹•Ğ ‰ÍÑ…ÑÕÌˆ¤€„ô€‰½¬ˆè(€€€€€€€€€€€É•ÑÕÉ¸…±Í”(€€€€€€€ÑÉäè(€€€€€€€€€€€É•ÑÕÉ¸É•Œ¹•Ğ ‰Í½ÕÉ•}¡…Í ˆ¤€ôô}Í¡„ÈÔÙ}™¥±”¡Á…Ñ ¤(€€€€€€€•á•ÁĞ=MÉÉ½Èè(€€€€€€€€€€€É•ÑÕÉ¸…±Í”((€€€‘•˜ÍÑ…ÑÕÍ}½˜¡Í•±˜°Á…Ñ èÍÑÈ¤€´øÍÑÈè(€€€€€€€É•ÑÕÉ¸Í•±˜¹}‘…Ñ„¹•Ğ¡½Ì¹Á…Ñ ¹‰…Í•¹…µ”¡Á…Ñ ¤°íô¤¹•Ğ ‰ÍÑ…ÑÕÌˆ°€‰Á•¹‘¥¹œˆ¤((€€€…Íå¹Œ‘•˜É•½É¡Í•±˜°Á…Ñ èÍÑÈ°ÍÑ…ÑÕÌèÍÑÈ°…ÑÑ•µÁÑÌè¥¹Ğ°±½ÍÍ…Éå}Ñ•ÉµÌè‘¥Ğ¤è(€€€€€€€€ˆˆ‰I•½É„¡…ÁÑ•ÈÌÉ•ÍÕ±Ğ…¹Á•ÉÍ¥ÍĞ¥µµ•‘¥…Ñ•±ä¸ˆˆˆ(€€€€€€€™¹…µ”€ô½Ì¹Á…Ñ ¹‰…Í•¹…µ”¡Á…Ñ ¤(€€€€€€€…Íå¹Œİ¥Ñ Í•±˜¹}±½¬è(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€Í½ÕÉ•}¡…Í €ô}Í¡„ÈÔÙ}™¥±”¡Á…Ñ ¤(€€€€€€€€€€€•á•ÁĞ=MÉÉ½Èè(€€€€€€€€€€€€€€€Í½ÕÉ•}¡…Í €ô€ˆˆ(€€€€€€€€€€€Í•±˜¹}‘…Ñ…m™¹…µ•t€ôì(€€€€€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆèÍÑ…ÑÕÌ°(€€€€€€€€€€€€€€€€‰Í½ÕÉ•}¡…Í ˆèÍ½ÕÉ•}¡…Í °(€€€€€€€€€€€€€€€€‰…ÑÑ•µÁÑÌˆè…ÑÑ•µÁÑÌ°(€€€€€€€€€€€€€€€€‰±½ÍÍ…Éå}Ñ•ÉµÌˆè±½ÍÍ…Éå}Ñ•ÉµÌ°(€€€€€€€€€€€ô(€€€€€€€€€€€€Œ5•É”Ñ•ÉµÌ¥¹Ñ¼Ñ¡”±¥Ù”±½ÍÍ…Éä(€€€€€€€€€€€Í•±˜¹}±½ÍÍ…Éä¹ÕÁ‘…Ñ”¡±½ÍÍ…Éå}Ñ•ÉµÌ¤(€€€€€€€€€€€Í•±˜¹}Á•ÉÍ¥ÍĞ ¤((€€€‘•˜}Á•ÉÍ¥ÍĞ¡Í•±˜¤è(€€€€€€€€ˆˆ‰]É¥Ñ”ÁÉ½É•ÍÌ¹©Í½¸€¬±½ÍÍ…Éä¹©Í½¸…Ñ½µ¥…±±ä€¡…±±•È¡½±‘Ì±½¬¤¸ˆˆˆ(€€€€€€€½Ì¹µ…­•‘¥ÉÌ¡Í•±˜¹ÍÑ…Ñ•}‘¥È°•á¥ÍÑ}½¬õQÉÕ”¤(€€€€€€€ÑµÀ€ôÍ•±˜¹ÁÉ½É•ÍÍ}Á…Ñ €¬€ˆ¹ÑµÀˆ(€€€€€€€İ¥Ñ ½Á•¸¡ÑµÀ°€‰Üˆ°•¹½‘¥¹œô‰ÕÑ˜´àˆ¤…Ì˜è(€€€€€€€€€€€©Í½¸¹‘ÕµÀ¡Í•±˜¹}‘…Ñ„°˜°•¹ÍÕÉ•}…Í¥¤õ…±Í”°¥¹‘•¹ĞôÈ¤(€€€€€€€½Ì¹É•Á±…”¡ÑµÀ°Í•±˜¹ÁÉ½É•ÍÍ}Á…Ñ ¤((€€€€€€€İ¥Ñ ½Á•¸¡Í•±˜¹±½ÍÍ…Éå}Á…Ñ °€‰Üˆ°•¹½‘¥¹œô‰ÕÑ˜´àˆ¤…Ì˜è(€€€€€€€€€€€©Í½¸¹‘ÕµÀ¡Í•±˜¹}±½ÍÍ…Éä°˜°•¹ÍÕÉ•}…Í¥¤õ…±Í”°¥¹‘•¹ĞôÈ¤((€€€‘•˜ÍÕµµ…Éä¡Í•±˜¤€´ø‘¥Ğè(€€€€€€€€ˆˆ‰I•ÑÕÉ¸í½¬°Í­¥ÁÁ•‘}Í••°µ…¹Õ…°°•ÉÉ½È°Á•¹‘¥¹ô½Õ¹ÑÌ¸ˆˆˆ(€€€€€€€½Õ¹ÑÌ€ôì‰½¬ˆè€À°€‰µ…¹Õ…°ˆè€À°€‰•ÉÉ½Èˆè€À°€‰Á•¹‘¥¹œˆè€À°€‰Í­¥ÁÁ•‘}¡¥¹•Í”ˆè€Áô(€€€€€€€™½ÈÉ•Œ¥¸Í•±˜¹}‘…Ñ„¹Ù…±Õ•Ì ¤è(€€€€€€€€€€€Ì€ôÉ•Œ¹•Ğ ‰ÍÑ…ÑÕÌˆ°€‰Á•¹‘¥¹œˆ¤(€€€€€€€€€€€¥˜Ì¥¸½Õ¹ÑÌè(€€€€€€€€€€€€€€€½Õ¹ÑÍmÍt€¬ô€Ä(€€€€€€€€€€€•±¥˜Ì€ôô€‰Í­¥ÁÁ•ˆè(€€€€€€€€€€€€€€€½Õ¹ÑÍl‰Í­¥ÁÁ•‘}¡¥¹•Í”‰t€¬ô€Ä(€€€€€€€É•ÑÕÉ¸½Õ¹ÑÌ(((ŒƒŠRŠR 5…¥¸İ½É­™±½ÜƒŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠR )…Íå¹Œ‘•˜ÑÉ…¹Í±…Ñ•}‰½½¬¡‰½½­}‘¥ÈèÍÑÈ°½¹ÕÉÉ•¹äè¥¹Ğ°Í••‘}½Õ¹Ğè¥¹Ğ°µ…á}É•ÑÉäè¥¹Ğ°(€€€€€€€€€€€€€€€€€€€€€€€€™É•Í è‰½½°€ô…±Í”°ÉÕ¹}Å„è‰½½°€ôQÉÕ”¤è(€€€¥˜¹½ĞA%}-dè(€€€€€€€ÁÉ¥¹Ğ ‰ÉÉ½ÈèAM-}A%}-d¹½ĞÍ•Ğ¸¡•¬€¹•¹Øˆ°™¥±”õÍåÌ¹ÍÑ‘•ÉÈ¤(€€€€€€€É•ÑÕÉ¸€Ä((€€€€Œ±¥•¹Ğƒ’â7–7¦r¢šŠSŠQ}ÑÉ…¹Í±…Ñ•}½¹”ƒšRçR …±±}±±µ}½¹”£–>3–6?¢º»¦¦4§(€€€€ŒÑÉ…¹Í±…Ñ•}¡…ÁÑ•Èƒj±¥•¹Ğƒ–>šVÃ’şwVd9½¹”£–BG–B;–ó–ºç¶û–B4§((€€€€Œ½±±•Ğ¡…ÁÑ•È™¥±•Ìè ¨¹µ€¬ÁÉ•™…”¹µ€¡¥˜•á¥ÍÑÌ¤°•á±Õ‘¥¹œ}¥¹‘•à¹µ(€€€™¥±•Ì€ôÍ½ÉÑ•¡±½ˆ¹±½ˆ¡½Ì¹Á…Ñ ¹©½¥¸¡‰½½­}‘¥È°€‰ ¨¹µˆ¤¤¤(€€€€Œ±Í¼¥¹±Õ‘”ÁÉ•™…”¹µ¥˜¥Ğ•á¥ÍÑÌ…¹¥Í¸Ğ…±É•…‘ä¥¸Ñ¡”±¥ÍĞ(€€€ÁÉ•™…”€ô½Ì¹Á…Ñ ¹©½¥¸¡‰½½­}‘¥È°€‰ÁÉ•™…”¹µˆ¤(€€€¥˜½Ì¹Á…Ñ ¹•á¥ÍÑÌ¡ÁÉ•™…”¤…¹ÁÉ•™…”¹½Ğ¥¸™¥±•Ìè(€€€€€€€™¥±•Ì¹¥¹Í•ÉĞ À°ÁÉ•™…”¤(€€€¥˜¹½Ğ™¥±•Ìè(€€€€€€€€Œ™…±±‰…¬è…¹ä€¹µ•á•ÁĞ}¥¹‘•à¹µ(€€€€€€€™¥±•Ì€ôÍ½ÉÑ•¡˜™½È˜¥¸±½ˆ¹±½ˆ¡½Ì¹Á…Ñ ¹©½¥¸¡‰½½­}‘¥È°€ˆ¨¹µˆ¤¤¥˜¹½Ğ˜¹•¹‘Íİ¥Ñ  ‰}¥¹‘•à¹µˆ¤¤(€€€¥˜¹½Ğ™¥±•Ìè(€€€€€€€ÁÉ¥¹Ğ¡˜‰9¼€¹µ™¥±•Ì™½Õ¹¥¸í‰½½­}‘¥Éôˆ°™¥±”õÍåÌ¹ÍÑ‘•ÉÈ¤(€€€€€€€É•ÑÕÉ¸€Ä((€€€Ñ½Ñ…°€ô±•¸¡™¥±•Ì¤(€€€Í••‘Ì€ô™¥±•ÍléÍ••‘}½Õ¹Ñt(€€€É•ÍĞ€ô™¥±•ÍmÍ••‘}½Õ¹Ğét(€€€ÑÉ…­•È€ôAÉ½É•ÍÍQÉ…­•È¡‰½½­}‘¥È°™É•Í õ™É•Í ¤(€€€±½ÍÍ…Éä€ôÑÉ…­•È¹±½ÍÍ…Éä€€ŒÉ•ÍÕµ”èÁ¥¬ÕÀÑ•ÉµÌ™É½´ÁÉ¥½ÈÉÕ¸(€€€½¹™±¥ÑÌ€ômt(€€€É•ÍÕ±ÑÌ€ômt((€€€€ŒA…ÉÑ¥Ñ¥½¸èÍ­¥À…±É•…‘äµ½¬¡…ÁÑ•ÉÌ°ÑÉ…¹Í±…Ñ”Ñ¡”É•ÍĞ(€€€Í­¥ÁÁ•‘}™¥±•Ì€ôm˜™½È˜¥¸™¥±•Ì¥˜ÑÉ…­•È¹Í¡½Õ±‘}Í­¥À¡˜¥t(€€€Ñ½‘½}™¥±•Ì€ôm˜™½È˜¥¸™¥±•Ì¥˜¹½ĞÑÉ…­•È¹Í¡½Õ±‘}Í­¥À¡˜¥t(€€€¥˜Í­¥ÁÁ•‘}™¥±•Ìè(€€€€€€€ÁÉ¥¹Ğ¡˜‹šZ·
çî·¢ŞG¾òk¢ŞÏ¢şí±•¸¡Í­¥ÁÁ•‘}™¥±•Ì¥ôƒ®ƒ–ŞË–º3š"C¾ò#–ÄíÑ½Ñ…±ôƒ®ƒ¾ò$ˆ¤(€€€€€€€™½È˜¥¸Í­¥ÁÁ•‘}™¥±•Ìè(€€€€€€€€€€€™¹…µ”€ô½Ì¹Á…Ñ ¹‰…Í•¹…µ”¡˜¤(€€€€€€€€€€€É•ÍÕ±ÑÌ¹…ÁÁ•¹ ¡™¹…µ”°ÑÉ…­•È¹ÍÑ…ÑÕÍ}½˜¡˜¤½È€‰½¬ˆ°€À°€‹–ŞËòO–¶`ˆ¤¤(€€€¥˜¹½ĞÑ½‘½}™¥±•Ìè(€€€€€€€ÁÉ¥¹Ğ ‹š&šr'®ƒ¢*–ŞËşï¢¾G–º3š"C¾ò3š^ƒ¦r¦7¢ŞGˆ¤(€€€€€€€±½ÍÍ…Éä€ôÑÉ…­•È¹±½ÍÍ…Éä(€€€•±Í”è(€€€€€€€€ŒI”µÁ…ÉÑ¥Ñ¥½¸Ñ½‘¼¥¹Ñ¼Í••‘Ì½É•ÍĞ™½ÈÑ¡”…Ñ¥Ù”ÉÕ¸(€€€€€€€…Ñ¥Ù•}Í••‘Ì€ôm˜™½È˜¥¸Ñ½‘½}™¥±•Ì¥˜˜¥¸Í••‘ÍuléÍ••‘}½Õ¹Ñt(€€€€€€€…Ñ¥Ù•}É•ÍĞ€ôm˜™½È˜¥¸Ñ½‘½}™¥±•Ì¥˜˜¹½Ğ¥¸…Ñ¥Ù•}Í••‘Ít(€€€€€€€€Œ%˜…±°Í••‘Ìİ•É”Í­¥ÁÁ•°ÁÉ½µ½Ñ”•…É±¥•ÍĞÑ½‘¼¡…ÁÑ•ÉÌ…ÌÍ••‘Ì¸(€€€€€€€€ŒAÉ½µ½Ñ•¡…ÁÑ•ÉÌ…É”9=PÑÉÕ”Í••‘ÌƒŠPÑ¡”±½ÍÍ…Éä…±É•…‘ä•á¥ÍÑÌ™É½´(€€€€€€€€ŒÑ¡”ÁÉ¥½ÈÉÕ¸°Í¼¥Í}Í••µÕÍĞ‰”…±Í”€¡Í••õQÉÕ”Í­¥ÁÌ±½ÍÍ…Éä½¹Ñ•áĞ¤¸(€€€€€€€ÁÉ½µ½Ñ•€ô…±Í”(€€€€€€€¥˜¹½Ğ…Ñ¥Ù•}Í••‘Ì…¹…Ñ¥Ù•}É•ÍĞè(€€€€€€€€€€€…Ñ¥Ù•}Í••‘Ì€ô…Ñ¥Ù•}É•ÍÑléµ¥¸¡Í••‘}½Õ¹Ğ°±•¸¡…Ñ¥Ù•}É•ÍĞ¤¥t(€€€€€€€€€€€…Ñ¥Ù•}É•ÍĞ€ô…Ñ¥Ù•}É•ÍÑm±•¸¡…Ñ¥Ù•}Í••‘Ì¤ét(€€€€€€€€€€€ÁÉ½µ½Ñ•€ôQÉÕ”((€€€€€€€ÁÉ¥¹Ğ¡˜‹şï¢¾Dí±•¸¡Ñ½‘½}™¥±•Ì¥ôƒ®€ğƒ7–¶@í±•¸¡…Ñ¥Ù•}Í••‘Ì¥ôƒ’âË¢†0€¬í±•¸¡…Ñ¥Ù•}É•ÍĞ¥ôƒ–æÛ¢†0£–æÛ–>Dõí½¹ÕÉÉ•¹åô¤ˆ¤(€€€€€€€¥˜ÁÉ½µ½Ñ•è(€€€€€€€€€€€ÁÉ¥¹Ğ¡˜ˆ€€£7–¶C–ŞË–r£’â+š²‡–º3š"C¾ò3š>C–6í±•¸¡…Ñ¥Ù•}Í••‘Ì¥ôƒ®ƒ’âË¢†3¢†—¢¾G¾ò3–’7R£–ŞËšr'šr¿¢¾·¢† ¤ˆ¤(€€€€€€€ÁÉ¥¹Ğ¡˜‰AÉ½Ù¥‘•Èèí	M}UI1ôğ5½‘•°èí5=1õq¸ˆ¤((€€€€€€€€ŒƒŠRŠR A¡…Í”€ÄèÍ••¡…ÁÑ•ÉÌ€¡Í•É¥…°¤ƒŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠR (€€€€€€€€Œ¥Í}Í••½¹±äİ¡•¸Ñ¡•É”Ì¹¼±½ÍÍ…Éäå•Ğ€¡ÑÉÕ”™¥ÉÍĞÉÕ¸¤¸(€€€€€€€€ŒAÉ½µ½Ñ•¡…ÁÑ•ÉÌ½¸É•ÍÕµ”ÕÍ”Ñ¡”•á¥ÍÑ¥¹œ±½ÍÍ…ÉäƒŠH¥Í}Í••õ…±Í”¸(€€€€€€€Í••‘}¥Í}Í••€ô¹½Ğ‰½½°¡±½ÍÍ…Éä¤(€€€€€€€™½È¤°˜¥¸•¹Õµ•É…Ñ”¡…Ñ¥Ù•}Í••‘Ì¤è(€€€€€€€€€€€™¹…µ”€ô½Ì¹Á…Ñ ¹‰…Í•¹…µ”¡˜¤(€€€€€€€€€€€ÁÉ¥¹Ğ¡˜‰mí¤¬Åô½í±•¸¡Ñ½‘½}™¥±•Ì¥õtí™¹…µ•ôƒşï¢¾G’â´¸¸¸ˆ°•¹ôˆˆ°™±ÕÍ õQÉÕ”¤(€€€€€€€€€€€ÍÑ…ÑÕÌ°É•ÑÉ¥•Ì°¥ÍÍÕ•Ì°¹•İ}Ñ•ÉµÌ€ô…İ…¥ĞÑÉ…¹Í±…Ñ•}¡…ÁÑ•È (€€€€€€€€€€€€€€€9½¹”°˜°±½ÍÍ…Éä°¥Í}Í••õÍ••‘}¥Í}Í••°µ…á}É•ÑÉäõµ…á}É•ÑÉä°(€€€€€€€€€€€€€€€Í•´õ…Íå¹¥¼¹M•µ…Á¡½É” Ä¤(€€€€€€€€€€€€¤(€€€€€€€€€€€µ•É•}±½ÍÍ…Éä¡±½ÍÍ…Éä°¹•İ}Ñ•ÉµÌ°™¹…µ”°½¹™±¥ÑÌ¤(€€€€€€€€€€€…İ…¥ĞÑÉ…­•È¹É•½É¡˜°ÍÑ…ÑÕÌ°É•ÑÉ¥•Ì°¹•İ}Ñ•ÉµÌ¤(€€€€€€€€€€€Ñ…œ€ô€‹ŠrLˆ¥˜ÍÑ…ÑÕÌ€ôô€‰½¬ˆ•±Í”€ ‹Š>´ˆ¥˜ÍÑ…ÑÕÌ€ôô€‰Í­¥ÁÁ•ˆ•±Í”€ ‹Šj€ˆ¥˜ÍÑ…ÑÕÌ€ôô€‰µ…¹Õ…°ˆ•±Í”€‹Šr\ˆ¤¤(€€€€€€€€€€€É•ÑÉå}¥¹™¼€ô˜ˆ€£¦7¢¾UíÉ•ÑÉ¥•Í÷š²„¤ˆ¥˜É•ÑÉ¥•Ì•±Í”€ˆˆ(€€€€€€€€€€€ÁÉ¥¹Ğ¡˜‰qÉíÑ…ômí¤¬Åô½í±•¸¡Ñ½‘½}™¥±•Ì¥õtí™¹…µ•õíÉ•ÑÉå}¥¹™½ôƒŠPí¥ÍÍÕ•Íôˆ¤(€€€€€€€€€€€É•ÍÕ±ÑÌ¹…ÁÁ•¹ ¡™¹…µ”°ÍÑ…ÑÕÌ°É•ÑÉ¥•Ì°¥ÍÍÕ•Ì¤¤((€€€€€€€€ŒƒŠRŠR A¡…Í”€ÈèÉ•µ…¥¹¥¹œ¡…ÁÑ•ÉÌ€¡Á…É…±±•°¤ƒŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠR (€€€€€€€¥˜…Ñ¥Ù•}É•ÍĞè(€€€€€€€€€€€Í•´€ô…Íå¹¥¼¹M•µ…Á¡½É”¡½¹ÕÉÉ•¹ä¤(€€€€€€€€€€€½™™Í•Ğ€ô±•¸¡…Ñ¥Ù•}Í••‘Ì¤€¬±•¸¡Í­¥ÁÁ•‘}™¥±•Ì¤((€€€€€€€€€€€…Íå¹Œ‘•˜ÉÕ¹}½¹”¡¥‘à°™Á…Ñ ¤è(€€€€€€€€€€€€€€€™¹…µ”€ô½Ì¹Á…Ñ ¹‰…Í•¹…µ”¡™Á…Ñ ¤(€€€€€€€€€€€€€€€€ŒÍ¹…ÁÍ¡½Ğ±½ÍÍ…Éä…ĞÑ…Í¬É•…Ñ¥½¸Ñ¥µ”€¡Í••Ñ•ÉµÌ¤(€€€€€€€€€€€€€€€°€ô‘¥Ğ¡±½ÍÍ…Éä¤(€€€€€€€€€€€€€€€ÍÑ…ÑÕÌ°É•ÑÉ¥•Ì°¥ÍÍÕ•Ì°¹•İ}Ñ•ÉµÌ€ô…İ…¥ĞÑÉ…¹Í±…Ñ•}¡…ÁÑ•È (€€€€€€€€€€€€€€€€€€€9½¹”°™Á…Ñ °°°¥Í}Í••õ…±Í”°µ…á}É•ÑÉäõµ…á}É•ÑÉä°Í•´õÍ•´(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€µ•É•}±½ÍÍ…Éä¡±½ÍÍ…Éä°¹•İ}Ñ•ÉµÌ°™¹…µ”°½¹™±¥ÑÌ¤(€€€€€€€€€€€€€€€…İ…¥ĞÑÉ…­•È¹É•½É¡™Á…Ñ °ÍÑ…ÑÕÌ°É•ÑÉ¥•Ì°¹•İ}Ñ•ÉµÌ¤(€€€€€€€€€€€€€€€Ñ…œ€ô€‹ŠrLˆ¥˜ÍÑ…ÑÕÌ€ôô€‰½¬ˆ•±Í”€ ‹Š>´ˆ¥˜ÍÑ…ÑÕÌ€ôô€‰Í­¥ÁÁ•ˆ•±Í”€ ‹Šj€ˆ¥˜ÍÑ…ÑÕÌ€ôô€‰µ…¹Õ…°ˆ•±Í”€‹Šr\ˆ¤¤(€€€€€€€€€€€€€€€É•ÑÉå}¥¹™¼€ô˜ˆ€£¦7¢¾UíÉ•ÑÉ¥•Í÷š²„¤ˆ¥˜É•ÑÉ¥•Ì•±Í”€ˆˆ(€€€€€€€€€€€€€€€ÁÉ¥¹Ğ¡˜‰íÑ…ômí½™™Í•Ğ­¥‘à¬Åô½íÑ½Ñ…±õtí™¹…µ•õíÉ•ÑÉå}¥¹™½ôƒŠPí¥ÍÍÕ•Íôˆ¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸€¡™¹…µ”°ÍÑ…ÑÕÌ°É•ÑÉ¥•Ì°¥ÍÍÕ•Ì¤((€€€€€€€€€€€Ñ…Í­Ì€ômÉÕ¹}½¹”¡¤°˜¤™½È¤°˜¥¸•¹Õµ•É…Ñ”¡…Ñ¥Ù•}É•ÍĞ¥t(€€€€€€€€€€€Á…É…±±•±}É•ÍÕ±ÑÌ€ô…İ…¥Ğ…Íå¹¥¼¹…Ñ¡•È ©Ñ…Í­Ì°É•ÑÕÉ¹}•á•ÁÑ¥½¹ÌõQÉÕ”¤(€€€€€€€€€€€™½ÈÈ¥¸Á…É…±±•±}É•ÍÕ±ÑÌè(€€€€€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡È°á•ÁÑ¥½¸¤è(€€€€€€€€€€€€€€€€€€€É•ÍÕ±ÑÌ¹…ÁÁ•¹  ‰Õ¹­¹½İ¸ˆ°€‰•ÉÉ½Èˆ°€À°ÍÑÈ¡È¤¤¤(€€€€€€€€€€€€€€€•±Í”è(€€€€€€€€€€€€€€€€€€€É•ÍÕ±ÑÌ¹…ÁÁ•¹¡È¤((€€€€ŒƒŠRŠR I•Á½ÉĞƒŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠR (€€€…¡•€ô±•¸¡Í­¥ÁÁ•‘}™¥±•Ì¤(€€€½¬€ôÍÕ´ Ä™½È|°Ì°|°|¥¸É•ÍÕ±ÑÌ¥˜Ì€ôô€‰½¬ˆ¤(€€€Í­¥ÁÁ•€ôÍÕ´ Ä™½È|°Ì°|°|¥¸É•ÍÕ±ÑÌ¥˜Ì€ôô€‰Í­¥ÁÁ•ˆ¤(€€€µ…¹Õ…°€ôÍÕ´ Ä™½È|°Ì°|°|¥¸É•ÍÕ±ÑÌ¥˜Ì€ôô€‰µ…¹Õ…°ˆ¤(€€€•ÉÉ½ÉÌ€ôÍÕ´ Ä™½È|°Ì°|°|¥¸É•ÍÕ±ÑÌ¥˜Ì€ôô€‰•ÉÉ½Èˆ¤((€€€ÁÉ¥¹Ğ¡˜‰q¹ìœôœ¨ØÁôˆ¤(€€€ÁÉ¥¹Ğ¡˜‹şï¢¾G–º3š"C¾òií½­ôƒ¦k¢ş£–B¬í…¡•‘ôƒòO–¶`¤€¼íÍ­¥ÁÁ•‘ôƒ¢ŞÏ¢ş£’â·šZ¤€¼íµ…¹Õ…±ôƒ¦r’êë–Ş”€¼í•ÉÉ½ÉÍôƒ¦Rg¢¾¿¾ò#–ÄíÑ½Ñ…±ôƒ®ƒ¾ò$ˆ¤(€€€ÁÉ¥¹Ğ¡˜‹šr¿¢¾·¢†£¾òií±•¸¡±½ÍÍ…Éä¥ôƒšv„ƒŠHíÑÉ…­•È¹±½ÍÍ…Éå}Á…Ñ¡ôˆ¤(€€€¥˜½¹™±¥ÑÌè(€€€€€€€ÁÉ¥¹Ğ¡˜‰q»Šj€ƒšr¿¢¾·–Ëªí±•¸¡½¹™±¥ÑÌ¥ôƒ–’¾òhˆ¤(€€€€€€€™½ÈŒ¥¸½¹™±¥ÑÌè(€€€€€€€€€€€ÁÉ¥¹Ğ¡˜ˆ€ílÑ•É´uôèíl•á¥ÍÑ¥¹œuôÙÌíl¹•Üuô€¡¥¸ílÍ½ÕÉ”uô¤ˆ¤((€€€¥˜µ…¹Õ…°è(€€€€€€€ÁÉ¥¹Ğ ‰q»¦r’êë–Ş—šš~—j®ƒ¢*¾òhˆ¤(€€€€€€€™½È™¹…µ”°ÍÑ…ÑÕÌ°|°¥ÍÍÕ•Ì¥¸É•ÍÕ±ÑÌè(€€€€€€€€€€€¥˜ÍÑ…ÑÕÌ€ôô€‰µ…¹Õ…°ˆè(€€€€€€€€€€€€€€€ÁÉ¥¹Ğ¡˜ˆ€í™¹…µ•ôèí¥ÍÍÕ•Íôˆ¤((€€€€ŒƒŠRŠR ½¹Í¥ÍÑ•¹äE€¡½ÁÑ¥½¹…°°…™Ñ•ÈÑÉ…¹Í±…Ñ¥½¸¤ƒŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠR (€€€¥˜ÉÕ¹}Å„…¹}A%A1%9¹•Ğ ‰½¹Í¥ÍÑ•¹å}Å„ˆ¤…¹¹½Ğ•ÉÉ½ÉÌè(€€€€€€€ÑÉäè(€€€€€€€€€€€™É½´½¹Í¥ÍÑ•¹å}Å„¥µÁ½ÉĞÉÕ¹}½¹Í¥ÍÑ•¹å}Å„(€€€€€€€€€€€ÁÉ¥¹Ğ ‰q»¢Ş£®ƒ’â¢ÓšŸš&¯š><¸¸¸ˆ¤(€€€€€€€€€€€Å…}¥ÍÍÕ•Ì€ô…İ…¥ĞÉÕ¹}½¹Í¥ÍÑ•¹å}Å„¡‰½½­}‘¥È°±½ÍÍ…Éä¤(€€€€€€€€€€€¥˜Å…}¥ÍÍÕ•Ìè(€€€€€€€€€€€€€€€É•Á½ÉÑ}Á…Ñ €ô½Ì¹Á…Ñ ¹©½¥¸¡‰½½­}‘¥È°€ˆ¹ÑÉ…¹Í±…Ñ•}ÍÑ…Ñ”ˆ°€‰½¹Í¥ÍÑ•¹å}É•Á½ÉĞ¹µˆ¤(€€€€€€€€€€€€€€€½Ì¹µ…­•‘¥ÉÌ¡½Ì¹Á…Ñ ¹‘¥É¹…µ”¡É•Á½ÉÑ}Á…Ñ ¤°•á¥ÍÑ}½¬õQÉÕ”¤(€€€€€€€€€€€€€€€İ¥Ñ ½Á•¸¡É•Á½ÉÑ}Á…Ñ °€‰Üˆ°•¹½‘¥¹œô‰ÕÑ˜´àˆ¤…Ì˜è(€€€€€€€€€€€€€€€€€€€˜¹İÉ¥Ñ”¡Å…}¥ÍÍÕ•Ì¤(€€€€€€€€€€€€€€€ÁÉ¥¹Ğ¡˜ˆ€ƒ–>G:ÀíÅ…}¥ÍÍÕ•Ì¹½Õ¹Ğ œŒŒŒœ¥ôƒÆï¦^»¦Š`ƒŠHíÉ•Á½ÉÑ}Á…Ñ¡ôˆ¤(€€€€€€€€€€€•±Í”è(€€€€€€€€€€€€€€€ÁÉ¥¹Ğ ˆ€ƒŠrLƒš^ƒ’â¢ÓšŸ¦^»¦Š`ˆ¤(€€€€€€€•á•ÁĞ%µÁ½ÉÑÉÉ½Èè(€€€€€€€€€€€Á…ÍÌ€€Œ½¹Í¥ÍÑ•¹å}Å„¹Áä¹½Ğ…Ù…¥±…‰±”ƒŠPÍ­¥ÀÍ¥±•¹Ñ±ä(€€€€€€€•á•ÁĞá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€ÁÉ¥¹Ğ¡˜ˆ€ƒŠj€ƒ’â¢ÓšŸš&¯š>?–’Ç¢Ò”èí•ôˆ°™¥±”õÍåÌ¹ÍÑ‘•ÉÈ¤((€€€É•ÑÕÉ¸€Ä¥˜•ÉÉ½ÉÌ•±Í”€À(()…Íå¹Œ‘•˜ÑÉ…¹Í±…Ñ•}Í¥¹±”¡Á…Ñ èÍÑÈ°µ…á}É•ÑÉäè¥¹Ğ°½¹}ÁÉ½É•ÍÌõ9½¹”¤è(€€€€ˆˆ‰M¥¹±”µ™¥±”µ½‘”€¡™½ÈÁ…Á•ÉÌ¤¸9¼±½ÍÍ…Éä¸((€€€½¹}ÁÉ½É•ÍÌ¡‘½¹”°Ñ½Ñ…°¤¥Ì™½Éİ…É‘•Ñ¼ÑÉ…¹Í±…Ñ•}¡…ÁÑ•È™½È¡Õ¹¬µ±•Ù•°(€€€ÁÉ½É•ÍÌÉ•Á½ÉÑ¥¹œ€¡”¹œ¸Ñ¼„©½ˆ±½œ¤¸(€€€€ˆˆˆ(€€€¥˜¹½ĞA%}-dè(€€€€€€€ÁÉ¥¹Ğ ‰ÉÉ½ÈèAM-}A%}-d¹½ĞÍ•Ğ¸ˆ°™¥±”õÍåÌ¹ÍÑ‘•ÉÈ¤(€€€€€€€É•ÑÕÉ¸€Ä((€€€™¹…µ”€ô½Ì¹Á…Ñ ¹‰…Í•¹…µ”¡Á…Ñ ¤(€€€ÁÉ¥¹Ğ¡˜‹şï¢¾Dí™¹…µ•ô¸¸¸ˆ¤((€€€ÍÑ…ÑÕÌ°É•ÑÉ¥•Ì°¥ÍÍÕ•Ì°|€ô…İ…¥ĞÑÉ…¹Í±…Ñ•}¡…ÁÑ•È (€€€€€€€9½¹”°Á…Ñ °íô°¥Í}Í••õ…±Í”°µ…á}É•ÑÉäõµ…á}É•ÑÉä°Í•´õ…Íå¹¥¼¹M•µ…Á¡½É” Ä¤°¥¹}Á±…”õ…±Í”°½¹}ÁÉ½É•ÍÌõ½¹}ÁÉ½É•ÍÌ(€€€€¤(€€€Ñ…œ€ô€‹ŠrLˆ¥˜ÍÑ…ÑÕÌ€ôô€‰½¬ˆ•±Í”€ ‹Š>´ˆ¥˜ÍÑ…ÑÕÌ€ôô€‰Í­¥ÁÁ•ˆ•±Í”€ ‹Šj€ˆ¥˜ÍÑ…ÑÕÌ€ôô€‰µ…¹Õ…°ˆ•±Í”€‹Šr\ˆ¤¤(€€€½ÕĞ€ôÑÉ…¹Í±…Ñ•‘}Á…Ñ ¡Á…Ñ ¤(€€€ÁÉ¥¹Ğ¡˜‰íÑ…ôí™¹…µ•ôƒŠHí½Ì¹Á…Ñ ¹‰…Í•¹…µ”¡½ÕĞ¥ô€£¦7¢¾UíÉ•ÑÉ¥•Í÷š²„¤ƒŠPí¥ÍÍÕ•Íôˆ¤(€€€É•ÑÕÉ¸€Ä¥˜ÍÑ…ÑÕÌ€ôô€‰•ÉÉ½Èˆ•±Í”€À(()‘•˜}Í¡½İ}ÍÑ…ÑÕÌ¡‰½½­}‘¥ÈèÍÑÈ¤è(€€€€ˆˆ‰AÉ¥¹ĞÑÉ…¹Í±…Ñ¥½¸ÁÉ½É•ÍÌİ¥Ñ¡½ÕĞÑÉ…¹Í±…Ñ¥¹œ¸ˆˆˆ(€€€ÑÉ…­•È€ôAÉ½É•ÍÍQÉ…­•È¡‰½½­}‘¥È¤(€€€¥˜¹½ĞÑÉ…­•È¹}±½…‘•è(€€€€€€€ÁÉ¥¹Ğ ‹š^ƒşï¢¾G*ÛššZ’îÛ¾ò ¹ÑÉ…¹Í±…Ñ•}ÍÑ…Ñ”½ÁÉ½É•ÍÌ¹©Í½»¾ò'ˆ¤(€€€€€€€É•ÑÕÉ¸€À(€€€™¥±•Ì€ôÍ½ÉÑ•¡±½ˆ¹±½ˆ¡½Ì¹Á…Ñ ¹©½¥¸¡‰½½­}‘¥È°€‰ ¨¹µˆ¤¤¤(€€€¥˜¹½Ğ™¥±•Ìè(€€€€€€€™¥±•Ì€ôÍ½ÉÑ•¡˜™½È˜¥¸±½ˆ¹±½ˆ¡½Ì¹Á…Ñ ¹©½¥¸¡‰½½­}‘¥È°€ˆ¨¹µˆ¤¤¥˜¹½Ğ˜¹•¹‘Íİ¥Ñ  ‰}¥¹‘•à¹µˆ¤¤(€€€½Õ¹ÑÌ€ôì‰½¬ˆè€À°€‰Í­¥ÁÁ•ˆè€À°€‰µ…¹Õ…°ˆè€À°€‰•ÉÉ½Èˆè€À°€‰Á•¹‘¥¹œˆè€Áô(€€€ÁÉ¥¹Ğ¡˜‰ìŸ®ƒ¢*œèğÌÁôìŸ*ÛšœèğÄÁôìŸ¦7¢¾TœèğÙôƒ¢¾Óšb8ˆ¤(€€€ÁÉ¥¹Ğ ˆ´ˆ€¨€ÜÀ¤(€€€™½È˜¥¸™¥±•Ìè(€€€€€€€™¹…µ”€ô½Ì¹Á…Ñ ¹‰…Í•¹…µ”¡˜¤(€€€€€€€¥˜ÑÉ…­•È¹Í¡½Õ±‘}Í­¥À¡˜¤è(€€€€€€€€€€€ÍÑ…ÑÕÌ°É•ÑÉ¥•Ì°¥ÍÍÕ•Ì€ô€‰½¬ˆ°€À°€‹–ŞËòO–¶`£šêCšr«–>`¤ˆ(€€€€€€€•±Í”è(€€€€€€€€€€€É•Œ€ôÑÉ…­•È¹}‘…Ñ„¹•Ğ¡™¹…µ”°íô¤(€€€€€€€€€€€ÍÑ…ÑÕÌ€ôÉ•Œ¹•Ğ ‰ÍÑ…ÑÕÌˆ°€‰Á•¹‘¥¹œˆ¤(€€€€€€€€€€€É•ÑÉ¥•Ì€ôÉ•Œ¹•Ğ ‰…ÑÑ•µÁÑÌˆ°€À¤(€€€€€€€€€€€¥ÍÍÕ•Ì€ô€ˆˆ¥˜ÍÑ…ÑÕÌ€ôô€‰Á•¹‘¥¹œˆ•±Í”€‹¦r¦7¢ŞDˆ(€€€€€€€½Õ¹ÑÍmÍÑ…ÑÕÍt€ô½Õ¹ÑÌ¹•Ğ¡ÍÑ…ÑÕÌ°€À¤€¬€Ä(€€€€€€€ÁÉ¥¹Ğ¡˜‰í™¹…µ”èğÌÁôíÍÑ…ÑÕÌèğÄÁôíÉ•ÑÉ¥•ÌèğÙôí¥ÍÍÕ•Íôˆ¤(€€€ÁÉ¥¹Ğ ˆ´ˆ€¨€ÜÀ¤(€€€ÁÉ¥¹Ğ¡˜‹šï¢º‡¾òií½Õ¹ÑÌ¹•Ğ ½¬œ°À¥ôƒ¦k¢ş€¼í½Õ¹ÑÌ¹•Ğ Í­¥ÁÁ•œ°À¥ôƒ’â·šZ¢ŞÏ¢ş€¼€ˆ(€€€€€€€€€˜‰í½Õ¹ÑÌ¹•Ğ µ…¹Õ…°œ°À¥ôƒ¦r’êë–Ş”€¼í½Õ¹ÑÌ¹•Ğ •ÉÉ½Èœ°À¥ôƒ¦Rg¢¾¼€¼€ˆ(€€€€€€€€€˜‰í½Õ¹ÑÌ¹•Ğ Á•¹‘¥¹œœ°À¥ôƒ–úşï¢¾G¾ò#–Äí±•¸¡™¥±•Ì¥ôƒ®ƒ¾ò$ˆ¤(€€€É•ÑÕÉ¸€À(()‘•˜µ…¥¸ ¤è(€€€…À€ô…ÉÁ…ÉÍ”¹ÉÕµ•¹ÑA…ÉÍ•È¡‘•ÍÉ¥ÁÑ¥½¸ô‰QÉ…¹Í±…Ñ”µ…É­‘½İ¸¡…ÁÑ•ÉÌÑ¼¡¥¹•Í”¸ˆ¤(€€€…À¹…‘‘}…ÉÕµ•¹Ğ ‰Ñ…É•Ğˆ°¡•±Àô‰‰½½¬‘¥É•Ñ½Éä½ÈÍ¥¹±”€¹µ™¥±”ˆ¤(€€€…À¹…‘‘}…ÉÕµ•¹Ğ ˆ´µ½¹ÕÉÉ•¹äˆ°ÑåÁ”õ¥¹Ğ°‘•™…Õ±ĞôĞ°¡•±Àô‰Á…É…±±•°¡…ÁÑ•ÉÌ€¡‘•™…Õ±Ğ€Ğ¤ˆ¤(€€€…À¹…‘‘}…ÉÕµ•¹Ğ ˆ´µÍ••µ¡…ÁÑ•ÉÌˆ°ÑåÁ”õ¥¹Ğ°‘•™…Õ±ĞôÈ°¡•±Àô‰Í•É¥…°Í••¡…ÁÑ•ÉÌ€¡‘•™…Õ±Ğ€È¤ˆ¤(€€€…À¹…‘‘}…ÉÕµ•¹Ğ ˆ´µÉ•ÑÉäˆ°ÑåÁ”õ¥¹Ğ°‘•™…Õ±ĞôÈ°¡•±Àô‰µ…àÉ•ÑÉ¥•ÌÁ•È¡…ÁÑ•È€¡‘•™…Õ±Ğ€È¤ˆ¤(€€€…À¹…‘‘}…ÉÕµ•¹Ğ ˆ´µ™É•Í ˆ°…Ñ¥½¸ô‰ÍÑ½É•}ÑÉÕ”ˆ°¡•±Àô‰¥¹½É”ÍÑ…Ñ”°É”µÑÉ…¹Í±…Ñ”…±°¡…ÁÑ•ÉÌˆ¤(€€€…À¹…‘‘}…ÉÕµ•¹Ğ ˆ´µÍÑ…ÑÕÌˆ°…Ñ¥½¸ô‰ÍÑ½É•}ÑÉÕ”ˆ°¡•±Àô‰Í¡½ÜÁÉ½É•ÍÌİ¥Ñ¡½ÕĞÑÉ…¹Í±…Ñ¥¹œˆ¤(€€€…À¹…‘‘}…ÉÕµ•¹Ğ ˆ´µ¹¼µÅ„ˆ°…Ñ¥½¸ô‰ÍÑ½É•}ÑÉÕ”ˆ°¡•±Àô‰Í­¥ÀÉ½ÍÌµ¡…ÁÑ•È½¹Í¥ÍÑ•¹äEˆ¤(€€€…ÉÌ€ô…À¹Á…ÉÍ•}…ÉÌ ¤((€€€¥˜½Ì¹Á…Ñ ¹¥Í™¥±”¡…ÉÌ¹Ñ…É•Ğ¤è(€€€€€€€É•ÑÕÉ¸…Íå¹¥¼¹ÉÕ¸¡ÑÉ…¹Í±…Ñ•}Í¥¹±”¡…ÉÌ¹Ñ…É•Ğ°…ÉÌ¹É•ÑÉä¤¤(€€€•±¥˜½Ì¹Á…Ñ ¹¥Í‘¥È¡…ÉÌ¹Ñ…É•Ğ¤è(€€€€€€€¥˜…ÉÌ¹ÍÑ…ÑÕÌè(€€€€€€€€€€€É•ÑÕÉ¸}Í¡½İ}ÍÑ…ÑÕÌ¡…ÉÌ¹Ñ…É•Ğ¤(€€€€€€€É•ÑÕÉ¸…Íå¹¥¼¹ÉÕ¸¡ÑÉ…¹Í±…Ñ•}‰½½¬ (€€€€€€€€€€€…ÉÌ¹Ñ…É•Ğ°…ÉÌ¹½¹ÕÉÉ•¹ä°…ÉÌ¹Í••‘}¡…ÁÑ•ÉÌ°…ÉÌ¹É•ÑÉä°(€€€€€€€€€€€™É•Í õ…ÉÌ¹™É•Í °ÉÕ¹}Å„õ¹½Ğ…ÉÌ¹¹½}Å„°(€€€€€€€€¤¤(€€€•±Í”è(€€€€€€€ÁÉ¥¹Ğ¡˜‰ÉÉ½Èèí…ÉÌ¹Ñ…É•Ñô¹½Ğ™½Õ¹ˆ°™¥±”õÍåÌ¹ÍÑ‘•ÉÈ¤(€€€€€€€É•ÑÕÉ¸€Ä(()¥˜}}¹…µ•}|€ôô€‰}}µ…¥¹}|ˆè(€€€ÍåÌ¹•á¥Ğ¡µ…¥¸ ¤¤