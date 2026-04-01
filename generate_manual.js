const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  Header, Footer, AlignmentType, HeadingLevel, BorderStyle, WidthType,
  ShadingType, VerticalAlign, PageNumber, PageBreak, LevelFormat,
  TableOfContents, ExternalHyperlink, UnderlineType
} = require("docx");
const fs = require("fs");

// ── Color palette ─────────────────────────────────────────────────────────────
const C = {
  blue:       "1A5276",   // primary heading
  blueLight:  "2E86C1",   // secondary heading
  bluePale:   "D6EAF8",   // header bg
  blueRow:    "EBF5FB",   // table alt row
  green:      "1E8449",   // tip box bg text
  greenPale:  "D5F5E3",   // tip box bg
  orange:     "D35400",   // warning text
  orangePale: "FDEBD0",   // warning bg
  gray:       "566573",   // body muted
  grayLight:  "F2F3F4",   // code bg
  grayBorder: "AEB6BF",   // table border
  white:      "FFFFFF",
  black:      "1C2833",
  red:        "922B21",
  redPale:    "FADBD8",
};

// ── DXA helpers ───────────────────────────────────────────────────────────────
// A4: 11906 x 16838, margins 1134 each side → content = 9638
const PAGE_W    = 11906;
const PAGE_H    = 16838;
const MARGIN    = 1134;  // ~0.79"
const CONTENT_W = PAGE_W - MARGIN * 2;  // 9638

// ── Numbering config ──────────────────────────────────────────────────────────
const numbering = {
  config: [
    {
      reference: "bullets",
      levels: [{
        level: 0, format: LevelFormat.BULLET, text: "\u2022",
        alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 600, hanging: 300 } } }
      },{
        level: 1, format: LevelFormat.BULLET, text: "\u25E6",
        alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 1000, hanging: 300 } } }
      }]
    },
    {
      reference: "steps",
      levels: [{
        level: 0, format: LevelFormat.DECIMAL, text: "%1.",
        alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 720, hanging: 360 } } }
      }]
    },
  ]
};

// ── Border helpers ────────────────────────────────────────────────────────────
const borderSingle = (color = C.grayBorder, size = 4) =>
  ({ style: BorderStyle.SINGLE, size, color });

const noBorder = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };

const cellBorders = (color = C.grayBorder) => ({
  top: borderSingle(color), bottom: borderSingle(color),
  left: borderSingle(color), right: borderSingle(color),
});

// ── Text helpers ──────────────────────────────────────────────────────────────
const run = (text, opts = {}) => new TextRun({ text, font: "Arial", ...opts });
const bold = (text, color) => run(text, { bold: true, color });
const code = (text) => new TextRun({ text, font: "Courier New", size: 20, color: C.blue });

const para = (children, opts = {}) => new Paragraph({
  children: Array.isArray(children) ? children : [children],
  spacing: { after: 120 },
  ...opts
});

const h1 = (text) => new Paragraph({
  heading: HeadingLevel.HEADING_1,
  children: [new TextRun({ text, font: "Arial", size: 36, bold: true, color: C.blue })],
  spacing: { before: 400, after: 200 },
  border: { bottom: { style: BorderStyle.SINGLE, size: 8, color: C.blue, space: 4 } },
});

const h2 = (text) => new Paragraph({
  heading: HeadingLevel.HEADING_2,
  children: [new TextRun({ text, font: "Arial", size: 28, bold: true, color: C.blueLight })],
  spacing: { before: 280, after: 140 },
});

const h3 = (text) => new Paragraph({
  heading: HeadingLevel.HEADING_3,
  children: [new TextRun({ text, font: "Arial", size: 24, bold: true, color: C.gray })],
  spacing: { before: 200, after: 100 },
});

const bullet = (text, level = 0) => new Paragraph({
  numbering: { reference: "bullets", level },
  children: [run(text, { size: 22 })],
  spacing: { after: 80 },
});

const step = (text) => new Paragraph({
  numbering: { reference: "steps", level: 0 },
  children: [run(text, { size: 22 })],
  spacing: { after: 100 },
});

const spacer = (pts = 80) => new Paragraph({
  children: [run("")],
  spacing: { after: pts },
});

// ── Callout box (tip / warning) ───────────────────────────────────────────────
const callout = (label, text, bgColor, textColor) => {
  const border = borderSingle(textColor, 6);
  return new Table({
    width: { size: CONTENT_W, type: WidthType.DXA },
    columnWidths: [CONTENT_W],
    borders: { top: border, bottom: border, left: border, right: border },
    rows: [new TableRow({ children: [
      new TableCell({
        width: { size: CONTENT_W, type: WidthType.DXA },
        shading: { fill: bgColor, type: ShadingType.CLEAR },
        margins: { top: 100, bottom: 100, left: 160, right: 160 },
        borders: { top: border, bottom: border, left: border, right: border },
        children: [new Paragraph({
          children: [
            new TextRun({ text: `${label}  `, font: "Arial", bold: true, size: 22, color: textColor }),
            new TextRun({ text, font: "Arial", size: 22, color: C.black }),
          ],
          spacing: { after: 0 }
        })]
      })
    ]})]
  });
};

const tip     = (text) => callout("💡 提示", text, C.greenPale, C.green);
const warning = (text) => callout("⚠️ 注意", text, C.orangePale, C.orange);
const info    = (text) => callout("ℹ️ 說明", text, C.bluePale, C.blueLight);

// ── Standard table ────────────────────────────────────────────────────────────
const makeTable = (headers, rows, colWidths) => {
  const totalW = colWidths.reduce((a, b) => a + b, 0);
  const headerRow = new TableRow({
    tableHeader: true,
    children: headers.map((h, i) => new TableCell({
      width: { size: colWidths[i], type: WidthType.DXA },
      shading: { fill: C.blue, type: ShadingType.CLEAR },
      margins: { top: 80, bottom: 80, left: 120, right: 120 },
      borders: cellBorders(C.blue),
      verticalAlign: VerticalAlign.CENTER,
      children: [new Paragraph({
        children: [new TextRun({ text: h, font: "Arial", bold: true, size: 22, color: C.white })],
        alignment: AlignmentType.CENTER,
        spacing: { after: 0 }
      })]
    }))
  });

  const dataRows = rows.map((row, ri) => new TableRow({
    children: row.map((cell, ci) => new TableCell({
      width: { size: colWidths[ci], type: WidthType.DXA },
      shading: { fill: ri % 2 === 0 ? C.white : C.blueRow, type: ShadingType.CLEAR },
      margins: { top: 80, bottom: 80, left: 120, right: 120 },
      borders: cellBorders(C.grayBorder),
      verticalAlign: VerticalAlign.CENTER,
      children: [new Paragraph({
        children: typeof cell === "string"
          ? [new TextRun({ text: cell, font: "Arial", size: 22, color: C.black })]
          : cell,
        spacing: { after: 0 }
      })]
    }))
  }));

  return new Table({
    width: { size: CONTENT_W, type: WidthType.DXA },
    columnWidths: colWidths,
    rows: [headerRow, ...dataRows],
  });
};

// ── Code block ────────────────────────────────────────────────────────────────
const codeBlock = (lines) => new Table({
  width: { size: CONTENT_W, type: WidthType.DXA },
  columnWidths: [CONTENT_W],
  rows: [new TableRow({ children: [new TableCell({
    width: { size: CONTENT_W, type: WidthType.DXA },
    shading: { fill: "1C2833", type: ShadingType.CLEAR },
    margins: { top: 120, bottom: 120, left: 200, right: 200 },
    borders: { top: borderSingle("1C2833"), bottom: borderSingle("1C2833"),
               left: borderSingle("1C2833"), right: borderSingle("1C2833") },
    children: lines.map(l => new Paragraph({
      children: [new TextRun({ text: l, font: "Courier New", size: 20, color: "58D68D" })],
      spacing: { after: 40 }
    }))
  })]})],
});

// ── UI diagram table ──────────────────────────────────────────────────────────
const uiDiagram = () => {
  const diagramBorder = borderSingle("2E86C1", 8);
  const diagramBorders = {
    top: diagramBorder, bottom: diagramBorder,
    left: diagramBorder, right: diagramBorder
  };
  const sepBorder = borderSingle("AEB6BF", 4);
  const sepBorders = { top: noBorder, bottom: sepBorder, left: noBorder, right: noBorder };

  const row = (label, desc, bg = C.white) => new TableRow({ children: [
    new TableCell({
      width: { size: 1200, type: WidthType.DXA },
      shading: { fill: C.blue, type: ShadingType.CLEAR },
      margins: { top: 60, bottom: 60, left: 100, right: 100 },
      borders: { top: noBorder, bottom: sepBorder, left: diagramBorder, right: noBorder },
      verticalAlign: VerticalAlign.CENTER,
      children: [new Paragraph({
        children: [new TextRun({ text: label, font: "Arial", bold: true, size: 22, color: C.white })],
        alignment: AlignmentType.CENTER, spacing: { after: 0 }
      })]
    }),
    new TableCell({
      width: { size: CONTENT_W - 1200, type: WidthType.DXA },
      shading: { fill: bg, type: ShadingType.CLEAR },
      margins: { top: 60, bottom: 60, left: 140, right: 100 },
      borders: { top: noBorder, bottom: sepBorder, left: noBorder, right: diagramBorder },
      children: [new Paragraph({
        children: [new TextRun({ text: desc, font: "Courier New", size: 20, color: C.black })],
        spacing: { after: 0 }
      })]
    }),
  ]});

  return new Table({
    width: { size: CONTENT_W, type: WidthType.DXA },
    columnWidths: [1200, CONTENT_W - 1200],
    borders: { top: diagramBorder, bottom: diagramBorder,
               left: diagramBorder, right: diagramBorder },
    rows: [
      row("❶ 頂部列", "🎙  Whisper 語音轉文字     [模型: base ▾]  [語言: 自動偵測 ▾]", C.bluePale),
      row("❷ 波形區", "░░░░▁▂▃▄▅▄▃▂▁░░░░  即時音量視覺化動畫", C.white),
      row("❸ 錄音鍵", "         ╭────────────────╮         \n         │ 🎤  點擊錄音   │         \n         ╰────────────────╯         ", C.white),
      row("❹ 快捷鍵", "──── 或按住快捷鍵 ⌘⇧Space 錄音 ────", C.white),
      row("❺ 結果區", "📝 轉錄結果    (15s · ZH · base)  [清除 ✕]\n─────────────────────────────────────\n  轉錄的文字顯示在此，支援捲動與多次追加...", C.grayLight),
      row("❻ 操作列", "[📋 複製]  [💾 存檔]  [✨ Ollama 潤飾]  [⚙ 設定]", C.bluePale),
      row("❼ 狀態列", "● 模型已就緒 (base)     快捷鍵: ⌘⇧Space     00:00", C.grayLight),
    ]
  });
};

// ── State machine diagram ─────────────────────────────────────────────────────
const stateDiagram = () => {
  const states = [
    { label: "IDLE", color: "1E8449", bg: C.greenPale, desc: "待機（綠色）" },
    { label: "RECORDING", color: "922B21", bg: C.redPale, desc: "錄音中（紅色脈衝）" },
    { label: "PROCESSING", color: "D35400", bg: C.orangePale, desc: "轉錄中（橘色）" },
  ];
  const arrows = ["──[按下]──►", "──[放開]──►", "──[完成]──►"];

  const children = [];
  states.forEach((s, i) => {
    children.push(new TableCell({
      width: { size: 2200, type: WidthType.DXA },
      shading: { fill: s.bg, type: ShadingType.CLEAR },
      margins: { top: 100, bottom: 100, left: 80, right: 80 },
      borders: { top: borderSingle(s.color, 8), bottom: borderSingle(s.color, 8),
                 left: borderSingle(s.color, 8), right: borderSingle(s.color, 8) },
      verticalAlign: VerticalAlign.CENTER,
      children: [
        new Paragraph({ children: [new TextRun({ text: s.label, font: "Arial", bold: true, size: 22, color: s.color })], alignment: AlignmentType.CENTER, spacing: { after: 40 } }),
        new Paragraph({ children: [new TextRun({ text: s.desc, font: "Arial", size: 18, color: C.gray })], alignment: AlignmentType.CENTER, spacing: { after: 0 } }),
      ]
    }));
    if (i < arrows.length) {
      children.push(new TableCell({
        width: { size: 900, type: WidthType.DXA },
        shading: { fill: C.white, type: ShadingType.CLEAR },
        margins: { top: 100, bottom: 100, left: 20, right: 20 },
        borders: { top: noBorder, bottom: noBorder, left: noBorder, right: noBorder },
        verticalAlign: VerticalAlign.CENTER,
        children: [new Paragraph({ children: [new TextRun({ text: arrows[i], font: "Arial", size: 18, color: C.gray })], alignment: AlignmentType.CENTER, spacing: { after: 0 } })]
      }));
    }
  });

  return new Table({
    width: { size: CONTENT_W, type: WidthType.DXA },
    columnWidths: [2200, 900, 2200, 900, 2200],
    rows: [new TableRow({ children })],
    borders: { top: noBorder, bottom: noBorder, left: noBorder, right: noBorder },
  });
};

// ── Model comparison table ────────────────────────────────────────────────────
const modelTable = () => makeTable(
  ["模型", "參數量", "速度", "精準度", "建議用途"],
  [
    ["tiny",   "39M",   "⚡⚡⚡⚡⚡",  "★★☆☆☆",  "快速英文速記、測試用"],
    ["base",   "74M",   "⚡⚡⚡⚡",   "★★★☆☆",  "日常推薦，速度與精準度平衡"],
    ["small",  "244M",  "⚡⚡⚡",    "★★★★☆",  "中文效果佳，稍慢"],
    ["medium", "769M",  "⚡⚡",     "★★★★★",  "高精準度，需 8GB+ RAM"],
    ["large",  "1.5G",  "⚡",      "★★★★★",  "最高精準度，需 16GB+ RAM"],
  ],
  [1200, 1000, 1800, 1600, 3638 - 200]
);

// ── FAQ table ─────────────────────────────────────────────────────────────────
const faqEntry = (q, a) => [
  para([bold(`Q：${q}`, C.blue)], { spacing: { before: 160, after: 60 } }),
  para([run(a, { size: 22 })], { spacing: { after: 120 },
    indent: { left: 240 } }),
];

// ═══════════════════════════════════════════════════════════════════════════════
//  DOCUMENT
// ═══════════════════════════════════════════════════════════════════════════════

const doc = new Document({
  creator: "Whisper App",
  title: "Whisper 語音轉文字小幫手 使用手冊",
  description: "完整使用說明文件",
  numbering,

  styles: {
    default: {
      document: { run: { font: "Arial", size: 22, color: C.black } }
    },
    paragraphStyles: [
      {
        id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 36, bold: true, font: "Arial", color: C.blue },
        paragraph: { spacing: { before: 400, after: 200 }, outlineLevel: 0 }
      },
      {
        id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 28, bold: true, font: "Arial", color: C.blueLight },
        paragraph: { spacing: { before: 280, after: 140 }, outlineLevel: 1 }
      },
      {
        id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 24, bold: true, font: "Arial", color: C.gray },
        paragraph: { spacing: { before: 200, after: 100 }, outlineLevel: 2 }
      },
    ]
  },

  sections: [
    // ═══════════════════════════════════
    //  封面頁
    // ═══════════════════════════════════
    {
      properties: {
        page: {
          size: { width: PAGE_W, height: PAGE_H },
          margin: { top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN }
        }
      },
      children: [
        spacer(1400),

        // 藍色標題背景條
        new Table({
          width: { size: CONTENT_W, type: WidthType.DXA },
          columnWidths: [CONTENT_W],
          rows: [new TableRow({ children: [new TableCell({
            width: { size: CONTENT_W, type: WidthType.DXA },
            shading: { fill: C.blue, type: ShadingType.CLEAR },
            margins: { top: 300, bottom: 300, left: 300, right: 300 },
            borders: { top: borderSingle(C.blue), bottom: borderSingle(C.blue),
                       left: borderSingle(C.blue), right: borderSingle(C.blue) },
            children: [
              new Paragraph({
                children: [new TextRun({ text: "🎙", font: "Arial", size: 80 })],
                alignment: AlignmentType.CENTER, spacing: { after: 120 }
              }),
              new Paragraph({
                children: [new TextRun({ text: "Whisper 語音轉文字小幫手", font: "Arial", bold: true, size: 64, color: C.white })],
                alignment: AlignmentType.CENTER, spacing: { after: 80 }
              }),
              new Paragraph({
                children: [new TextRun({ text: "Speech-to-Text Assistant", font: "Arial", size: 32, color: "B3D7F0" })],
                alignment: AlignmentType.CENTER, spacing: { after: 0 }
              }),
            ]
          })]})],
        }),

        spacer(400),

        new Paragraph({
          children: [new TextRun({ text: "完整使用手冊", font: "Arial", size: 32, color: C.gray })],
          alignment: AlignmentType.CENTER, spacing: { after: 80 }
        }),
        new Paragraph({
          children: [new TextRun({ text: "User Manual v1.0", font: "Arial", size: 24, color: C.grayBorder })],
          alignment: AlignmentType.CENTER, spacing: { after: 120 }
        }),

        spacer(400),

        // 特色列表
        new Table({
          width: { size: 6000, type: WidthType.DXA },
          columnWidths: [6000],
          rows: [
            ["✅  本地執行，無需網路，無需 API Key",  C.greenPale],
            ["🌐  支援中文、英文、日文等多語言",      C.bluePale],
            ["⌘  全域快捷鍵 Push-to-Talk 操作",     C.bluePale],
            ["🤖  未來支援 Ollama 大語言模型潤飾",   C.greenPale],
          ].map(([text, bg]) => new TableRow({ children: [new TableCell({
            width: { size: 6000, type: WidthType.DXA },
            shading: { fill: bg, type: ShadingType.CLEAR },
            margins: { top: 80, bottom: 80, left: 160, right: 160 },
            borders: { top: borderSingle(C.grayBorder, 2), bottom: borderSingle(C.grayBorder, 2),
                       left: borderSingle(C.grayBorder, 2), right: borderSingle(C.grayBorder, 2) },
            children: [new Paragraph({
              children: [new TextRun({ text, font: "Arial", size: 24, color: C.black })],
              spacing: { after: 0 }
            })]
          })]}))
        }),

        spacer(600),

        new Paragraph({
          children: [new TextRun({ text: `最後更新：2026 年 3 月 31 日`, font: "Arial", size: 20, color: C.grayBorder })],
          alignment: AlignmentType.CENTER, spacing: { after: 0 }
        }),

        // 頁面結束
        new Paragraph({ children: [new PageBreak()] }),
      ]
    },

    // ═══════════════════════════════════
    //  主文件（目錄 + 內文）
    // ═══════════════════════════════════
    {
      properties: {
        page: {
          size: { width: PAGE_W, height: PAGE_H },
          margin: { top: MARGIN, right: MARGIN, bottom: MARGIN + 400, left: MARGIN }
        }
      },
      headers: {
        default: new Header({
          children: [new Paragraph({
            children: [
              new TextRun({ text: "🎙  Whisper 語音轉文字小幫手 — 使用手冊", font: "Arial", size: 18, color: C.gray }),
            ],
            border: { bottom: { style: BorderStyle.SINGLE, size: 4, color: C.grayBorder, space: 4 } },
            spacing: { after: 0 }
          })]
        })
      },
      footers: {
        default: new Footer({
          children: [new Paragraph({
            children: [
              new TextRun({ text: "v1.0  |  2026-03-31        第 ", font: "Arial", size: 18, color: C.gray }),
              new TextRun({ children: [PageNumber.CURRENT], font: "Arial", size: 18, color: C.blue }),
              new TextRun({ text: " 頁", font: "Arial", size: 18, color: C.gray }),
            ],
            alignment: AlignmentType.CENTER,
            border: { top: { style: BorderStyle.SINGLE, size: 4, color: C.grayBorder, space: 4 } },
            spacing: { after: 0 }
          })]
        })
      },

      children: [

        // ── 目錄 ──────────────────────────────────────────────────────────
        h1("目錄"),
        new TableOfContents("目錄", {
          hyperlink: true,
          headingStyleRange: "1-2",
          stylesWithLevels: [],
        }),
        new Paragraph({ children: [new PageBreak()] }),

        // ── CH 1 系統需求 ─────────────────────────────────────────────────
        h1("1   系統需求"),
        para([run("在開始使用 Whisper 語音轉文字小幫手之前，請確認您的電腦符合以下需求。", { size: 22 })]),
        spacer(),

        makeTable(
          ["項目", "需求"],
          [
            ["作業系統",   "macOS 12 Monterey 以上（建議 macOS 14 Sonoma）"],
            ["Python 版本", "3.10 以上（建議 3.13）"],
            ["套件管理",   "Homebrew（用於安裝 portaudio、ffmpeg）"],
            ["磁碟空間",   "base 模型約 145 MB；large 模型約 3 GB"],
            ["記憶體",     "base/small 模型：建議 8 GB；medium/large：16 GB+"],
            ["麥克風",     "內建或外接麥克風皆可"],
            ["網路",       "首次下載模型時需要；後續完全離線運行"],
          ],
          [2800, CONTENT_W - 2800]
        ),
        spacer(),
        tip("首次啟動會自動從 Hugging Face 下載所選的 Whisper 模型，之後完全本地運行，不需要網路也不需要 API Key。"),

        spacer(200),
        new Paragraph({ children: [new PageBreak()] }),

        // ── CH 2 安裝步驟 ─────────────────────────────────────────────────
        h1("2   安裝步驟"),

        h2("2.1   安裝系統依賴"),
        para([run("開啟終端機（Terminal），依序執行以下指令：", { size: 22 })]),
        spacer(),
        codeBlock([
          "# 安裝 PortAudio（麥克風驅動）與 ffmpeg（音訊處理）",
          "brew install portaudio ffmpeg",
        ]),
        spacer(),

        makeTable(
          ["套件", "用途", "安裝時間"],
          [
            ["portaudio", "麥克風錄音底層驅動，sounddevice 依賴此套件",    "約 30 秒"],
            ["ffmpeg",    "音訊格式處理，Whisper 模型轉換音訊時使用",    "約 2–3 分鐘"],
          ],
          [1400, CONTENT_W - 1400 - 1200, 1200]
        ),
        spacer(),

        h2("2.2   建立 Python 虛擬環境"),
        para([run("建議使用虛擬環境隔離依賴，避免影響系統 Python：", { size: 22 })]),
        spacer(),
        codeBlock([
          "cd /Users/jerrychen/project/Claude_code",
          "",
          "# 建立虛擬環境",
          "python3 -m venv venv",
          "",
          "# 啟動虛擬環境",
          "source venv/bin/activate",
        ]),
        spacer(),

        h2("2.3   安裝 Python 套件"),
        codeBlock([
          'pip install customtkinter==5.2.2 sounddevice==0.5.1 "numpy<3" \\',
          "            faster-whisper pyperclip==1.9.0 pynput==1.7.7 requests==2.32.3",
        ]),
        spacer(),

        makeTable(
          ["套件", "版本", "用途"],
          [
            ["customtkinter", "5.2.2", "現代風格 GUI 介面框架（基於 tkinter）"],
            ["sounddevice",   "0.5.1", "麥克風音訊錄製"],
            ["faster-whisper","最新",  "Whisper 語音辨識（比原版快 3–5x）"],
            ["pyperclip",     "1.9.0", "系統剪貼簿讀寫"],
            ["pynput",        "1.7.7", "全域快捷鍵監聽"],
            ["requests",      "2.32.3","Ollama HTTP API 呼叫（預留）"],
          ],
          [2000, 1000, CONTENT_W - 3000]
        ),
        spacer(200),
        new Paragraph({ children: [new PageBreak()] }),

        // ── CH 3 啟動 App ─────────────────────────────────────────────────
        h1("3   啟動 App"),

        h2("3.1   執行指令"),
        codeBlock([
          "cd /Users/jerrychen/project/Claude_code",
          "venv/bin/python3 main.py",
        ]),
        spacer(),
        info("若已透過 source venv/bin/activate 啟動虛擬環境，可直接執行 python3 main.py。"),
        spacer(),

        h2("3.2   首次啟動流程"),
        para([run("第一次啟動時，App 會執行以下初始化步驟：", { size: 22 })]),
        spacer(),
        step("App 視窗開啟，狀態列顯示「模型載入中…」"),
        step("背景自動從 Hugging Face 下載 Whisper base 模型（約 145 MB）"),
        step("模型下載完成後，狀態列更新為「模型已就緒 (base)」"),
        step("若偵測到缺少 macOS 輔助使用權限，彈出引導視窗"),
        step("App 就緒，可開始錄音"),
        spacer(),
        warning("若狀態列長時間顯示「模型載入中」，請確認網路連線正常。模型會快取到 ~/.cache/huggingface/hub/，重啟後不需重新下載。"),

        spacer(200),
        new Paragraph({ children: [new PageBreak()] }),

        // ── CH 4 介面說明 ─────────────────────────────────────────────────
        h1("4   介面說明"),

        h2("4.1   主視窗佈局"),
        para([run("主視窗固定尺寸為 720 × 680 px，分為七個功能區塊：", { size: 22 })]),
        spacer(),

        uiDiagram(),
        spacer(),

        h2("4.2   各區塊功能說明"),
        makeTable(
          ["編號", "名稱", "功能說明"],
          [
            ["❶", "頂部工具列", "快速切換 Whisper 模型大小（tiny/base/small/medium/large）與辨識語言"],
            ["❷", "音量波形",  "錄音中即時顯示聲音強度動畫（藍→紅 gradient）；待機時顯示靜態灰色波形"],
            ["❸", "錄音按鈕",  "點擊開始錄音，再次點擊停止。顏色反映目前狀態（見第 5 章）"],
            ["❹", "快捷鍵提示","顯示目前設定的全域快捷鍵組合，按住即可錄音"],
            ["❺", "轉錄結果",  "顯示辨識完成的文字，支援捲動。標題顯示錄音時長、語言、使用模型"],
            ["❻", "操作列",   "複製、存檔、Ollama 潤飾（預留）、設定"],
            ["❼", "狀態列",   "顯示模型狀態、快捷鍵提示、錄音計時器（MM:SS）"],
          ],
          [600, 1600, CONTENT_W - 2200]
        ),

        spacer(200),
        new Paragraph({ children: [new PageBreak()] }),

        // ── CH 5 錄音操作 ─────────────────────────────────────────────────
        h1("5   錄音操作"),

        h2("5.1   按鈕狀態說明"),
        makeTable(
          ["按鈕顏色", "狀態", "說明"],
          [
            ["🟢 綠色",  "IDLE（待機）",      "就緒，等待錄音。點擊即可開始。"],
            ["🔴 紅色脈衝", "RECORDING（錄音中）", "正在收音。計時器顯示錄音時長，波形實時更新。"],
            ["🟠 橘色",  "PROCESSING（轉錄中）", "Whisper 模型分析音訊，轉圈動畫顯示進度，完成後自動回到待機。"],
          ],
          [1500, 2000, CONTENT_W - 3500]
        ),
        spacer(),

        h2("5.2   操作方式一：點擊按鈕"),
        spacer(),
        new Table({
          width: { size: CONTENT_W, type: WidthType.DXA },
          columnWidths: [800, 2400, CONTENT_W - 3200],
          rows: [
            new TableRow({ tableHeader: true, children: [
              ...(["步驟", "操作", "畫面變化"].map((h, i) => new TableCell({
                width: { size: [800,2400,CONTENT_W-3200][i], type: WidthType.DXA },
                shading: { fill: C.blue, type: ShadingType.CLEAR },
                margins: { top: 80, bottom: 80, left: 120, right: 120 },
                borders: cellBorders(C.blue),
                children: [new Paragraph({ children: [new TextRun({ text: h, font: "Arial", bold: true, size: 22, color: C.white })], alignment: AlignmentType.CENTER, spacing: { after: 0 } })]
              })))
            ]}),
            ...[
              ["1", "點擊綠色「🎤 點擊錄音」按鈕", "按鈕變為紅色脈衝，波形開始動畫，計時器啟動"],
              ["2", "對麥克風說話", "波形高度隨音量即時變化，狀態列顯示「🔴 錄音中」"],
              ["3", "再次點擊按鈕停止", "按鈕變為橘色「轉錄中…」，波形回到靜態"],
              ["4", "等待轉錄完成", "結果文字出現在結果區，右下角顯示完成通知"],
            ].map(([step, op, change], ri) => new TableRow({ children: [
              ...[step, op, change].map((text, ci) => new TableCell({
                width: { size: [800,2400,CONTENT_W-3200][ci], type: WidthType.DXA },
                shading: { fill: ri % 2 === 0 ? C.white : C.blueRow, type: ShadingType.CLEAR },
                margins: { top: 80, bottom: 80, left: 120, right: 120 },
                borders: cellBorders(C.grayBorder),
                children: [new Paragraph({ children: [new TextRun({ text, font: "Arial", size: 22, color: ci === 0 ? C.blue : C.black, bold: ci === 0 })], spacing: { after: 0 } })]
              }))
            ]}))
          ]
        }),
        spacer(),

        h2("5.3   操作方式二：全域快捷鍵（Push-to-Talk）"),
        para([run("這是最推薦的使用方式，可在任何 App 前景下操作，無需切換視窗：", { size: 22 })]),
        spacer(),

        new Table({
          width: { size: CONTENT_W, type: WidthType.DXA },
          columnWidths: [CONTENT_W / 3, CONTENT_W / 3, CONTENT_W / 3],
          rows: [new TableRow({ children: [
            ["按住 ⌘⇧Space", C.greenPale, C.green, "說話中"],
            ["→ 說話 →",     C.bluePale,  C.blue,  "轉錄處理中"],
            ["放開 ⌘⇧Space", C.orangePale,C.orange,"自動完成"],
          ].map(([text, bg, color, sub], i) => new TableCell({
            width: { size: CONTENT_W / 3, type: WidthType.DXA },
            shading: { fill: bg, type: ShadingType.CLEAR },
            margins: { top: 120, bottom: 120, left: 100, right: 100 },
            borders: cellBorders(color),
            verticalAlign: VerticalAlign.CENTER,
            children: [
              new Paragraph({ children: [new TextRun({ text, font: "Arial", bold: true, size: 28, color })], alignment: AlignmentType.CENTER, spacing: { after: 40 } }),
              new Paragraph({ children: [new TextRun({ text: sub, font: "Arial", size: 18, color: C.gray })], alignment: AlignmentType.CENTER, spacing: { after: 0 } }),
            ]
          }))})]
        }),
        spacer(),

        h2("5.4   狀態機流程圖"),
        para([run("App 在三個狀態之間切換，確保操作不會互相干擾：", { size: 22 })]),
        spacer(),
        stateDiagram(),
        spacer(),
        para([run("說明：每次只能處於一個狀態，按鈕在 PROCESSING 中自動 disabled，防止重複觸發。", { size: 20, color: C.gray, italics: true })]),

        spacer(200),
        new Paragraph({ children: [new PageBreak()] }),

        // ── CH 6 全域快捷鍵 ───────────────────────────────────────────────
        h1("6   全域快捷鍵"),

        h2("6.1   預設快捷鍵"),
        new Table({
          width: { size: CONTENT_W, type: WidthType.DXA },
          columnWidths: [CONTENT_W],
          rows: [new TableRow({ children: [new TableCell({
            width: { size: CONTENT_W, type: WidthType.DXA },
            shading: { fill: C.blue, type: ShadingType.CLEAR },
            margins: { top: 200, bottom: 200, left: 200, right: 200 },
            borders: cellBorders(C.blue),
            children: [new Paragraph({ children: [new TextRun({ text: "⌘  +  ⇧  +  Space", font: "Arial", bold: true, size: 60, color: C.white })], alignment: AlignmentType.CENTER, spacing: { after: 0 } })]
          })]})],
        }),
        spacer(),
        para([run("即 Command + Shift + 空白鍵，設計為不易與系統快捷鍵衝突的組合。", { size: 22 })]),
        spacer(),

        h2("6.2   macOS 輔助使用權限"),
        para([run("全域快捷鍵需要 macOS「輔助使用（Accessibility）」權限，才能在其他 App 前景時監聽按鍵。", { size: 22 })]),
        spacer(),

        makeTable(
          ["步驟", "操作"],
          [
            ["1", "點擊 App 啟動時的引導彈窗「開啟系統設定」，或手動前往：\n系統設定 → 隱私權與安全性 → 輔助使用"],
            ["2", "點擊左下角「+」按鈕"],
            ["3", "找到並選擇 Terminal（或執行 python3 的 App）"],
            ["4", "確認右側切換開關為「開啟」（藍色）"],
            ["5", "重新啟動 Whisper App"],
          ],
          [600, CONTENT_W - 600]
        ),
        spacer(),
        tip("若跳過此步驟，仍可在 App 視窗有焦點時透過點擊按鈕錄音。全域快捷鍵僅用於在其他 App 前景下快速觸發。"),
        spacer(),

        h2("6.3   自訂快捷鍵"),
        step("點擊操作列「⚙ 設定」按鈕"),
        step("找到「快捷鍵」區塊，點擊「重新綁定」"),
        step("在彈出的對話框中，按下你想要的按鍵組合（例如 Ctrl + Alt + R）"),
        step("對話框即時顯示偵測到的按鍵"),
        step("放開按鍵後，點擊「確認套用」"),
        step("在設定視窗點擊「儲存」，快捷鍵立即生效"),
        spacer(),
        warning("不建議使用單鍵或常用組合（如 Cmd+C、Cmd+V），以免干擾其他 App 操作。"),

        spacer(200),
        new Paragraph({ children: [new PageBreak()] }),

        // ── CH 7 轉錄結果操作 ─────────────────────────────────────────────
        h1("7   轉錄結果操作"),

        h2("7.1   操作按鈕說明"),
        makeTable(
          ["按鈕", "功能", "說明"],
          [
            ["📋 複製", "複製到剪貼簿", "將結果區所有文字複製至系統剪貼簿，右下角顯示「已複製」通知"],
            ["💾 存檔", "儲存為文字檔",  "開啟 Finder 儲存對話框，選擇路徑後存成 .txt 格式"],
            ["清除 ✕", "清空結果",      "清除結果區所有文字，重設標題"],
            ["✨ Ollama 潤飾", "AI 潤飾文字", "透過本地 Ollama 大語言模型修正文字（需額外設定，見第 10 章）"],
          ],
          [1600, 1800, CONTENT_W - 3400]
        ),
        spacer(),

        h2("7.2   多次錄音追加模式"),
        para([run("預設開啟「追加模式」，每次新的錄音結果會附加到現有文字下方，並以分隔線區分，方便整理多段錄音。", { size: 22 })]),
        spacer(),
        new Table({
          width: { size: CONTENT_W, type: WidthType.DXA },
          columnWidths: [CONTENT_W],
          rows: [new TableRow({ children: [new TableCell({
            width: { size: CONTENT_W, type: WidthType.DXA },
            shading: { fill: "1C2833", type: ShadingType.CLEAR },
            margins: { top: 120, bottom: 120, left: 200, right: 200 },
            borders: cellBorders("1C2833"),
            children: [
              new Paragraph({ children: [new TextRun({ text: "第一段錄音文字...", font: "Courier New", size: 20, color: "A9DFBF" })], spacing: { after: 60 } }),
              new Paragraph({ children: [new TextRun({ text: "── ── ── ── ──", font: "Courier New", size: 20, color: "566573" })], spacing: { after: 60 } }),
              new Paragraph({ children: [new TextRun({ text: "第二段錄音文字...", font: "Courier New", size: 20, color: "A9DFBF" })], spacing: { after: 0 } }),
            ]
          })]})],
        }),
        spacer(),
        para([run("可在「設定 → 輸出 → 每次錄音追加到結果」關閉此功能，改為每次覆蓋。", { size: 22, color: C.gray })]),
        spacer(),

        h2("7.3   結果區標題資訊"),
        para([run("轉錄完成後，結果區標題會顯示本次辨識的統計資訊：", { size: 22 })]),
        spacer(),
        codeBlock(["📝 轉錄結果  (15s · ZH · base)"]),
        spacer(),
        makeTable(
          ["欄位", "說明", "範例"],
          [
            ["15s",   "本次錄音時長",     "15 秒"],
            ["ZH",    "Whisper 偵測到的語言代碼", "ZH=中文、EN=英文、JA=日文"],
            ["base",  "使用的 Whisper 模型", "tiny/base/small/medium/large"],
          ],
          [800, CONTENT_W - 2400, 1600]
        ),

        spacer(200),
        new Paragraph({ children: [new PageBreak()] }),

        // ── CH 8 設定面板 ─────────────────────────────────────────────────
        h1("8   設定面板"),
        para([run("點擊操作列「⚙ 設定」開啟設定視窗（400 × 520 px）。", { size: 22 })]),
        spacer(),

        h2("8.1   語音辨識設定"),
        makeTable(
          ["設定項目", "選項", "說明"],
          [
            ["模型大小", "tiny / base / small / medium / large", "影響辨識速度與準確度（詳見第 9 章）"],
            ["辨識語言", "自動偵測 / 中文 / English / 日本語 / 韓語 / 等", "指定語言可提升準確度，自動偵測適合多語言場景"],
          ],
          [2000, 3000, CONTENT_W - 5000]
        ),
        spacer(),

        h2("8.2   輸出偏好"),
        makeTable(
          ["設定項目", "預設值", "說明"],
          [
            ["每次錄音追加到結果", "開啟 ✅", "開啟：新結果附加到現有文字下方；關閉：每次覆蓋"],
            ["轉錄後自動複製",     "關閉 ❌", "開啟後，每次轉錄完成自動將文字複製到剪貼簿"],
          ],
          [2400, 1200, CONTENT_W - 3600]
        ),
        spacer(),

        h2("8.3   關於"),
        makeTable(
          ["項目", "路徑"],
          [
            ["Whisper 模型快取",  "~/.cache/huggingface/hub/"],
            ["App 設定檔",        "~/.whisper_app/config.json"],
          ],
          [2400, CONTENT_W - 2400]
        ),
        spacer(),
        tip("點擊「開啟設定資料夾」可直接在 Finder 開啟 ~/.whisper_app/，方便備份或重置設定。"),

        spacer(200),
        new Paragraph({ children: [new PageBreak()] }),

        // ── CH 9 模型選擇指南 ─────────────────────────────────────────────
        h1("9   模型選擇指南"),
        para([run("Whisper 提供五種模型大小，在速度與準確度之間取得不同平衡。", { size: 22 })]),
        spacer(),

        modelTable(),
        spacer(),

        h2("9.1   推薦選擇"),
        makeTable(
          ["使用情境", "推薦模型", "原因"],
          [
            ["快速英文速記、會議摘要",   "tiny",   "速度極快，英文辨識足夠實用"],
            ["日常中英混合使用",         "base",   "預設值，速度與精準度最佳平衡"],
            ["中文為主的長篇錄音",       "small",  "中文辨識精準度明顯提升"],
            ["專業錄音、演講逐字稿",     "medium", "高精準度，適合要求高的場景"],
            ["最高品質需求",             "large",  "最大模型，精準度最高"],
          ],
          [2400, 1200, CONTENT_W - 3600]
        ),
        spacer(),

        h2("9.2   切換模型"),
        bullet("在頂部工具列「模型」下拉選單直接選擇"),
        bullet("或進入「設定」面板更改"),
        bullet("切換後首次轉錄時，App 會自動下載並載入新模型（需等待）"),
        bullet("模型下載後快取在本地，後續切換無需重複下載"),
        spacer(),
        warning("medium 和 large 模型需要大量記憶體。若電腦記憶體不足，建議使用 base 或 small 模型。"),

        spacer(200),
        new Paragraph({ children: [new PageBreak()] }),

        // ── CH 10 Ollama 整合 ─────────────────────────────────────────────
        h1("10   Ollama 整合（進階）"),
        para([run("Ollama 功能讓本地 AI 大語言模型自動潤飾轉錄文字，可修正錯字、調整語氣、重新斷句。目前為預留功能，需手動啟用。", { size: 22 })]),
        spacer(),

        h2("10.1   啟用步驟"),
        h3("步驟一：安裝 Ollama"),
        codeBlock([
          "# 使用 Homebrew 安裝",
          "brew install ollama",
          "",
          "# 或前往官網下載安裝包",
          "# https://ollama.com",
        ]),
        spacer(),

        h3("步驟二：下載語言模型"),
        codeBlock([
          "# 下載 Llama 3.2（約 2 GB）",
          "ollama pull llama3.2",
          "",
          "# 啟動 Ollama 服務",
          "ollama serve",
        ]),
        spacer(),

        h3("步驟三：啟用 App 內功能"),
        para([run("編輯專案目錄中的 ollama_client.py，找到第 19 行，將 False 改為 True：", { size: 22 })]),
        spacer(),
        codeBlock([
          "# 修改前",
          "OLLAMA_ENABLED: bool = False",
          "",
          "# 修改後",
          "OLLAMA_ENABLED: bool = True",
        ]),
        spacer(),
        step("儲存檔案"),
        step("重新啟動 App"),
        step("「✨ Ollama 潤飾」按鈕自動變為可點擊狀態"),
        spacer(),

        h2("10.2   使用方式"),
        step("完成一次錄音轉錄，確認結果區有文字"),
        step("點擊「✨ Ollama 潤飾」按鈕"),
        step("等待 AI 處理（依模型大小與文字長度約需 3–30 秒）"),
        step("結果區文字會被 AI 潤飾後的版本取代"),
        spacer(),
        info("App 預設使用的 Prompt 為：修正錯字、調整斷句、讓文字更通順自然。可在 ollama_client.py 的 DEFAULT_PROMPT 變數中自訂。"),

        spacer(200),
        new Paragraph({ children: [new PageBreak()] }),

        // ── CH 11 常見問題 ────────────────────────────────────────────────
        h1("11   常見問題"),
        spacer(),

        ...faqEntry(
          "App 啟動後一直顯示「模型載入中…」",
          "首次使用需要下載 Whisper 模型（base 約 145 MB）。請確認網路連線正常並耐心等待。下載完成後會自動更新為「模型已就緒」。"
        ),
        warning("模型快取於 ~/.cache/huggingface/hub/，重啟 App 後不需重新下載。"),
        spacer(),

        ...faqEntry(
          "點擊錄音後沒有聲音波形，也沒辦法轉錄",
          "sounddevice 無法存取麥克風，或 portaudio 安裝有問題。請確認：(1) 已安裝 portaudio（brew install portaudio）；(2) 前往系統設定 → 隱私權與安全性 → 麥克風，確認 Terminal 有麥克風權限；(3) 重新啟動 App。"
        ),
        spacer(),

        ...faqEntry(
          "全域快捷鍵按下去沒有反應",
          "缺少 macOS 輔助使用（Accessibility）權限。請前往系統設定 → 隱私權與安全性 → 輔助使用，加入並允許 Terminal，然後重新啟動 App。"
        ),
        spacer(),

        ...faqEntry(
          "轉錄結果語言不正確或準確度低",
          "建議：(1) 在頂部工具列「語言」手動指定語言（如「中文」）而非自動偵測；(2) 切換到較大的模型（small 或 medium）；(3) 確保錄音環境安靜，說話速度清晰。"
        ),
        spacer(),

        ...faqEntry(
          "App 報錯「缺少套件」",
          "請確認使用正確的虛擬環境執行：venv/bin/python3 main.py。或重新安裝套件：venv/bin/pip install -r requirements.txt"
        ),
        spacer(),

        ...faqEntry(
          "如何刪除已下載的 Whisper 模型？",
          "模型快取位於 ~/.cache/huggingface/hub/，可手動刪除對應的模型資料夾。例如刪除 base 模型：\nrm -rf ~/.cache/huggingface/hub/models--Systran--faster-whisper-base"
        ),
        spacer(),

        ...faqEntry(
          "如何重置所有設定？",
          "刪除設定檔即可重置為預設值：rm ~/.whisper_app/config.json。重新啟動 App 後，會自動建立預設設定。"
        ),

        spacer(200),
        new Paragraph({ children: [new PageBreak()] }),

        // ── CH 12 檔案結構 ────────────────────────────────────────────────
        h1("12   檔案結構參考"),
        para([run("專案目錄結構說明，供進階使用者參考：", { size: 22 })]),
        spacer(),

        makeTable(
          ["檔案", "說明"],
          [
            ["main.py",           "程式入口：依賴檢查、模型預熱、啟動視窗"],
            ["gui.py",            "主視窗 UI 與所有互動邏輯、狀態機"],
            ["transcriber.py",    "Whisper 模型封裝（執行緒安全）"],
            ["recorder.py",       "麥克風音訊錄製（sounddevice 封裝）"],
            ["hotkey_manager.py", "全域快捷鍵監聽（pynput 封裝）"],
            ["config.py",         "設定讀寫（~/.whisper_app/config.json）"],
            ["ollama_client.py",  "Ollama LLM 整合（預設停用，OLLAMA_ENABLED=False）"],
            ["requirements.txt",  "Python 套件清單"],
            ["venv/",             "Python 虛擬環境（不需上傳到 Git）"],
          ],
          [2400, CONTENT_W - 2400]
        ),
        spacer(),

        // 結語
        spacer(200),
        new Table({
          width: { size: CONTENT_W, type: WidthType.DXA },
          columnWidths: [CONTENT_W],
          rows: [new TableRow({ children: [new TableCell({
            width: { size: CONTENT_W, type: WidthType.DXA },
            shading: { fill: C.blue, type: ShadingType.CLEAR },
            margins: { top: 200, bottom: 200, left: 300, right: 300 },
            borders: cellBorders(C.blue),
            children: [
              new Paragraph({ children: [new TextRun({ text: "感謝使用 Whisper 語音轉文字小幫手", font: "Arial", bold: true, size: 28, color: C.white })], alignment: AlignmentType.CENTER, spacing: { after: 80 } }),
              new Paragraph({ children: [new TextRun({ text: "如有問題或建議，歡迎透過 GitHub Issues 回饋", font: "Arial", size: 22, color: "B3D7F0" })], alignment: AlignmentType.CENTER, spacing: { after: 0 } }),
            ]
          })]})],
        }),
      ]
    }
  ]
});

// ── 輸出 ──────────────────────────────────────────────────────────────────────
Packer.toBuffer(doc).then(buf => {
  fs.writeFileSync("Whisper語音轉文字_使用手冊.docx", buf);
  console.log("✅ 使用手冊已生成：Whisper語音轉文字_使用手冊.docx");
}).catch(err => {
  console.error("❌ 生成失敗：", err);
  process.exit(1);
});
