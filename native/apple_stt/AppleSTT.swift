// whisperpro-apple-stt — macOS 26 SpeechAnalyzer 的命令列包裝
//
// 【為什麼需要這支原生 helper】
// macOS 26 的新語音辨識引擎（SpeechAnalyzer / SpeechTranscriber / DictationTranscriber）
// 是 Swift-only，Apple 沒有把它們橋接給 Objective-C runtime，所以 PyObjC 完全看不到
// ——實測 objc.lookUpClass("SpeechAnalyzer") 直接 nosuchclass_error（2026-09-17）。
// 對照 SDK 的 Speech.swiftinterface：這些型別宣告成 final class / actor，742 行裡
// 只有 19 處 @objc，而且幾乎都只是 deinit，不是真的 API 表面。
//
// 【為什麼不用舊的 SFSpeechRecognizer（那個 PyObjC 叫得到）】
// 舊 API 雖然能從 Python 直接呼叫（已實測 zh-TW 可離線），但：
//   1. Apple 官方文件明載單次辨識約有 1 分鐘上限，長錄音會被截斷
//   2. 它不是系統「聽寫」在用的那顆引擎，速度與品質是另一條產線
// 所以選擇多寫這支 helper，換到與系統聽寫同源的新引擎。
//
// 【輸出契約】
// stdout 永遠只有一行 JSON；所有診斷訊息一律走 stderr。
// Python 端可以直接 json.loads(stdout)，不需要過濾雜訊。
// 結束碼：0 = 成功、2 = 可預期的失敗（JSON 內有 error 代碼）、其他 = 非預期崩潰。

import AVFoundation
import Foundation
import Speech

// MARK: - 輸出資料結構

/// 轉錄／錯誤共用的輸出。欄位一律 Optional：JSON 裡不出現的欄位代表「這次不適用」，
/// 而不是「值為 0」——Python 端才能分辨「沒測到」與「測到 0」。
struct Payload: Encodable {
    var ok: Bool
    var engine: String?
    var locale: String?
    var text: String?
    var segments: Int?
    var audio_seconds: Double?
    var elapsed_ms: Double?
    var asset_status: String?
    var downloaded: Bool?
    var contextual_terms: Int?
    var error: String?
    var detail: String?
}

/// --probe 的輸出。用途是「還沒錄音之前就先知道這台機器能不能跑」，
/// 給 Python 端的 warmup 與設定畫面用。
struct EngineProbe: Encodable {
    var available: Bool
    var supported_locale: String?
    var asset_status: String
    var installed_locales: [String]
}

struct ProbePayload: Encodable {
    var ok: Bool
    var os_version: String
    var requested_locale: String
    var speech: EngineProbe
    var dictation: EngineProbe
}

// MARK: - 輸出工具

private func emit<T: Encodable>(_ value: T) {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.withoutEscapingSlashes]
    if let data = try? encoder.encode(value),
       let text = String(data: data, encoding: .utf8) {
        print(text)
    } else {
        // 連錯誤都編不出來的情況：手寫一行合法 JSON，確保 Python 端永遠 parse 得動
        print("{\"ok\":false,\"error\":\"encode_failed\"}")
    }
}

private func note(_ message: String) {
    FileHandle.standardError.write(("[apple-stt] " + message + "\n").data(using: .utf8)!)
}

private func bail(_ code: String, _ detail: String? = nil) -> Never {
    emit(Payload(ok: false, engine: nil, locale: nil, text: nil, segments: nil,
                 audio_seconds: nil, elapsed_ms: nil, asset_status: nil,
                 downloaded: nil, contextual_terms: nil, error: code, detail: detail))
    exit(2)
}

// MARK: - 參數解析

struct Args {
    var probe = false
    var audioPath: String?
    var localeID = "zh-TW"
    var termsFile: String?
    var engine = "speech"          // speech | dictation
    var allowDownload = false
    var maxTerms = 100             // 上限刻意保守：舊 API 文件建議 contextualStrings 不超過 100 個
}

func parseArgs() -> Args {
    var args = Args()
    var it = CommandLine.arguments.dropFirst().makeIterator()
    while let flag = it.next() {
        switch flag {
        case "--probe":          args.probe = true
        case "--allow-download": args.allowDownload = true
        case "--audio":          args.audioPath = it.next()
        case "--locale":         args.localeID = it.next() ?? args.localeID
        case "--terms-file":     args.termsFile = it.next()
        case "--engine":         args.engine = it.next() ?? args.engine
        case "--max-terms":      args.maxTerms = Int(it.next() ?? "") ?? args.maxTerms
        case "--help", "-h":
            note("usage: whisperpro-apple-stt --audio <file.wav> [--locale zh-TW] "
                 + "[--engine speech|dictation] [--terms-file terms.txt] [--allow-download]")
            note("       whisperpro-apple-stt --probe [--locale zh-TW]")
            exit(0)
        default:
            bail("bad_argument", flag)
        }
    }
    return args
}

// MARK: - 資產狀態

func statusName(_ status: AssetInventory.Status) -> String {
    switch status {
    case .unsupported:  return "unsupported"   // 這個語言根本不支援
    case .downloading:  return "downloading"   // 正在下載中
    case .supported:    return "supported"     // 支援、但模型還沒裝
    case .installed:    return "installed"     // 可以直接用
    @unknown default:   return "unknown"
    }
}

/// 確認模型已安裝；沒裝就看要不要下載。
/// 回傳 (狀態字串, 這次有沒有真的下載)。
func ensureAssets(for module: any SpeechModule,
                  allowDownload: Bool) async throws -> (String, Bool) {
    let status = await AssetInventory.status(forModules: [module])
    if status == .installed { return (statusName(status), false) }

    guard allowDownload else {
        // 刻意不在這裡自動下載：下載可能要好幾分鐘，而這支程式是被「按完熱鍵
        // 等著貼上」的流程呼叫的。讓它快速失敗，由 Python 端決定要不要在
        // 暖機階段帶 --allow-download 重跑一次。
        return (statusName(status), false)
    }

    note("模型尚未安裝（status=\(statusName(status))），開始下載…")
    if let request = try await AssetInventory.assetInstallationRequest(supporting: [module]) {
        try await request.downloadAndInstall()
        note("模型下載完成")
        return (statusName(await AssetInventory.status(forModules: [module])), true)
    }
    // 回傳 nil 代表系統認為不需要安裝任何東西
    return (statusName(await AssetInventory.status(forModules: [module])), false)
}

// MARK: - 讀取偏好詞彙

/// 一行一個詞。空行與 # 開頭的註解行會被跳過。
/// 走檔案而不是命令列參數的原因：詞彙可能有上百個、還可能含空白與標點，
/// 塞進 argv 容易被 shell 跟長度上限咬到。
func loadTerms(_ path: String?, limit: Int) -> [String] {
    guard let path, let raw = try? String(contentsOfFile: path, encoding: .utf8) else { return [] }
    var seen = Set<String>()
    var terms: [String] = []
    for line in raw.split(separator: "\n", omittingEmptySubsequences: true) {
        let term = line.trimmingCharacters(in: .whitespaces)
        if term.isEmpty || term.hasPrefix("#") { continue }
        if seen.insert(term).inserted { terms.append(term) }
        if terms.count >= limit { break }
    }
    return terms
}

// MARK: - 主流程

@main
struct AppleSTT {

    static func main() async {
        let args = parseArgs()
        let locale = Locale(identifier: args.localeID)

        if args.probe {
            await runProbe(locale: locale, requested: args.localeID)
            exit(0)
        }

        guard let audioPath = args.audioPath else { bail("missing_audio") }
        let url = URL(fileURLWithPath: audioPath)
        guard FileManager.default.fileExists(atPath: audioPath) else {
            bail("audio_not_found", audioPath)
        }

        let terms = loadTerms(args.termsFile, limit: args.maxTerms)

        do {
            let payload = try await transcribe(url: url,
                                               locale: locale,
                                               engine: args.engine,
                                               terms: terms,
                                               allowDownload: args.allowDownload)
            emit(payload)
            exit(payload.ok ? 0 : 2)
        } catch {
            bail("transcribe_failed", String(describing: error))
        }
    }

    // MARK: 探測

    static func runProbe(locale: Locale, requested: String) async {
        let osv = ProcessInfo.processInfo.operatingSystemVersionString

        // SpeechTranscriber：通用轉錄模組，自己一套模型資產
        var speechProbe = EngineProbe(available: SpeechTranscriber.isAvailable,
                                      supported_locale: nil,
                                      asset_status: "unknown",
                                      installed_locales: [])
        let speechLocale = await SpeechTranscriber.supportedLocale(equivalentTo: locale)
        speechProbe.supported_locale = speechLocale?.identifier
        speechProbe.installed_locales = await SpeechTranscriber.installedLocales.map(\.identifier)
        if let loc = speechLocale {
            let module = SpeechTranscriber(locale: loc, preset: .transcription)
            speechProbe.asset_status = statusName(await AssetInventory.status(forModules: [module]))
        } else {
            speechProbe.asset_status = "unsupported"
        }

        // DictationTranscriber：系統「聽寫」用的模組。值得一起探測的原因是——
        // 使用者的系統聽寫語言已經設成 zh_TW，這條路可能不必再下載任何模型。
        var dictationProbe = EngineProbe(available: true,
                                         supported_locale: nil,
                                         asset_status: "unknown",
                                         installed_locales: [])
        let dictationLocale = await DictationTranscriber.supportedLocale(equivalentTo: locale)
        dictationProbe.supported_locale = dictationLocale?.identifier
        dictationProbe.installed_locales = await DictationTranscriber.installedLocales.map(\.identifier)
        if let loc = dictationLocale {
            let module = DictationTranscriber(locale: loc, preset: .shortDictation)
            dictationProbe.asset_status = statusName(await AssetInventory.status(forModules: [module]))
        } else {
            dictationProbe.asset_status = "unsupported"
        }

        emit(ProbePayload(ok: true,
                          os_version: osv,
                          requested_locale: requested,
                          speech: speechProbe,
                          dictation: dictationProbe))
    }

    // MARK: 轉錄

    static func transcribe(url: URL,
                           locale: Locale,
                           engine: String,
                           terms: [String],
                           allowDownload: Bool) async throws -> Payload {

        let file = try AVAudioFile(forReading: url)
        let audioSeconds = Double(file.length) / file.fileFormat.sampleRate

        // 0 影格的檔案要在這裡擋掉：送進去的話 analyzeSequence 立刻回來，但
        // module.results 這個 AsyncSequence 永遠收不到結束訊號，收集用的 Task
        // 就卡在 await 上不回來——整支程式掛住，只能從外面殺掉。
        // （實測：空 wav 連跑三次都要靠 timeout 才結束。）
        guard file.length > 0 else {
            return Payload(ok: false, engine: engine, locale: locale.identifier, text: nil,
                           segments: nil, audio_seconds: 0, elapsed_ms: nil,
                           asset_status: nil, downloaded: nil, contextual_terms: terms.count,
                           error: "empty_audio", detail: "file has 0 frames")
        }

        // 偏好詞彙掛在 AnalysisContext 上，兩種引擎共用同一個機制。
        let context = AnalysisContext()
        if !terms.isEmpty { context.contextualStrings[.general] = terms }

        let started = Date()

        switch engine {
        case "dictation":
            guard let loc = await DictationTranscriber.supportedLocale(equivalentTo: locale) else {
                return Payload(ok: false, engine: engine, locale: locale.identifier, text: nil,
                               segments: nil, audio_seconds: audioSeconds, elapsed_ms: nil,
                               asset_status: "unsupported", downloaded: nil,
                               contextual_terms: terms.count,
                               error: "locale_unsupported", detail: locale.identifier)
            }
            // 30 秒是 Apple 對 short/long 兩個 preset 的分野依據（短句 vs 長篇口述）。
            // 這裡照音檔實際長度自動挑，避免短錄音被當長篇處理而多付啟動成本。
            let preset: DictationTranscriber.Preset =
                audioSeconds <= 30 ? .shortDictation : .longDictation
            let module = DictationTranscriber(locale: loc, preset: preset)
            let (status, downloaded) = try await ensureAssets(for: module, allowDownload: allowDownload)
            if status != "installed" {
                return Payload(ok: false, engine: engine, locale: loc.identifier, text: nil,
                               segments: nil, audio_seconds: audioSeconds, elapsed_ms: nil,
                               asset_status: status, downloaded: downloaded,
                               contextual_terms: terms.count,
                               error: "asset_not_installed", detail: "status=\(status)")
            }

            let analyzer = SpeechAnalyzer(modules: [module])
            try await analyzer.setContext(context)
            // 結果是 AsyncSequence，必須有人在消費，analyzeSequence 才不會被卡住，
            // 所以先開一個 Task 收，再送音訊進去。
            let collector = Task { () -> [String] in
                var parts: [String] = []
                for try await result in module.results {
                    parts.append(String(result.text.characters))
                }
                return parts
            }
            _ = try await analyzer.analyzeSequence(from: file)
            try await analyzer.finalizeAndFinishThroughEndOfInput()
            let parts = try await collector.value
            return finish(engine: engine, locale: loc.identifier, parts: parts,
                          audioSeconds: audioSeconds, started: started,
                          status: status, downloaded: downloaded, terms: terms.count)

        case "speech":
            guard SpeechTranscriber.isAvailable else {
                return Payload(ok: false, engine: engine, locale: locale.identifier, text: nil,
                               segments: nil, audio_seconds: audioSeconds, elapsed_ms: nil,
                               asset_status: nil, downloaded: nil, contextual_terms: terms.count,
                               error: "engine_unavailable", detail: "SpeechTranscriber.isAvailable = false")
            }
            guard let loc = await SpeechTranscriber.supportedLocale(equivalentTo: locale) else {
                return Payload(ok: false, engine: engine, locale: locale.identifier, text: nil,
                               segments: nil, audio_seconds: audioSeconds, elapsed_ms: nil,
                               asset_status: "unsupported", downloaded: nil,
                               contextual_terms: terms.count,
                               error: "locale_unsupported", detail: locale.identifier)
            }
            let module = SpeechTranscriber(locale: loc, preset: .transcription)
            let (status, downloaded) = try await ensureAssets(for: module, allowDownload: allowDownload)
            if status != "installed" {
                return Payload(ok: false, engine: engine, locale: loc.identifier, text: nil,
                               segments: nil, audio_seconds: audioSeconds, elapsed_ms: nil,
                               asset_status: status, downloaded: downloaded,
                               contextual_terms: terms.count,
                               error: "asset_not_installed", detail: "status=\(status)")
            }

            let analyzer = SpeechAnalyzer(modules: [module])
            try await analyzer.setContext(context)
            let collector = Task { () -> [String] in
                var parts: [String] = []
                for try await result in module.results {
                    parts.append(String(result.text.characters))
                }
                return parts
            }
            _ = try await analyzer.analyzeSequence(from: file)
            try await analyzer.finalizeAndFinishThroughEndOfInput()
            let parts = try await collector.value
            return finish(engine: engine, locale: loc.identifier, parts: parts,
                          audioSeconds: audioSeconds, started: started,
                          status: status, downloaded: downloaded, terms: terms.count)

        default:
            return Payload(ok: false, engine: engine, locale: locale.identifier, text: nil,
                           segments: nil, audio_seconds: audioSeconds, elapsed_ms: nil,
                           asset_status: nil, downloaded: nil, contextual_terms: terms.count,
                           error: "bad_engine", detail: engine)
        }
    }

    static func finish(engine: String, locale: String, parts: [String],
                       audioSeconds: Double, started: Date,
                       status: String, downloaded: Bool, terms: Int) -> Payload {
        // 直接相接、不補空白：中文之間插空白會破壞後面的字典比對與 n-gram 去重。
        let text = parts.joined()
        return Payload(ok: true, engine: engine, locale: locale, text: text,
                       segments: parts.count, audio_seconds: audioSeconds,
                       elapsed_ms: Date().timeIntervalSince(started) * 1000,
                       asset_status: status, downloaded: downloaded,
                       contextual_terms: terms, error: nil, detail: nil)
    }
}
