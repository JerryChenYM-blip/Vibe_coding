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
    "以下是 Whisper 語音轉文字的結果（中英混講）。請只做三件事：\n"
    "1. 修正明顯的同音錯字（例：在/再、的/得/地、措置率→錯字率）\n"
    "2. 補上適當的標點符號與斷句\n"
    "3. 刪除無意義贅詞（嗯、啊、那個、um、uh）\n\n"
    "**Whisper 常見誤辨識修正（重要、必做）**：\n"
    "Whisper 經常把 Claude（/klɔːd/）誤聽成 Cloud（/klaʊd/）。請依以下規則**主動逆向校正**：\n\n"
    "原文出現「Cloud」時，**必須改成「Claude」**的情況：\n"
    "  - 「Cloud Code」→ Claude Code（Anthropic 命令列工具）\n"
    "  - 「Cloud AI」→ Claude AI\n"
    "  - 「Cloud Sonnet」/「Cloud Opus」→ Claude Sonnet/Opus（Anthropic 模型）\n"
    "  - 「跟 Cloud 講話/對話」→ 跟 Claude 講話/對話\n"
    "  - 「問 Cloud」/「Cloud 回答」/「Cloud 幫我」→ Claude\n"
    "  - 「Cloud 寫 code/程式」/「Cloud 寫文章」→ Claude\n"
    "  - 「Cloud 模型」（在 AI 語境）→ Claude 模型\n\n"
    "**只在以下語境保留「Cloud」**：\n"
    "  - Google Cloud、AWS、Azure、Cloud Run、Cloud Storage（明確雲端服務）\n"
    "  - 部署到 Cloud、Cloud 上、雲端搭配的 Cloud\n\n"
    "範例：\n"
    "  原：「我用 Cloud Code 寫程式」→ 改：「我用 Claude Code 寫程式」\n"
    "  原：「我跟 Cloud 對話」→ 改：「我跟 Claude 對話」\n"
    "  原：「我用 Cloud AI 寫 code,然後部署到 Google Cloud」→ 改：「我用 Claude AI 寫 code,然後部署到 Google Cloud」\n"
    "  原：「Code 放到 Google Cloud 上」→ 保留：「Code 放到 Google Cloud 上」\n\n"
    "**絕對禁止**：\n"
    "- 不要翻譯成英文或其他語言\n"
    "- 不要改寫、不要重新表達\n"
    "- 不要增加任何說明、前綴或解釋\n"
    "- 輸出語言必須**完全等於原文語言**\n"
    "- 中文輸出**一律使用繁體字**（台灣用語）；嚴禁輸出簡體字（例如：'设备/設備'要用後者、'语言/語言'要用後者、'体验/體驗'要用後者）\n\n"
    "原文：\n{text}\n\n"
    "修正後（直接輸出、繁體中文、不加任何說明）："
)

# v2.13.0：Ollama /api/generate 的 system 欄位，給 instruct 模型更明確 role。
# qwen2.5:3b-instruct 等小型 instruct 模型對純 prompt 易過度發揮（擴寫、翻譯），
# 用 system role 鎖定行為。對不支援 system 的模型仍會 fallback 走 prompt 描述。
OLLAMA_POLISH_SYSTEM = (
    "你是繁體中文校對員（台灣用語）。只做三件事：修同音錯字、補標點、刪贅詞。"
    "絕對不翻譯、不改寫、不增刪內容。"
    "輸出語言必須等於原文語言；中文輸出**一律繁體中文**，嚴禁簡體字。"
    "\n/no_think"  # v2.13.1：關 Qwen 3+ thinking mode（避免長思考拖到 20+s 才回答）
)

# 舊別名（向後相容）
OLLAMA_SYSTEM_PROMPT = OLLAMA_POLISH_PROMPT


# ── Phase 2 情境 preset prompts ───────────────────────────────────────────────

# 每個 preset 的 prompt 都繼承 Phase 1 的「去 filler / 修錯字 / 保留英文」底盤，
# 再套上情境特有的語氣與格式要求。

OLLAMA_EMAIL_PROMPT = (
    "你是語音轉文字後處理助理。輸入是一段 Whisper 中英混講轉錄，目標是輸出一封"
    "**正式但自然**的 email 內容。做下列事情：\n"
    "1. 刪除語氣詞與無意義重複\n"
    "2. 修正同音錯字\n"
    "3. 使用完整句、適當分段；若有明確收件人語氣就加招呼語（例如 Hi X,）\n"
    "4. 保留所有英文原文，不要翻譯技術術語、品牌名、人名\n"
    "5. **不要憑空加上簽名檔、祝福語或寄件人資訊**（除非原文有提到）\n"
    "6. 維持原意，不要改寫或擴充內容\n\n"
    "只輸出 email 本文，不要加任何說明、標題、前綴、引號或括號。\n\n"
    "原文：\n{text}"
)

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

OLLAMA_NOTE_PROMPT = (
    "你是語音轉文字後處理助理。輸入是一段 Whisper 中英混講轉錄，目標是輸出一段"
    "**結構清晰的筆記內容**（Notion / Obsidian / Bear 使用情境）。做：\n"
    "1. 刪除語氣詞與無意義重複\n"
    "2. 修正同音錯字\n"
    "3. 若內容自然呈現多個要點，可用「-」條列；若為連貫敘述，維持段落\n"
    "4. 標點完整、斷句清楚\n"
    "5. 保留所有英文原文\n"
    "6. 維持原意\n\n"
    "只輸出筆記內容，不要加任何說明、標題、前綴、引號或括號。\n\n"
    "原文：\n{text}"
)

OLLAMA_CODE_COMMENT_PROMPT = (
    "你是語音轉文字後處理助理。輸入是一段 Whisper 中英混講轉錄，目標是輸出一段"
    "**技術註解**（給 Xcode / VS Code / Cursor 使用）。做：\n"
    "1. 刪除語氣詞與無意義重複\n"
    "2. 修正技術術語錯字\n"
    "3. 技術名詞與 API 名稱一律用英文原文\n"
    "4. 簡潔、一到兩句話為主，不要冗長解釋\n"
    "5. 維持原意，不要憑空加入使用範例或 TODO\n\n"
    "只輸出註解內容本身，不要加 // 或 # 前綴、不要加引號或括號。\n\n"
    "原文：\n{text}"
)

# ── Phase 3.1 Voice Shortcuts（action preset prompts）──────────────────────
#
# 這組 prompt 由 presets.py 的 action preset 觸發：使用者說話開頭含對應
# 關鍵字（「翻譯英文」「條列」「會議紀錄」）時路由至此，關鍵字會被 presets.py
# 剝除，只把「動作目標文字」傳進 {text}。

OLLAMA_TRANSLATE_EN_PROMPT = (
    "你是中英翻譯助理。輸入是一段中文轉錄（可能夾雜英文專有名詞），"
    "目標是輸出一段**自然流暢的英文**。做下列事情：\n"
    "1. 刪除語氣詞與無意義重複（嗯、啊、那個、um、uh、like）\n"
    "2. 翻譯成符合母語者語感的英文，不要逐字直譯\n"
    "3. 英文專有名詞、人名、品牌名、技術術語維持原拼寫\n"
    "4. 若原文極短（≤ 3 字），仍保持完整英文句結構\n"
    "5. 維持原意，不要擴充或省略資訊\n\n"
    "範例：\n"
    "輸入：嗯我明天下午三點要跟 Jerry 開會討論那個 Q2 roadmap\n"
    "輸出：I have a meeting with Jerry tomorrow at 3 PM to discuss the Q2 roadmap.\n\n"
    "只輸出翻譯後的英文，不要加任何說明、標題、前綴、引號或括號。\n\n"
    "原文：\n{text}"
)

OLLAMA_LIST_PROMPT = (
    "你是語音轉文字後處理助理。輸入是一段 Whisper 中英混講轉錄，"
    "目標是把使用者口述的項目**整理為條列清單**。做：\n"
    "1. 刪除語氣詞與無意義重複\n"
    "2. 每個項目一行，開頭用「- 」（減號空格）\n"
    "3. 順序保留使用者說的順序；若使用者明確說「第一」「第二」可去掉這些編號詞\n"
    "4. 項目內保留英文原文、不翻譯\n"
    "5. 不要憑空新增項目，也不要合併相近項目\n\n"
    "範例：\n"
    "輸入：那我要買牛奶然後買麵包還有要買雞蛋\n"
    "輸出：\n- 牛奶\n- 麵包\n- 雞蛋\n\n"
    "只輸出條列內容，不要加標題、說明、前綴、引號或括號。\n\n"
    "原文：\n{text}"
)

OLLAMA_MEETING_NOTES_PROMPT = (
    "你是會議紀錄整理助理。輸入是一段會議中的口述轉錄，"
    "目標是輸出**結構化的會議紀錄**。做：\n"
    "1. 刪除語氣詞與無意義重複\n"
    "2. 按內容性質分段，每段可用「## 重點 」「## 行動項 」「## 決議 」等標題\n"
    "3. 行動項（action items）用條列，格式「- [負責人] 要做的事」\n"
    "4. 決議（decisions）用條列\n"
    "5. 保留所有英文原文（人名、專案名、技術術語）\n"
    "6. 沒有對應內容的段落（例如沒有行動項）就不要留空標題\n"
    "7. 維持原意，不要憑空加結論\n\n"
    "範例：\n"
    "輸入：今天討論 Q2 roadmap 大家覺得 API 那塊要 Jerry 負責 UI 的話 Alice 來做 時程訂在六月底\n"
    "輸出：\n## 重點\n- 討論 Q2 roadmap\n- 時程訂在 6 月底\n\n## 行動項\n- [Jerry] 負責 API\n- [Alice] 負責 UI\n\n"
    "只輸出會議紀錄內容，不要加說明、前綴、引號或括號。\n\n"
    "原文：\n{text}"
)

# Phase 2 核心資料：preset 名 → prompt 字串
OLLAMA_PRESET_PROMPTS: dict[str, str] = {
    "default":       OLLAMA_POLISH_PROMPT,
    "email":         OLLAMA_EMAIL_PROMPT,
    "chat":          OLLAMA_CHAT_PROMPT,
    "note":          OLLAMA_NOTE_PROMPT,
    "code_comment":  OLLAMA_CODE_COMMENT_PROMPT,
    # Phase 3.1 action presets
    "translate_en":  OLLAMA_TRANSLATE_EN_PROMPT,
    "list":          OLLAMA_LIST_PROMPT,
    "meeting_notes": OLLAMA_MEETING_NOTES_PROMPT,
}

# 舊別名保留：OLLAMA_MEETING_MINUTES_PROMPT 是 v2.1.0 預留的佔位符，
# Phase 3.1 正式用 OLLAMA_MEETING_NOTES_PROMPT 取代（內容更完整）。
OLLAMA_MEETING_MINUTES_PROMPT = OLLAMA_MEETING_NOTES_PROMPT


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
