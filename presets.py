"""
Phase 2 情境 preset 系統。

路由規則（依序）：
  1. 關鍵字前綴觸發（voice shortcut）——若原文以已註冊關鍵字開頭，
     切對應 preset，並把關鍵字從 text 剝除
  2. 前景 app 對應——若 app 名稱匹配某 preset 的 triggers_app，切之
  3. Default preset（走 Phase 1 原 prompt）

設計原則（規劃書 §2.1）：
  • app 名稱比對：lowercase + strip ".app"，同時支援英／中本地化名
  • 關鍵字觸發需嚴格：開頭錨點 + 後接分隔符（空白 / 標點），避免誤判
  • 任何例外都降級回 default，絕不讓 polish pipeline 崩
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import prompts

from logger import get_logger, log_error

log = get_logger("presets")


# ── Preset 契約 ───────────────────────────────────────────────────────────────

@dataclass
class Preset:
    name:             str           # preset 識別名（也用於 log）
    display_name:     str           # 使用者可讀名稱
    triggers_app:     set[str]      # 前景 app 比對（小寫、無 .app）
    triggers_keyword: list[str]     # 開頭關鍵字（大小寫不敏感）
    prompt_attr:      str           # 動態從 prompts 模組讀取的屬性名（reload safe）

    def resolve_prompt(self) -> str:
        """每次呼叫時動態查 prompts 模組，支援熱重載。"""
        return getattr(prompts, self.prompt_attr, prompts.OLLAMA_POLISH_PROMPT)


# ── Preset 清單 ───────────────────────────────────────────────────────────────

PRESETS: dict[str, Preset] = {
    "default": Preset(
        name="default",
        display_name="通用",
        triggers_app=set(),
        triggers_keyword=[],
        prompt_attr="OLLAMA_POLISH_PROMPT",
    ),
    "email": Preset(
        name="email",
        display_name="Email",
        triggers_app={
            "mail", "outlook", "thunderbird", "superhuman",
            "airmail", "spark", "郵件",
        },
        triggers_keyword=["email to", "寄信給", "寫封信給", "發 email"],
        prompt_attr="OLLAMA_EMAIL_PROMPT",
    ),
    "chat": Preset(
        name="chat",
        display_name="即時通訊",
        triggers_app={
            "slack", "discord", "messages", "訊息", "telegram", "line",
            "whatsapp", "wechat", "messenger",
        },
        triggers_keyword=["傳訊息給", "dm ", "chat to"],
        prompt_attr="OLLAMA_CHAT_PROMPT",
    ),
    "note": Preset(
        name="note",
        display_name="筆記",
        triggers_app={
            "notion", "obsidian", "bear", "notes", "備忘錄",
            "craft", "logseq", "apple notes",
        },
        triggers_keyword=["筆記", "note mode"],
        prompt_attr="OLLAMA_NOTE_PROMPT",
    ),
    "code_comment": Preset(
        name="code_comment",
        display_name="程式註解",
        triggers_app={
            "xcode", "visual studio code", "code",  # vscode 的進程名有時是 code
            "cursor", "zed", "sublime text", "pycharm", "intellij idea",
            "webstorm", "goland", "rubymine", "android studio",
            "jetbrains toolbox", "vscodium",
        },
        triggers_keyword=["// ", "# comment", "code comment"],
        prompt_attr="OLLAMA_CODE_COMMENT_PROMPT",
    ),
    # ── Phase 3.1 Voice Shortcuts（action preset）────────────────────────────
    # 只靠關鍵字觸發（triggers_app 留空），避免「在 Mail 裡說『翻譯英文…』」
    # 這類句子被 target preset 搶走。
    "translate_en": Preset(
        name="translate_en",
        display_name="翻英文",
        triggers_app=set(),
        triggers_keyword=[
            "翻譯英文", "翻成英文", "翻英文",
            "translate to english", "translate english", "in english",
        ],
        prompt_attr="OLLAMA_TRANSLATE_EN_PROMPT",
    ),
    "list": Preset(
        name="list",
        display_name="條列",
        triggers_app=set(),
        triggers_keyword=[
            "條列", "列點", "列出",
            "list mode", "bullet points", "bullet list",
        ],
        prompt_attr="OLLAMA_LIST_PROMPT",
    ),
    "meeting_notes": Preset(
        name="meeting_notes",
        display_name="會議紀錄",
        triggers_app=set(),
        triggers_keyword=[
            "會議紀錄", "會議記錄", "會議摘要",
            "meeting notes", "meeting minutes",
        ],
        prompt_attr="OLLAMA_MEETING_NOTES_PROMPT",
    ),
}


# ── App 名稱正規化 ────────────────────────────────────────────────────────────

def _normalize_app(name: Optional[str]) -> str:
    if not name:
        return ""
    s = name.strip().lower()
    if s.endswith(".app"):
        s = s[:-4]
    return s


# ── 關鍵字比對（嚴格）─────────────────────────────────────────────────────────

# 邊界：關鍵字後接空格、標點、冒號、逗號，或到字串結尾
_KW_BOUNDARY = re.compile(r"[\s,，:：。、!！?？]")


def _match_keyword(text: str, keywords: list[str]) -> Optional[tuple[str, str]]:
    """若 text 以任一關鍵字開頭（後接邊界）→ 回 (matched_kw, stripped_text)。

    不區分大小寫。stripped_text 已去掉關鍵字與其後的一個分隔符。
    """
    stripped = text.lstrip()
    lower    = stripped.lower()
    for kw in keywords:
        kw_l = kw.lower()
        if not lower.startswith(kw_l):
            continue
        # 邊界檢查：關鍵字後必須是分隔符或結尾
        tail_idx = len(kw_l)
        if tail_idx >= len(lower):
            return kw, ""
        if _KW_BOUNDARY.match(lower[tail_idx]):
            # 剝掉關鍵字 + 一個分隔符
            remainder = stripped[tail_idx + 1:].lstrip()
            return kw, remainder
    return None


# ── 主要 API ──────────────────────────────────────────────────────────────────

@dataclass
class PresetSelection:
    preset:         Preset
    text:           str             # 送給 LLM 的（可能剝去關鍵字）
    matched_reason: str             # "keyword:xxx" / "app:xxx" / "default"


def select_preset(
    text:          str,
    app_name:      Optional[str]      = None,
    enabled:       Optional[dict[str, bool]] = None,
) -> PresetSelection:
    """選 preset 的主入口。

    enabled: preset_name → bool 對照。None 或該 key 不存在時視為啟用。
    使用者可在設定中停用特定 preset（只會切 default）。
    任何意外一律降級回 default preset，絕不拋例外。
    """
    enabled = enabled or {}

    def _is_enabled(p: Preset) -> bool:
        return enabled.get(p.name, True)

    try:
        # 1) 關鍵字前綴觸發（voice shortcut）
        for p in PRESETS.values():
            if p.name == "default" or not _is_enabled(p):
                continue
            hit = _match_keyword(text, p.triggers_keyword)
            if hit is not None:
                _, stripped = hit
                return PresetSelection(
                    preset=p,
                    text=stripped if stripped else text,
                    matched_reason=f"keyword:{hit[0]}",
                )

        # 2) 前景 app 比對
        app = _normalize_app(app_name)
        if app:
            for p in PRESETS.values():
                if p.name == "default" or not _is_enabled(p):
                    continue
                if app in p.triggers_app:
                    return PresetSelection(
                        preset=p,
                        text=text,
                        matched_reason=f"app:{app}",
                    )

        # 3) 降級
        return PresetSelection(
            preset=PRESETS["default"],
            text=text,
            matched_reason="default",
        )
    except Exception:
        log_error("select_preset_exception", text_len=len(text), app=app_name)
        return PresetSelection(
            preset=PRESETS["default"],
            text=text,
            matched_reason="default_on_error",
        )
