# -*- coding: utf-8 -*-
"""文本过滤模块：朗读时跳过指定符号、删除 Markdown 标记、切分长文本。

所有函数纯文本处理，无副作用，可被任意模块导入。
"""
import json
import os

# 朗读时忽略的成对符号（含包裹内容不读）。key=左符号, value=右符号。
# 默认：方括号、花括号、中文括号、英文括号、书名号、尖括号。
DEFAULT_IGNORE_PAIRS = {
    "[": "]", "{": "}", "【": "】", "（": "）", "(": ")", "《": "》", "<": ">",
}

# 朗读时跳过的孤立符号（Markdown 标记等）。只删符号本身，保留其中文字。
DEFAULT_STRIP_SYMBOLS = set("*~`#>|_-")

# 面板设置存 settings_app.json（Rust 写入），sidecar 每次合成时实时读取开关与自定义符号对
APP_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings_app.json")

# edge-tts 单次合成上限（字）。超长文本切成多块，每块独立合成、依次播放
MAX_BLOCK = 500


def _ignore_config():
    """读取 settings_app.json 的 ignore_pairs 开关与 ignore_symbols 自定义符号对。

    返回 (enabled, pairs)。enabled 默认 True；pairs 为 {左符号: 右符号}，
    用户未配置时用内置默认。
    """
    try:
        with open(APP_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        enabled = bool(data.get("ignore_pairs", True))
        syms = data.get("ignore_symbols")
        if isinstance(syms, list) and syms:
            pairs = {}
            for s in syms:
                if isinstance(s, str) and len(s) >= 2:
                    pairs[s[0]] = s[-1]
            if pairs:
                return enabled, pairs
        return enabled, dict(DEFAULT_IGNORE_PAIRS)
    except Exception:
        return True, dict(DEFAULT_IGNORE_PAIRS)


def strip_ignored(text):
    """删除成对符号包裹的内容（含符号本身），用于跳过注释/编者注等不朗读的片段。

    只处理左右符号正确配对且不嵌套的片段；未闭合的符号保留原文。
    """
    enabled, pairs = _ignore_config()
    if not enabled or not text or not pairs:
        return text
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in pairs:
            end = pairs[ch]
            j = text.find(end, i + 1)
            if j != -1:
                i = j + 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_symbols_config():
    """读取 settings_app.json 的 strip_symbols 开关与自定义符号集合。"""
    try:
        with open(APP_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        enabled = bool(data.get("strip_symbols", True))
        custom = data.get("strip_symbol_chars")
        if isinstance(custom, str) and custom:
            return enabled, set(custom)
        return enabled, set(DEFAULT_STRIP_SYMBOLS)
    except Exception:
        return True, set(DEFAULT_STRIP_SYMBOLS)


def strip_symbols(text):
    """删除 Markdown 标记符号（不删文字），用于让 TTS 不念出星号/井号等噪音。"""
    enabled, syms = _strip_symbols_config()
    if not enabled or not text or not syms:
        return text
    return "".join(ch for ch in text if ch not in syms)


def split_blocks(text, max_len=MAX_BLOCK):
    """把长文本切成 max_len 字以内的若干块（保留段落边界，避免切半句话）。"""
    if len(text) <= max_len:
        return [text]
    blocks = []
    cur = ""
    # 按换行优先切分，保持段落完整；否则按最大长度硬切
    for para in text.split("\n"):
        if not para:
            continue
        if cur and len(cur) + 1 + len(para) <= max_len:
            cur += "\n" + para
            continue
        if cur:
            blocks.append(cur)
            cur = ""
        # 单个段落仍超长时，按字符硬切
        while len(para) > max_len:
            blocks.append(para[:max_len])
            para = para[max_len:]
        cur = para
    if cur:
        blocks.append(cur)
    return [b for b in blocks if b.strip()]