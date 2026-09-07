"""
所有 System Prompt 與 AI 指令的集中管理處。

此檔所有 prompt **皆為模組層 string 常數**，以利 prompt_reloader 偵測
mtime 變化後用 importlib.reload() 即時生效。呼叫端必須用「module.ATTR」
動態查詢形式，不可再用 `from prompts import OLLAMA_POLISH_PROMPT`，否則
reload 後仍會拿到舊值。

匯出：
  WHISPER_INITIAL_PROMPT     Whisper 轉錄時注入的語系／風格提示
  OLLAMA_POLISH_PROMPT       Phase 1 通用潤飾 prompt（default preset 用）
  OLLAMA_PRESET_PROMPTS      Phase 2 preset name → prompt 對照表
  format_whisper_prompt()    拼接 WHISPER_INITIAL_PROMPT + dictionary 術語
  format_polish_prompt()     拼接 preset prompt + dictionary 約束
"""

from __future__ import annotations

from typing import Iterable, Optional

# ── Whisper Transcription Prompts ─────────────────────────────────────────────

# 幫助 Whisper 維持繁體中文並保留英文專有名詞。
WHISPER_INITIAL_PROMPT = (
    "這是一段繁體中文與英文夾雜的對話。請使用正體中文（繁體中文），"
    "原文保留英文單字與專有名詞，不要翻譯，保持口語自然。"
)


# ── Ollama AI Refinement Prompts ──────────────────────────────────────────────

# Phase 1 通用潤飾 prompt（default preset 也用這個）。
# 嚴格版：正面清單 + 負面清單 + 固定結尾「只輸出修正後的文字」收斂 LLM 行為。
OLLAMA_POLISH_PROMPT = (
    "你是繁體中文校對員。**最小修改原則：能不改就不改、不重組句子、不替換同義詞**。\n\n"
    "**只做以下三件事**（其他一律保留原樣）：\n"
    "1. 修同音錯字（例：在/再、的/得/地、措置率→錯字率）\n"
    "2. 補缺漏的標點（逗號、句號、問號；**原有的標點不動**）\n"
    "3. 刪純語助詞（嗯、啊、那個、um、uh；**只刪純語助詞、不刪實詞**）\n\n"
    "**絕對禁止（最重要）**：\n"
    "- ❌ 不要改寫、不要重新表達、不要換句話說（即使覺得原文不通順）\n"
    "- ❌ 不要替換同義詞（例：「不知道」絕對不能改成「搞不清楚」、「賣得好」不能改成「賣得不錯」）\n"
    "- ❌ 不要改變語氣（「想要做」不能改成「想過要做」、「需求」不能加「了」）\n"
    "- ❌ 不要重組句子結構、不要合併或拆解句子\n"
    "- ❌ 不要翻譯英文詞（roadmap 不能變「路線圖」、code 不能變「程式碼」）\n"
    "- ❌ 不要翻譯、不要輸出簡體字（一律繁體中文台灣用語）\n"
    "- ❌ 不要加任何說明或前綴\n\n"
    "**示範**（注意保留原文每個字、每個結構）：\n"
    "  原：「我還不知道這件事情。」\n"
    "  ✓ 對：「我還不知道這件事情。」（無錯字、無贅詞 → 完全不動）\n"
    "  ✗ 錯：「我還在搞不清楚這件事呢。」（替換同義詞、改語氣）\n\n"
    "  原：「嗯我今天要去開會然後討論 Q2 roadmap」\n"
    "  ✓ 對：「我今天要去開會，然後討論 Q2 roadmap。」（刪嗯、補逗號和句號）\n"
    "  ✗ 錯：「我今天要參加會議，討論 Q2 路線圖。」（改寫、翻譯了 roadmap）\n\n"
    "原文：\n{text}\n\n"
    "修正後（最小修改、保留原句結構與用詞、繁體中文）："
)

# v2.13.0：Ollama /api/generate 的 system 欄位，給 instruct 模型更明確 role。
# qwen2.5:3b-instruct 等小型 instruct 模型對純 prompt 易過度發揮（擴寫、翻譯），
# 用 system role 鎖定行為。對不支援 system 的模型仍會 fallback 走 prompt 描述。
OLLAMA_POLISH_SYSTEM = (
    "你是繁體中文校對員（台灣用語）。**最小修改原則**：只做三件事——"
    "修同音錯字、補標點、刪贅詞（嗯啊那個）。"
    "絕對禁止改寫、替換同義詞、改變語氣、重組句子。"
    "「不知道」絕不能改「搞不清楚」、「想要做」絕不能改「想過要做」。"
    "若原文已通順、就一字不動輸出原文。"
    "輸出一律繁體中文（台灣用語）、嚴禁簡體字。"
    "\n/no_think"
)

# 舊別名（向後相容）
OLLAMA_SYSTEM_PROMPT = OLLAMA_POLISH_PROMPT


# ── v2.19.x Hybrid polish 用、極短指令、Flash-Lite 跑（cost-sensitive）────
# Hybrid backend 跑 rule → pinyin → optional Gemini Flash-Lite 三層。
# 第三層走 Flash-Lite（最便宜），prompt 故意極短、只修明顯錯字、不重寫。
HYBRID_POLISH_PROMPT = """\
只修明顯錯字、不改寫、不加標點、不換詞、不解釋。直接輸出修正後文字：
{text}
"""


# ── Phase 2 情境 preset prompts ───────────────────────────────────────────────

# 每個 preset 的 prompt 都繼承 Phase 1 的「去 filler / 修錯字 / 保留英文」底盤，
# 再套上情境特有的語氣與格式要求。


OLLAMA_CHAT_PROMPT = (
    "你是語音轉文字後處理助理。輸入是一段 Whisper 中英混講轉錄，目標是輸出一則"
    "**輕鬆口語、符合即時通訊（Slack / Line / Messages）風格**的訊息。做：\n"
    "1. 刪除語氣詞與無意義重複\n"
    "2. 修正同音錯字\n"
    "3. 保持口語化，短句優先、標點少量；英文句子可維持小寫（除非專有名詞）\n"
    "4. 保留所有英文原文\n"
    "5. 不要加招呼語或簽名\n"
    "6. 維持原意\n\n"
    "只輸出訊息本文，不要加任何說明、標題、前綴、引號或括號。\n\n"
    "原文：\n{text}"
)



# ── Phase 3.1 Voice Shortcuts（action preset prompts）──────────────────────
#
# 這組 prompt 由 presets.py 的 action preset 觸發：使用者說話開頭含對應
# 關鍵字（「翻譯英文」「條列」「會議紀錄」）時路由至此，關鍵字會被 presets.py
# 剝除，只把「動作目標文字」傳進 {text}。




# Phase 2 核心資料：preset 名 → prompt 字串
OLLAMA_PRESET_PROMPTS: dict[str, str] = {
    "default":       OLLAMA_POLISH_PROMPT,
    "chat":          OLLAMA_CHAT_PROMPT,
}

# ── Dictionary 注入 helpers ───────────────────────────────────────────────────

def format_whisper_prompt(dictionary_terms: Optional[Iterable[str]] = None) -> str:
    """拼出傳給 Whisper 的 initial_prompt：基礎提示 + 術語清單。

    dictionary_terms 是使用者個人字典的詞彙；最多取 30 個（Whisper prompt
    長度有限），用逗號拼接附在後面。空或 None 時回基礎提示。
    """
    base = WHISPER_INITIAL_PROMPT
    if not dictionary_terms:
        return base
    terms = [t.strip() for t in dictionary_terms if t and t.strip()]
    if not terms:
        return base
    snippet = "、".join(terms[:30])
    return f"{base} 常用詞彙：{snippet}。"


def format_qwen3_context(dictionary_terms: Optional[Iterable[str]] = None) -> str:
    """拼出傳給 Qwen3-ASR 的 `context` 字串（system prompt biasing 用）。

    v2.14.0 新增。Qwen3-ASR 用 `context` 取代 Whisper 的 `initial_prompt`：
      • Qwen3-ASR: `<|im_start|>system\n{context}<|im_end|>` 真正的 LLM
        system message，整個 generation 過程都看得到、biasing 強度比 Whisper
        的 decoder prefix 高
      • 上限沒明確規定，這裡仍取前 50 個（跟 polish 上限同步）

    v2.19.0：**格式改造**——加 metadata frame「專有名詞：…。」防字典 dump 幻覺。
      原本是空白分隔的 bare word list（官方範例「交易 停滞」），但實機 log 發現
      靜音 / 雜音段時 Qwen3-ASR 會把這串 bare list 當成「**剛剛轉錄完的句子**」
      續寫——直接把字典詞當答案吐出來（00:42:02 真實 case：
      「Qwen3-ASR Qwen3 Qwen Large V3 Turbo Whisper Pro Ollama Claude Cloud
      ChatGPT Cursor GitHub」）。
      改成有「專有名詞：」前綴 + 頓號分隔 + 句號收尾的 framed list 後，模型
      理解為「**參考清單**而非待續寫文本」，dump 機率大幅下降。
      TypeWhisper 184-run 實測（同類技術）：WER 33.8% → 18.2%。

    dictionary_terms 空時回空字串（Qwen3-ASR transcribe(context="") 是合法的）。
    """
    if not dictionary_terms:
        return ""
    terms = [t.strip() for t in dictionary_terms if t and t.strip()]
    if not terms:
        return ""
    # v2.19.0：framed list — 加 metadata 前綴 + 頓號分隔 + 句號收尾
    return "專有名詞：" + "、".join(terms[:50]) + "。"


def format_polish_prompt(
    base_prompt: str,
    dictionary_terms: Optional[Iterable[str]] = None,
) -> str:
    """在 preset prompt 尾端（{text} 之前）追加一行保留術語約束。

    dictionary_terms 空時回原 base_prompt 不動。
    """
    if not dictionary_terms:
        return base_prompt
    terms = [t.strip() for t in dictionary_terms if t and t.strip()]
    if not terms:
        return base_prompt
    snippet = "、".join(terms[:50])
    # 將一行約束插在最後一行「只輸出…」之前（通常是原文之上）。策略：
    # 若 base 含 "原文：\n{text}"，就把約束插在它前面。
    addon = f"★ 務必逐字保留下列術語原文，不要替換為同音字或翻譯：{snippet}\n\n"
    marker = "原文：\n{text}"
    if marker in base_prompt:
        return base_prompt.replace(marker, addon + marker)
    # 退而求其次：直接拼在尾端
    return base_prompt + "\n" + addon
