"""
Centralized storage for all System Prompts and AI instructions.
Easy to maintain and version control.
"""

# ── Whisper Transcription Prompts ─────────────────────────────────────────────

# This helps Whisper maintain Traditional Chinese and handle technical terms.
WHISPER_INITIAL_PROMPT = (
    "這是一段繁體中文與英文夾雜的對話。請使用正體中文（繁體中文），"
    "原文保留英文單字與專有名詞，不要翻譯，保持口語自然。"
)

# ── Ollama AI Refinement Prompts ──────────────────────────────────────────────

# Standard refinement prompt for cleaning up transcriptions.
OLLAMA_SYSTEM_PROMPT = (
    "你是一位專業的文字編輯助理。以下是一段語音辨識轉錄文字，"
    "請修正錯字、斷句、語氣，使其更通順自然，只回傳修正後的文字，不要加任何說明或旁白：\n\n{text}"
)

# Optional: Add different personalities here
OLLAMA_MEETING_MINUTES_PROMPT = (
    "你是一位會議記錄專家。請將以下轉錄文字整理成條列式的會議要點，"
    "保留關鍵決策與行動項，使用繁體中文：\n\n{text}"
)
