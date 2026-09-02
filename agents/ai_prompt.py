"""
AI Prompt Builder — 把今日 log 包成完整提问稿，供本地 AI CLI 或网页使用。

不调 API，零成本。流程：
  1. 读取今日 log（logs/run_YYYYMMDD.log）
  2. 截取最近 N 个完整 cycle（去掉 moomoo connect/disconnect 噪音）
  3. 包上明确的中文问题模板
  4. 保存到 signals/ai_prompt_YYYY-MM-DD.md
  5. 同步复制到 Windows 剪贴板（subprocess clip）

用户也可打开 Claude.ai / ChatGPT，Ctrl+V 手动使用。

独立调用:
  python ai_prompt.py [review|morning|deep]
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import BASE_DIR, SIGNALS_DIR


# 交易日志、AI 分析和自动挂单目标都属于美股交易日。运行机器在 JST，
# 若直接使用本地日期，纽约盘中跨过日本午夜后会突然读不到刚生成的目标。
ET = ZoneInfo("America/New_York")


def _market_date() -> str:
    return datetime.now(ET).date().isoformat()


# ── ANSI 颜色 (Windows CMD UTF-8 模式下可用) ────────────────────────────────
_ANSI = {
    "reset":  "\033[0m",
    "bold":   "\033[1m",
    "red":    "\033[91m",
    "green":  "\033[92m",
    "yellow": "\033[93m",
    "cyan":   "\033[96m",
    "magenta":"\033[95m",
    "gray":   "\033[90m",
}


def _enable_ansi_on_windows() -> None:
    """启用 Windows console 的 ANSI 转义码（Win10+ cmd.exe 默认未开启）。"""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        h = kernel32.GetStdHandle(-11)   # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
            kernel32.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


_enable_ansi_on_windows()


# 颜色规则：中文 + 日语方向性关键词
_BULL_KW = [
    # 中文
    "看涨", "偏多", "做多", "反弹", "多头共振", "上涨", "冲高", "突破",
    "关注买入", "买入信号", "高开", "强势上行", "金叉",
    # 日本語
    "強気", "ロング", "買い", "反発", "上昇", "上抜け", "ブレイク",
    "押し目買い", "高寄り", "ゴールデンクロス", "上方ブレイク",
]
_BEAR_KW = [
    # 中文
    "看空", "偏空", "做空", "减仓", "空头共振", "下跌", "暴跌", "回吐",
    "承压", "止损", "警示", "卖出信号", "风险", "低开", "强势下行", "死叉",
    # 日本語
    "弱気", "ショート", "売り", "下落", "暴落", "急落", "戻り売り",
    "損切り", "ロスカット", "警戒", "売りシグナル", "リスク",
    "安寄り", "デッドクロス", "下方ブレイク", "下げ", "利益確定",
]
_NEUTRAL_KW = [
    # 中文
    "观望", "中性", "均衡", "震荡",
    # 日本語
    "様子見", "中立", "レンジ", "もみ合い", "膠着",
]


def _colorize_markdown(text: str) -> str:
    """把 AI markdown 输出转成 ANSI 着色 + 去掉 markdown 标记的 CMD 友好版本。"""
    out = text

    # 表格分隔行 |---|---| 直接删（CMD 显示乱）
    out = re.sub(r"^\s*\|[\s\-:|]+\|\s*$", "", out, flags=re.MULTILINE)

    # 标题 ## / ### → 粗体青色 + 换行加分隔
    out = re.sub(
        r"^(#{1,4})\s*(.+?)$",
        lambda m: f"\n{_ANSI['bold']}{_ANSI['cyan']}━━ {m.group(2)} ━━{_ANSI['reset']}",
        out, flags=re.MULTILINE,
    )

    # 粗体 **text** → ANSI bold（去掉星号）
    out = re.sub(
        r"\*\*(.+?)\*\*",
        lambda m: f"{_ANSI['bold']}{m.group(1)}{_ANSI['reset']}",
        out,
    )
    # 斜体 *text* → 普通文字（去掉单星号）
    out = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", out)

    # 带号数字着色: +X.XX% 绿色, -X.XX% 红色
    # lookbehind 只挡数字和点（避免 2026-05-13 日期被误识）；中文字符不挡
    out = re.sub(
        r"(?<![\d.])\+(\d+\.?\d*)(%?)",
        lambda m: f"{_ANSI['green']}+{m.group(1)}{m.group(2)}{_ANSI['reset']}",
        out,
    )
    out = re.sub(
        r"(?<![\d.])-(\d+\.?\d*)(%)",
        lambda m: f"{_ANSI['red']}-{m.group(1)}{m.group(2)}{_ANSI['reset']}",
        out,
    )

    # 方向性关键词着色（注意先做大词再做小词避免嵌套）
    for kw in sorted(_BULL_KW, key=len, reverse=True):
        out = out.replace(kw, f"{_ANSI['green']}{kw}{_ANSI['reset']}")
    for kw in sorted(_BEAR_KW, key=len, reverse=True):
        out = out.replace(kw, f"{_ANSI['red']}{kw}{_ANSI['reset']}")
    for kw in _NEUTRAL_KW:
        out = out.replace(kw, f"{_ANSI['yellow']}{kw}{_ANSI['reset']}")

    # markdown 列表符号 - → ·
    out = re.sub(r"^(\s*)-\s+", r"\1· ", out, flags=re.MULTILINE)

    return out


def _strip_markdown(text: str) -> str:
    """给文件 log 用：去掉所有 markdown 标记但不加 ANSI。"""
    out = re.sub(r"^(#{1,4})\s*", "", text, flags=re.MULTILINE)
    out = re.sub(r"\*\*(.+?)\*\*", r"\1", out)
    out = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", out)
    out = re.sub(r"^(\s*)-\s+", r"\1· ", out, flags=re.MULTILINE)
    out = re.sub(r"^\s*\|[\s\-:|]+\|\s*$", "", out, flags=re.MULTILINE)
    return out


_JA_LANGUAGE_GUARD = """⚠ **日本語モード厳守**：
- 回答は全文を自然な日本語で書く。中国語の簡体字・中国語市場用語を混ぜない。
- 見出しも本文も、以下の用語に統一する：
  - 夜間の先物取引：ナイトセッション
  - 寄付前の時間外取引：プレマーケット
  - 引け後の時間外取引：アフターマーケット
  - 通常取引時間：レギュラーセッション
  - 当日の最初の取引：寄付き
  - 寄付きが高い/安い：高寄り / 安寄り
  - ギャップを伴う寄付き：ギャップ高寄り / ギャップ安寄り
  - 事前シナリオ：見通し
- たとえ log 内に中国語ラベルが残っていても、回答では上記の日本語に言い換える。"""

_JA_TERM_REPLACEMENTS = [
    ("跳空高开", "ギャップ高寄り"),
    ("跳空高開", "ギャップ高寄り"),
    ("跳空低开", "ギャップ安寄り"),
    ("跳空低開", "ギャップ安寄り"),
    ("高开冲高回落", "高寄り後に上昇して失速"),
    ("高開後に上昇して失速", "高寄り後に上昇して失速"),
    ("开盘前", "寄付前"),
    ("開盤前", "寄付前"),
    ("开盘后", "寄付後"),
    ("開盤後", "寄付後"),
    ("开盘", "寄付き"),
    ("開盤", "寄付き"),
    ("高开", "高寄り"),
    ("高開", "高寄り"),
    ("低开", "安寄り"),
    ("低開", "安寄り"),
    ("盘前", "プレマーケット"),
    ("盤前", "プレマーケット"),
    ("盘后", "アフターマーケット"),
    ("盤後", "アフターマーケット"),
    ("盘中", "レギュラーセッション"),
    ("盤中", "レギュラーセッション"),
    ("夜盘期货", "ナイトセッション先物"),
    ("夜盤先物", "ナイトセッション先物"),
    ("夜盘先物", "ナイトセッション先物"),
    ("夜盘", "ナイトセッション"),
    ("夜盤", "ナイトセッション"),
    ("预演", "見通し"),
    ("予演", "見通し"),
    ("先回り買いが鮮明", "先回り買いが目立つ"),
    ("完全に反转", "完全に反転"),
    ("完全に反転", "はっきり反転"),
    ("承压", "上値が重い"),
    ("回吐", "上げ幅を吐き出す"),
    ("冲高", "上昇"),
    ("突破", "上抜け"),
    ("上涨", "上昇"),
    ("下跌", "下落"),
    ("暴跌", "急落"),
    ("数据源", "データソース"),
    ("解读", "解説"),
    ("早盘策略", "寄付戦略"),
    ("短線警示", "短期警告"),
    ("短線参考", "短期参考"),
    ("技术形态胜率排行", "テクニカル形態勝率ランキング"),
    ("今日触发规则", "本日発火ルール"),
    ("高胜率规则", "高勝率ルール"),
    ("胜率排行", "勝率ランキング"),
    ("测试胜率", "テスト勝率"),
    ("训练胜率", "訓練勝率"),
    ("胜率", "勝率"),
    ("回测", "バックテスト"),
    ("规则", "ルール"),
    ("触发", "発火"),
    ("样本", "サンプル"),
    ("平均收益", "平均リターン"),
    ("持仓", "保有"),
    ("今日尚未寄付き", "本日はまだ寄付いていない"),
    ("距 9:30 ET 还有", "9:30 ET まで残り"),
    ("月增", "前月比"),
    ("同比", "前年比"),
    ("上月新增", "前月増加"),
    ("上月", "前月"),
    ("近3月均", "直近3カ月平均"),
    ("当前利率", "現在金利"),
    ("延迟", "遅延"),
    ("进行中", "進行中"),
    ("已结束", "終了"),
    ("已收", "終了"),
    ("待开始", "開始前"),
    ("未开始", "開始前"),
]


def _is_ja_mode() -> bool:
    import os
    return os.environ.get("OUTPUT_LANG", "").lower() == "ja"


def _sanitize_ja_text(text: str) -> str:
    """
    Japanese mode is fed by Chinese-heavy system logs, so keep the AI prompt
    and final answer from leaking Chinese trading terms such as "夜盘".
    """
    out = text
    for src, dst in _JA_TERM_REPLACEMENTS:
        out = out.replace(src, dst)
    return out


def print_analysis(text: str, log_path: str | Path | None = None,
                   provider: str = "Codex") -> None:
    """
    打印 AI 分析结果：
      - 控制台：带 ANSI 颜色，markdown 符号去掉
      - 文件：纯文本去 markdown（避免控制台 ANSI 转义码污染日志文件）
    """
    if _is_ja_mode():
        text = _sanitize_ja_text(text)
    sys.stdout.write(_colorize_markdown(text) + "\n")
    sys.stdout.flush()
    if log_path:
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as f:
                label = f"{provider} 出力" if _is_ja_mode() else f"{provider} 输出"
                f.write(f"{ts} ----- {label} -----\n")
                f.write(_strip_markdown(text) + "\n")
        except Exception:
            pass


# ── 标的描述（动态从 config.TICKERS 渲染，避免硬编码） ──────────────────────
_TICKER_DESC_ZH = {
    "US.TQQQ": "TQQQ：3 倍做多纳斯达克 100 指数 ETF（科技股杠杆）",
    "US.SOXL": "SOXL：3 倍做多半导体板块 ETF（NVDA / AMD / Intel 等）",
    "US.GLD":  "GLD：黄金 ETF（避险资产）",
    "US.DRAM": "DRAM：Roundhill Memory ETF（存储芯片板块，含 Micron / SK Hynix / Samsung 暴露）",
    "US.MULL": "MULL：2 倍做多 Micron（DRAM/NAND 龙头单股杠杆 ETF）",
    "US.MU":   "MU：Micron（DRAM/NAND 龙头单股，1x）",
}

_TICKER_DESC_JA = {
    "US.TQQQ": "TQQQ：ナスダック100指数の3倍ロングETF（テクノロジー株レバレッジ）",
    "US.SOXL": "SOXL：半導体セクターの3倍ロングETF（NVDA / AMD / Intel など）",
    "US.GLD":  "GLD：金ETF（リスクオフ資産）",
    "US.DRAM": "DRAM：Roundhill Memory ETF（メモリ半導体セクター、Micron / SK Hynix / Samsung エクスポージャー）",
    "US.MULL": "MULL：Micron 2倍ロングETF（DRAM/NAND 大手の単銘柄レバレッジ）",
    "US.MU":   "MU：Micron（DRAM/NAND 大手、1x）",
}


def _render_tickers_block(ja_mode: bool = False) -> str:
    """从 config.TICKERS 渲染 prompt 用的标的列表。GLD 始终包含（黄金通过 _gold_rules）"""
    from config import TICKERS
    GOLD = "US.GLD"
    full_list = list(TICKERS)
    if GOLD not in full_list:
        full_list.append(GOLD)
    desc_map = _TICKER_DESC_JA if ja_mode else _TICKER_DESC_ZH
    return "\n".join(f"  - {desc_map.get(tk, tk)}" for tk in full_list)


def _ticker_short_names() -> list[str]:
    """返回标的简称列表（如 ['TQQQ','SOXL','DRAM','MULL','GLD']），供 prompt 提示词替换。"""
    from config import TICKERS
    GOLD = "US.GLD"
    full_list = list(TICKERS)
    if GOLD not in full_list:
        full_list.append(GOLD)
    return [tk.split(".")[-1] for tk in full_list]


# ── 日本語テンプレート（環境変数 OUTPUT_LANG=ja で有効化）─────────────────────
_PROMPTS_JA = {
    "review": """# ETF トレード復盤 — 本日の引け後まとめをお願いします

本日の日付: {date}
取引銘柄（全て個別に分析してください）:
{tickers}

私は**初級から中級の個人投資家**です。専門用語を並べるのではなく、**分かりやすい日本語**で説明してください。

⚠ **時間ルール（必読、日付を勝手に推測しない）**：
log の各セッション・データには `[2026-05-18 昨日アフター(終了)]` のような角括弧プレフィックスが付いています。

**完全なタイムライン（時系列順に読む）**：
1. **プレマーケット** 4:00-9:30 ET：寄付前の時間外取引
2. **レギュラーセッション（通常取引）** 9:30-16:00 ET：**本日の主戦場、最重要！**
3. **アフターマーケット** 16:00-20:00 ET：引け後の時間外取引
4. **ナイトセッション** 20:00-翌4:00 ET：夜間の先物取引

⚠ 角括弧の日付を厳密に解釈：
- 「昨日プレ/レギュラー/アフター」= **昨日**の出来事（過去）
- 「本日プレ/レギュラー」= **今日**の最新情報
- 昨日のアフターを「今日のアフター」と誤読しない
- 復盤を書く際はレギュラーセッション（最大変動）から書き出す

⚠ **未開場の禁止事項**：
- log に「本日まだ開場していない」と表示されていたら、**「本日 $X.XX で開いた」「本日 $Y.YY まで下げた」と書いてはいけない**
- 復盤では既に発生したことだけを論じる：昨日の4セッション + ナイトセッション
- 本日の予測は、プレマーケット/ナイトの実データのみに基づき、寄付値を捏造しない

⚠ **データ鮮度を最優先**（K線タイムスタンプを先に確認）：
- log 内の `最新K線@` または `[最新15m K線@` を検索
- タイムスタンプが古ければ素直に「最新Kがまだ無いので判断不可」と言う

---

以下は本日システムが出力した完全な log です：

```
{log}
```

---

**500-700字**で回答してください（初級投資家向け、**省略禁止**）：

1. **本日の全体相場の流れ**（ストーリー仕立てで一段落）
   - **全銘柄**（{tickers_short}）それぞれの騰落率、どの時間帯が最大変動だったか
   - 主因は何か（イベント？資金フロー？セクター連動？）

2. **各ETFのコア結論**（1銘柄ずつ一段落）
   - 「明確な強気/弱気/レンジ様子見」のどれか
   - 主要指標の解釈（数値 + 意味、例「RSI 71 は買われ過ぎ気味だが78超の極端水準ではない」）
   - 「RSI 超買」「CCI 超売」のように説明なしの専門用語は禁止

3. **短期トレード機会（15分足視点）**
   - log に短期警告・短期参考の記載があるか？
   - どのETFが1-4時間の短期トレードに適するか（ロング or ショート）？理由は？

4. **明日のトレードプラン**
   - 注目イベント（具体的な日付 + データ名 + 予想値）
   - 主要価格水準（各ETFのサポート/レジスタンス、理由付き）
   - 加減ポジションのトリガー条件（「Xが Yを下回ったらZ」形式）

5. **リスクポイント + 矛盾シグナル**
   - 競合するシグナルはあるか？（例：「日足は買われ過ぎだが15分は金叉」）
   - 過熱で警戒すべきもの、過冷で反発の可能性があるものは？

6. **総合観点（一言まとめ）**
   - 現在の地合いは「強気継続 / レンジ / 弱気転換」のどれか？

⚠ **根拠タグ規則（重要）**：
各結論・提案・価格水準の末尾に、参照したシステム block を角括弧で明記すること。
**block 番号**：
  ① 基礎レポート  ② 固定テクニカル形態の勝率  ③ シグナル実況(共振)
  ④ イベント・カレンダー  ⑤ Trump signal  ⑥ オプション・ウォール
  ⑦ MACD+ADX  ⑧ SOX PCA
**書式**：
- 単一根拠：`[根拠: ③RSI 51 中立]`
- 複数根拠：`[根拠: ③共振 5多 + ⑥Call Wall $82 + ⑧SOX MKT +4.06%]`
- 矛盾根拠：`[根拠: ③共振 強気 vs ⑤Trump GEOPOLITICAL 弱気]`
- 根拠なし（純推論）：`[根拠: 推論]`

⚠ **フォーマット要件**：
- **markdown テーブル禁止** `|...|...|`（CMD ウィンドウで崩れる）— 箇条書きや番号付きリストを使う
- 数値には符号を付ける（`+0.45%`、`-2.31%`）
- 各提案には「なぜ」を明記し、初級投資家が理解できるように
- 日本語、口語的に
""",

    "morning": """# ETF 寄付き戦略 — 寄付き後1時間の準備をお願いします

本日の日付: {date}
取引銘柄（全て個別に分析してください）:
{tickers}

私は**初級から中級の個人投資家**です。**分かりやすく、やや詳しめ**の日本語で。略語や専門用語の連発は避けてください。

⚠ **時間ルール（重要）**：
log の各セッション・データには `[2026-05-18 昨日アフター(終了)]` のような角括弧プレフィックスが付いています。

**完全なタイムライン**：
1. **プレマーケット** 4:00-9:30 ET：寄付前の時間外取引
2. **レギュラーセッション** 9:30-16:00 ET：**本日の主戦場、最重要！**
3. **アフターマーケット** 16:00-20:00 ET：引け後の時間外取引
4. **ナイトセッション** 20:00-翌4:00 ET：夜間の先物

⚠ 角括弧の日付を厳密に：
- 「昨日XXX」は**昨日**の事象（過去）
- 「本日プレ/ナイト」が**今日**の最新データ
- 昨日のアフターを「今日のアフター」と誤読しない

⚠ **未開場の禁止事項**：
- log に「本日まだ開場していない」と表示されていたら、**「本日 $X.XX で開いた」と書いてはいけない**
- このとき有効な「本日」データは：本日プレ / 進行中のナイト / 先物リアルタイム価のみ
- 寄付値を予測しない。「プレが $X、寄付ギャップ +Y% の可能性」と言うに留める

⚠ **時区規約（厳格）**：
- log 内**全ての**日付ラベル（[YYYY-MM-DD] 前置、「本日プレ/アフター/ナイト」等）は
  **ET（米東部）時区**で記載。回答時の「今日」= ET 日付、**JST や現地時間で推測しない**
- 例：log に `[2026-06-10] 本日プレマーケット(終了)` とあれば「ET 6-10 の 4:00-9:30 ET
  プレが終了」の意。これが ET 視点の「今日」、決して「昨日」と読まない
- log 末尾の「AI Brief」block に ET ↔ JST 双時区表記あり、まずそこを確認

⚠ **必読：先行指標**（log 末尾の「AI Brief」block）：
- ナイトセッション先物 NQ/ES/GC（先行 8-12h）、15分足ショートタームスキャン、
  Trump 強シグナル verbatim 等を含む
- **必ず読んで**から #2 (ナイト+プレ予演) と #4 (短期トレード機会) に答える
- AI Brief が「データ不可」を示している項目は、回答でも素直に「該当データなし」と書く、
  推測で埋めない

⚠ **データ鮮度優先**（先にK線タイムスタンプを確認）：
- すべての結論は log 内の**最新K線タイムスタンプ**に基づく
- プレマーケット中なら 15分足はプレ bar を含むはず（extended_time 有効）
- データが揃わない場合は「現状 snapshot のみで、プレK未着」と素直に言う

---

以下は寄付前スキャンの完全 log（日足 + 15分足 + プレ/アフター/ナイト + ナイト先物 + AI Brief 末尾サマリー）：

```
{log}
```

---

**700-1000字**で回答してください（初級投資家向け、**省略しすぎず実戦寄り**）：

1. **全銘柄の現状**（{tickers_short}、1銘柄2-3行ずつ、**省略しない**）
   - 現在価格 + 本日騰落 + 52週高値からの距離
   - 主要指標の人間語解釈（例「RSIは買い圧と売り圧の強弱を測る指標で、0-100。70超えで短期的に買われ過ぎ、反落しやすい」）
   - 指標名だけでなく、数値とその「何を物語るか」を述べる

2. **ナイト+プレ予演**（このパートが最重要）
   - NQ/ES/GC など先物の動きは何を示唆するか？
   - プレマーケットの騰落は寄付ギャップ（高寄り/安寄り）を示唆するか？
   - **log の角括弧日付を厳密に読み、昨日/本日を混同しない**

3. **本日の重要イベント**
   - 本日発表されるデータは？予想値は？前回値は？
   - 5日以内の予定イベントで要注意のものは？

4. **短期トレード機会（15分足視点）**
   - log の短期警告・短期参考の内容は？
   - どのETFが1-4時間の短期トレードに適する？ロング/ショート？理由は？
   - エントリー価格、損切り価格、利確目標（数値を明確に）

5. **固定テクニカルルールのバックテスト**
   - log の「テクニカル形態勝率ランキング」または「本日発火ルール」から、今日の局面に関係する高勝率ルールを引用する
   - ルール名、方向（買い/利確/警戒）、テスト勝率、サンプル数N、平均リターンがあれば必ず書く
   - 目安：勝率65%以上、または勝率55%以上かつサンプル数N>=3を優先
   - 高勝率ルールが発火していない場合は「本日は高勝率ルールの発火なし」と明記し、無理に推奨しない
   - 対象は固定された説明可能なテクニカルルールのみ。自動進化・ランダム交叉ルールは使用しない
   - バックテストは保証ではなく、共振・イベント・プレマーケットと合わせて使う、と一言添える

6. **執行プラン**（2層に分けて）
   - **日足（中長期）層**：様子見か、押し目待ちか、即エントリーか？理由は？
   - **15分足（短期）層**：実行可能なシグナルはあるか？ポジションサイズは？

⚠ **根拠タグ規則（重要）**：
各結論・提案・価格水準の末尾に、参照したシステム block を角括弧で明記すること。
**block 番号**：
  ① 基礎レポート  ② 固定テクニカル形態の勝率  ③ シグナル実況(共振)
  ④ イベント・カレンダー  ⑤ Trump signal  ⑥ オプション・ウォール
  ⑦ MACD+ADX  ⑧ SOX PCA
**書式**：
- 単一根拠：`[根拠: ③PSAR 弱気転換]`
- 複数根拠：`[根拠: ③PSAR 弱気 + ⑥Put Wall $72 + ⑤Trump 弱気]`
- 矛盾根拠：`[根拠: ③共振 強気 vs ⑤Trump 地政学 弱気]`
- 純推論：`[根拠: 推論]`

⚠ **フォーマット要件**：
- **markdown テーブル禁止** `|...|...|` — 番号付きリストや箇条書き
- 数値に符号（`+0.45%`、`-2.31%`）
- 各提案の「なぜ」を明確に
- 日本語、口語的に自然な文体で
""",

    "deep": """# 単一銘柄ディープ分析 — 推論をお願いします

本日の日付: {date}
私は初級から中級の投資家です。**分かりやすい日本語**で、専門用語の羅列は避けてください。

⚠ **時間ルール**：log のセッション・データには `[YYYY-MM-DD]` プレフィックス付き。日付を厳密に解釈、勝手に推測しない。

以下は本日 log（テクニカル指標、共振、ルール発火、イベント・カレンダー、ナイト先物含む）：

```
{log}
```

{tickers_short} のうち**最もシグナルが明確な1銘柄**を選び、500-700字で深堀推論してください：

1. **なぜこの銘柄を選んだか**：他の2銘柄を選ばない理由を明示（シグナル矛盾？方向感なし？過熱？）
2. **強気/弱気の総合判断**：テクニカル+マクロ+イベントの3層各々のスコアと、最終的な方向観
3. **サポート/レジスタンス**：各価格水準の根拠（MA20？BB下バンド？52週安値？出来高密集帯？）
4. **完全なエントリー戦略**：
   - エントリートリガー条件（具体的な価格 + 確認シグナル）
   - 損切り（具体的な数値 + その水準を選んだ理由）
   - 利確目標（保守的 / 攻撃的 各1つ）
   - 想定保有期間
5. **主要リスクポイント**：どのイベント/価格水準が結論を覆すか？
6. **3-5日後のシナリオ推論**：強気/弱気/レンジの確率は各どれくらい？理由は？

⚠ **根拠タグ規則（重要）**：
各結論・価格水準・確率の末尾に、参照したシステム block を角括弧で明記。
**block 番号**：① 基礎レポート ② 固定テクニカル形態の勝率 ③ シグナル実況(共振)
              ④ イベント・カレンダー ⑤ Trump signal ⑥ オプション・ウォール
              ⑦ MACD+ADX ⑧ SOX PCA
**書式**：`[根拠: ③PSAR 弱気 + ⑥Put Wall $72]` / 矛盾は `vs` / 純推論は `[根拠: 推論]`

⚠ **フォーマット要件**：
- markdown テーブル禁止（CMD で崩れる）
- 番号付きリスト + 箇条書き
- 数値に符号付き
- 日本語、口語的
""",
}


# ── 不同场景的提问模板 ────────────────────────────────────────────────────────
_PROMPTS = {
    "review": """# ETF 交易复盘 — 请帮我总结今日盘后情况

今天日期: {date}
我交易的标的（**全部**单独分析，不要只挑几个）:
{tickers}

我是一个**初级到中级**的个人投资者，希望你用**通俗易懂的语言**讲清楚情况，不要堆术语。

⚠ **时间约定（非常重要，不要自己猜日期）**：
log 里每个时段数据都带方括号前缀，例如 `[2026-05-18 昨日盘后(已结束)]`。

**完整时间链路（按时间顺序读）**：
1. **盘前** 4:00-9:30 ET：开盘前的延长交易
2. **盘中** 9:30-16:00 ET：**正式交易时段，全天最重要！**（之前 log 漏掉了这段，现已补回）
3. **盘后** 16:00-20:00 ET：收盘后延长交易
4. **夜盘** 20:00-次日4:00 ET：跨夜的期货延长交易

⚠ 严格按方括号日期解读：
- 「昨日盘前/盘中/盘后」都是**昨天**发生的事（已过去）
- 「今日盘前/盘中」才是**今天**的最新数据
- 不要把昨日盘后误认为"今天的盘后"
- 写复盘时，从盘中（最大波动）讲起，盘前/盘后是"补充情境"

⚠ **未开盘的禁区**：
- 若 log 显示「今日尚未开盘」，**绝对不要写"今日开盘 $X.XX"或"今日跌到 $Y.YY"**
- 复盘只讨论已发生的：昨日完整四时段 + 夜盘
- 今日预演只基于盘前/夜盘的实际数据，不要捏造开盘价

---

以下是今日系统输出的完整 log：

```
{log}
```

---

请用 **500-700 字**回答（面向初级投资者，**不要省略**）：

1. **今日整体行情怎么走的**（一段话讲故事）
   - **全部标的**（{tickers_short}）各自涨跌幅、什么时段动得最厉害
   - 主要驱动因素是什么（事件？资金流向？板块联动？）

2. **每只 ETF 的核心结论**（每只一段）
   - 用人话说：当前是「明显偏多/偏空/震荡观望」哪一种？
   - 关键技术指标的解读（说出数值 + 含义，例如 "RSI 71 表示已经偏热，但还没到 78 极度超买"）
   - 别只写「RSI 超买」「CCI 超卖」这种没头没尾的术语

3. **短线机会（15 分钟 K 视角）**
   - log 里有没有"短线警示"或"短线参考"？
   - 哪只 ETF 适合 1-4 小时的短线（做多还是做空）？理由是什么？
   - 如果都没机会，直接说"今天没短线信号"

4. **明天的交易计划**
   - 关注的事件（具体日期 + 数据名称 + 预期值）
   - 关键价位（每只 ETF 的支撑/阻力位，并解释为什么是这个位）
   - 加仓/减仓的触发条件（"如果 X 跌破 Y 就 Z"这种格式）

5. **风险点 + 矛盾信号**
   - 系统里有没有相互打架的信号？（例如「日K 超买但 15m 金叉」）
   - 哪些是过热需要警惕的，哪些是过冷可能反弹的？

6. **整体观点（一句话总结）**
   - 当前市场是「牛市延续 / 震荡盘整 / 转空开始」哪一种？

⚠ **依据标注规则（重要）**：
每个具体结论 / 建议 / 价位都必须在末尾用方括号标注引用了哪些系统模块（block）。
**系统 block 编号**：
  ① 基础报告  ② 固定技术形态胜率  ③ 信号实况(共振)
  ④ 事件日历  ⑤ Trump signal  ⑥ 期权墙  ⑦ MACD+ADX  ⑧ SOX PCA
**标注格式**：
- 单依据：`[依据: ③RSI 51 中性]`
- 多依据：`[依据: ③共振 5多 + ⑥Call Wall $82 + ⑧SOX MKT +4.06%]`
- 矛盾依据：`[依据: ③共振偏多 vs ⑤Trump GEOPOLITICAL bearish]`
- 纯推理无 block 支持：`[依据: 推断]`

⚠ **格式要求**：
- **不要用 markdown 表格** `|...|...|`（在命令行窗口显示会乱），用项目符号或编号列表
- 数字带正负号（`+0.45%`、`-2.31%`），避免歧义
- 每个建议都说清楚"为什么"，初级投资者看了能理解
- 中文，口语化
""",

    "morning": """# ETF 早盘策略 — 请帮我准备开盘后第一小时

今天日期: {date}
我交易的标的（**全部**单独分析，不要只挑几个）:
{tickers}

我是一个**初级到中级**的个人投资者，请用**通俗易懂、详细一些**的语言，不要太多缩略和术语。

⚠ **时间约定（重要）**：
log 里每个时段数据都带方括号前缀，例如 `[2026-05-18 昨日盘后(已结束)]`。

**完整时间链路（按时间顺序读，每天 24h 的市场活动）**：
1. **盘前** 4:00-9:30 ET：开盘前的延长交易
2. **盘中** 9:30-16:00 ET：**正式交易时段，全天最重要！数据量最大、价格最权威**
3. **盘后** 16:00-20:00 ET：收盘后延长交易
4. **夜盘** 20:00-次日4:00 ET：跨夜的期货延长交易

⚠ 严格按方括号日期解读：
- 「昨日盘前/盘中/盘后」是**昨天**整天发生的事（已过去）
- 「今日盘前/盘中」才是**今天**的最新信息
- 不要把昨日盘后当成"今天的盘后"
- 盘中是核心——很多情况下盘前+盘后+夜盘加起来的波动还不如盘中一小时

⚠ **未开盘的禁区（绝对不要捏造）**：
- 若 log 显示「[今日盘中] 今日尚未开盘（距 9:30 ET 还有 Xh）」，**就不要说"今日开 $X.XX"或"今日跌到 $Y.YY"**——今天根本没开盘
- 不要把 moomoo 返回的 open_price 当成"今日开盘价"——开盘前它是昨日的数据
- 此时唯一有效的"今日"数据是：今日盘前 / 夜盘正在进行 / 期货实时价
- 不要预测今日开盘价，只能说"盘前显示 $X，开盘可能跳空高开/低开 Y%"

⚠ **时区约定（严格）**：
- log 里**所有**日期标签（含 [YYYY-MM-DD] 前缀、"今日盘前/今日盘后/今晚夜盘"等）都按
  **ET (美东) 时区**标。你的"今天"= ET 日期，**不要按 JST 或本地时区推断**
- 例：如果 log 里写 `[2026-06-10] 今日盘前(已结束)`，意思是「ET 6-10 那天 4:00-9:30 ET
  的盘前已结束」，**这就是 ET 视角的"今天"**，不要把它当成 "昨天"
- log 末尾有「AI Brief」块明示 ET ↔ JST 双时区对照，先看那里再判断

⚠ **必读领先指标**（log 末尾的 「AI Brief」块）：
- 含夜盘期货 NQ/ES/GC（领先 8-12h）、15min 短线扫描、Trump 强信号 verbatim
- **必读** 这一块再回答 #2 短线机会和 #4 执行建议
- 如果 AI Brief 显示某项"数据不可用"，回答时如实说"此项暂无数据"，不要捏造

⚠ **数据时效性优先**（先看 K 线时间戳再分析）：
- 所有结论都要基于 log 里**最新的 K 线时间戳**（搜 `最新K线@` 或 `[最新15m K线@`）
- 如果 ET 当前是盘前（4:00-9:30 ET），log 里 15min K 应该包含盘前 bar（已开启 extended_time）
- 如果 ET 当前是盘后（16:00-20:00 ET），看盘后最新 bar
- 如果 ET 当前是夜盘（20:00-次日4:00 ET），15min K 通常没有 bar；用盘后最新 bar + 夜盘期货（NQ/ES）
- 数据不全时**直接说"X 数据不可用"**，不要捏造

---

以下是今天开盘前扫描的完整 log（含日K + 15min + 盘前/盘后/夜盘 + 夜盘期货 + AI Brief 末尾汇总）：

```
{log}
```

---

请用 **700-1000 字**回答（面向初级投资者，**不要过度压缩，要偏实战**）：

1. **全部标的现状**（{tickers_short}，每只一段，2-3 行，**不要省略**）
   - 当前价格 + 当日涨跌 + 距 52 周高点距离
   - 关键技术指标的人话解读（例：「RSI 是衡量买卖力量强弱的指标，0-100，超过 70 就说明短期买得太凶了，容易回调」）
   - 不要光说技术指标名字，给出数值和"它告诉我们什么"

2. **昨夜+盘前预演**（这段最关键）
   - NQ/ES/GC 等期货走势说明什么？
   - 盘前涨跌是否暗示开盘会跳空高开/低开？
   - 严格按 log 里的日期标签解读，不要混淆昨日/今日

3. **今日重大事件**
   - 今天有什么数据要发布？预期值是多少？上月是多少？
   - 即将发布的事件（5 天内）需要注意吗？

4. **短线机会（15 分钟 K 视角）**
   - log 里"短线警示"或"短线参考"是什么内容？
   - 哪只 ETF 适合 1-4 小时短线？做多还是做空？为什么？
   - 入场价位、止损价位、目标价位（必须明确数字）

5. **固定技术规则回测**
   - 从 log 的「技术形态胜率排行」或「今日触发规则」里，引用今天相关的高胜率规则
   - 必须写清：规则名、方向（买入/减仓/警戒）、测试胜率、样本数N、如有平均收益也写上
   - 优先引用：胜率≥65%，或胜率≥55% 且样本数N>=3 的规则
   - 如果今天没有高胜率规则触发，要明确说「今天没有高胜率规则触发」，不要硬凑结论
   - 只允许引用固定、可解释的技术规则；系统不再使用自动进化或随机交叉规则
   - 提醒：回测不是保证，只能和共振、事件、盘前走势一起判断

6. **执行建议**（分两层）
   - **日K 长线层**：今天该观望、等回踩、还是直接入场？为什么？
   - **15min 短线层**：是否有可操作信号？仓位多少？
   - **若 log 里某只 ETF 出现 `WATCH_BUY_LONG_HOLD` action**（V 反弹长持仓提示）：
     · 这是杠杆 ETF (3x) 出现 V 反弹时的特殊信号，**系统不会自动下单**
     · 历史回测：3x ETF V 反弹日 5d hit 仅 33%（67% 概率 5 天内继续亏），但 20d hit 58% / avg +28%
     · 你必须明示：这是"长持仓机会，不适合短线追"，建议持 ≥ 20d 且严格止损
     · 不要把它当成常规 WATCH_BUY 推荐用户当日入场

⚠ **依据标注规则（重要）**：
每个具体结论 / 建议 / 价位都必须在末尾用方括号标注引用了哪些系统模块（block）。
**系统 block 编号**：
  ① 基础报告  ② 固定技术形态胜率  ③ 信号实况(共振)
  ④ 事件日历  ⑤ Trump signal  ⑥ 期权墙  ⑦ MACD+ADX  ⑧ SOX PCA
  ⑨ 黄金宏观 (real_rate / DXY / WALCL / FOMC / 10Y / 油价)
  ⑩ 期权风险（三巫日 / GEX / Gamma 挤压）— 来自 events.options_risk
  ⑪ JP 博主推荐 — 日股 YouTuber 推荐标的，含星标 / 看好逻辑 / 历史胜率
**标注格式**：
- 单依据：`[依据: ③PSAR 转空]`
- 多依据：`[依据: ③PSAR 转空 + ⑥Put Wall $72 + ⑤Trump bearish]`
- 矛盾依据：`[依据: ③共振 5多0空 vs ⑤Trump GEOPOLITICAL bearish]`
- 纯推理无 block 支持：`[依据: 推断]`
例句：「TQQQ 短线偏空 [依据: ③PSAR 转空 + ⑤Trump GEOPOLITICAL large bearish + ⑥Put Wall $72]」
**⑨ 黄金宏观特别说明**（对 GLD 决策）：
- 系统不再用 keyword 关键词推断黄金 bias，而是用 6 因子加权聚合（实际利率最重）
- 回测发现宏观直接驱动决策会退化 → 系统决策仍以技术面为主
- 你看到 ⑨ banner 时，**用宏观信号作为"基本面背景"**给用户解读"宏观 vs 技术分歧"
- 例：「GLD 当前宏观转 bullish（real_rate 低 + Fed 扩表 + DXY 平），但技术 PSAR 仍空头 + MA 空排 — 等技术确认（PSAR 翻多 + 收回 MA20）再考虑入场 [依据: ⑨real_rate 2.16 mid + WALCL 扩表 vs ③PSAR 空 + MA 空排]」

**⑪ JP 博主推荐特别说明**（日股 YouTuber 跟踪）：
- 跟踪 higedura24 / SHO1112 / LA_Banker / NaNaShuoMeiGu / RhinoFinance 等日语博主
- 每只标的有星标（按"提及次数 × 涉及创作者数"）：
  · ★★★★★：≥3 创作者 OR ≥5 mentions（最高共识）
  · ★★★★：2 创作者 + ≥3 mentions
  · ★★★：2 创作者 OR ≥3 mentions
  · ★★：≥2 mentions
  · ★：单博主单次
- 每只标的附"逻辑"（thesis）+ "风险" + 时间维度
- "回测命中"按 1d/3d/5d/20d/60d 分档（yfinance 算的 hit rate）
- "博主历史胜率"显示创作者过去所有推荐的总体准确率
- **特别强调**：≥★★★ 的标的（多博主共识）必须列入早盘可参考清单，给具体看好逻辑
  + 创作者胜率 + 回测命中。≤★★ 的提及一笔带过即可
- 日股代码 JP.XXXX，需要 moomoo OpenD 日股报价（如未订阅则仅参考）
- 例：「JP.7203 丰田 ★★★★ 多头共识 [依据: ⑪higedura24+SHO1112 共 4 mentions / 历史 5d 67% / 20d 71% / 逻辑: 円安 + 出来高强 + 个股盈利预期上调]」
- events.options_risk 含三巫日识别 (phase: today/adjacent/approaching/far) + 每只标的 GEX 代理方向
- **三巫日（每季 3/6/9/12 月第三个周五）gamma 集中到期** — Claude 必须明示
- GEX 方向解读：
  · `positive_pin`：dealer 短 gamma 在上方 → 价格被压制 pin to max pain（震荡）
  · `negative_squeeze`：dealer 长 gamma 在下方 → 易突破 → **squeeze 风险，方向加速**
- 用户做日 K 长线时，三巫日前一天不建议重仓入场（gamma 抽尽风险）；squeeze 信号时如果方向已对则减小止损但留仓
- 例：「明日三巫日 + SOXL GEX negative_squeeze → 警惕下方突破后 gamma 加速 [依据: ⑪三巫日 adjacent + GEX strong negative]」

⚠ **结构化目标输出（system 自动解析，必须在答复末尾附）**：
分析完成后，**必须**在文末用 ```json``` 围栏输出每只标的的目标位 JSON，供 paper_trader
解析（系统会按 entry_ref 挂 GTC 限价单，按 stop_ref 挂 broker SELL STOP）。

输出格式（标的全部列出，含**不建议入场**也要列）：
```json
{{
  "version": 1,
  "ts": "YYYY-MM-DDTHH:MM",
  "targets": [
    {{
      "ticker": "TQQQ",
      "action": "watch_buy|hold|reduce|skip",
      "entry_ref": 80.0,        // 限价等回踩的入场价，null 表示不挂限价
      "stop_ref":  78.0,        // broker 端 SELL STOP 触发价，null 表示不挂
      "target_ref": 85.0,       // 第一目标位（参考，paper_trader 暂不用）
      "use_limit": true,        // true=挂限价等回踩，false=立即吃单
      "size_hint": "small|normal|aggressive",  // 仓位偏向（paper_trader 参考）
      "notes": "简短一句话理由"
    }},
    ...
  ]
}}
```
**规则**：
- `entry_ref`: 你说的"等回踩到 $X"价位。如果建议立即入场就给当前价，如果不建议入场给 null
- `stop_ref`: 你说的"跌破 $Y 走"价位。**必须低于 entry_ref**。无明确技术止损位给 null
- `use_limit`: 等回踩到 entry_ref 才进 = true；立即吃单 = false
- **杠杆 ETF 价格单位（强制）**：TQQQ 引用 QQQ 期权墙、SOXL 引用 SOXX/SMH 期权墙时，QQQ/SOXX/SMH 的 strike 只能作为结构触发条件；`entry_ref` / `stop_ref` / `target_ref` 必须填写日志中 `=> TQQQ≈$X` / `=> SOXL≈$X` 的换算价，严禁直接填底层 ETF 的原始 strike；若日志没有换算价，对应字段填 `null`，不能猜
- **每日重置与折损**：3x ETF 的换算是触发时的现价锚定近似。期限超过 1 天必须提示波动折损和路径依赖；挂单优先使用最新即时换算价，不把较长期到期估算当成精确成交价
- 所有数字带 2 位小数，不带 $ 符号
- 标的全部输出（包括 hold 的，方便系统对比）

⚠ **格式要求**：
- **不要用 markdown 表格** `|...|...|`（CMD 窗口显示会乱），改用编号或项目符号
- 数字带正负号（`+0.45%`、`-2.31%`）
- 解释每个建议背后的逻辑，让初级投资者能理解 why
- 中文，自然口语
- 结构化 JSON 必须用 ```json``` 围栏，便于系统提取
""",

    "deep": """# 单标的深度分析 — 请帮我深入推演

今天日期: {date}
我是初级到中级投资者，请用**通俗易懂**的语言讲清楚，避免堆砌术语。

⚠ **时间约定**：log 中扩展时段都带 `[YYYY-MM-DD]` 前缀，严格按日期解读，不要猜。

以下是今日 log（含技术指标、共振、规则触发、事件日历、夜盘期货）：

```
{log}
```

请挑出 {tickers_short} 中**信号最清晰**的一只做 500-700 字深度推演：

1. **为什么挑这只**：明确说为什么不挑另外两只（信号矛盾？无方向？过热？）
2. **多空综合判断**：技术面+宏观+事件三层各自得分，最终给出方向倾向
3. **支撑/阻力位**：每个价位说明依据（MA20？BB 下轨？52周低点？历史成交密集区？）
4. **完整入场策略**：
   - 入场触发条件（具体到价位+确认信号）
   - 止损位（具体数字 + 为什么是这个位）
   - 目标位（保守 / 激进各一个）
   - 持仓时间预期
5. **关键风险点**：哪些事件/价位会让结论翻车？
6. **未来 3-5 天情景推演**：牛/熊/震荡三种走势分别多少概率？为什么？

⚠ **依据标注规则（重要）**：
每个具体结论 / 价位 / 概率都必须在末尾用方括号标注引用的系统模块（block）。
**block 编号**：① 基础报告 ② 固定技术形态胜率 ③ 信号实况(共振) ④ 事件日历
              ⑤ Trump signal ⑥ 期权墙 ⑦ MACD+ADX ⑧ SOX PCA
**标注格式**：`[依据: ③PSAR 转空 + ⑥Put Wall $72]` / 矛盾用 `vs` / 纯推理用 `[依据: 推断]`

⚠ **格式要求**：
- 不要用 markdown 表格（`|...|...|` 在 CMD 显示会乱）
- 用编号列表 + 项目符号
- 数字带正负号
- 中文口语化
""",
}


def _read_today_log() -> str:
    """读取当前美股交易日相关 log（UTF-8）。

    旧 logger 仍按机器本地日切文件；JST 午夜后的同一个纽约交易日可能横跨
    两个文件。因此同时读取 ET 日和本地日，既不丢盘前内容，也不丢午夜后刷新。
    """
    day_keys = [_market_date().replace("-", ""), datetime.now().strftime("%Y%m%d")]
    parts = []
    for day_key in dict.fromkeys(day_keys):
        log_path = Path(BASE_DIR) / "logs" / f"run_{day_key}.log"
        if not log_path.exists():
            continue
        try:
            parts.append(log_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
    return "\n".join(parts)


# 过滤掉的噪音行（moomoo 连接日志、TodoWrite 提示等）
_NOISE_PATTERNS = [
    re.compile(r"open_context_base\.py.*conn="),
    re.compile(r"on_disconnect:"),
    re.compile(r"_init_connect_sync:"),
    re.compile(r"_connect_sync:"),
    re.compile(r"(?:Claude|Codex)\s+(?:早盘策略|寄付戦略|复盘总结|復盤まとめ):"),
    re.compile(r"(?:调用本地|ローカル)\s+(?:Claude|Codex) CLI"),
    re.compile(r"^\s*(?:详见|詳細):\s+.*ai_analysis_"),
    re.compile(r"\| \d+ \| \d+ \|"),  # moomoo SDK log 格式
]

_AI_OUTPUT_HEADER_RE = re.compile(r"-----\s*(?:Claude|Codex)\s*(?:输出|出力)\s*-----")
_LOG_TS_SEPARATOR_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ={20,}\s*$")


def _strip_prior_ai_outputs(log_text: str) -> str:
    """Remove previous AI answers before building the next prompt."""
    out_lines = []
    skipping = False
    for line in log_text.splitlines():
        if _AI_OUTPUT_HEADER_RE.search(line):
            skipping = True
            continue
        if skipping:
            if _LOG_TS_SEPARATOR_RE.match(line):
                skipping = False
            continue
        out_lines.append(line)
    return "\n".join(out_lines)


def _denoise(log_text: str) -> str:
    """删除连接日志和 SDK 噪音，只留业务信号行。"""
    out_lines = []
    for line in log_text.splitlines():
        if any(p.search(line) for p in _NOISE_PATTERNS):
            continue
        out_lines.append(line)
    return "\n".join(out_lines)


def _trim_to_last_cycles(log_text: str, max_chars: int = 28000) -> str:
    """
    截取最后 N 字符，控制本地 CLI / 网页模型的上下文大小。
    优先从分隔符（======）切断，保持语义完整。
    """
    if len(log_text) <= max_chars:
        return log_text
    tail = log_text[-max_chars:]
    # 找第一个 ======= 分隔符，从那里开始（避免半截）
    m = re.search(r"\n=+\n", tail)
    if m:
        tail = tail[m.end():]
    return "[...前面省略...]\n" + tail


def _copy_to_clipboard(text: str) -> bool:
    """Windows 用 clip 命令，无需额外依赖。"""
    try:
        # text=True + encoding 让 stdin 走 UTF-8
        subprocess.run(["clip"], input=text, text=True,
                       encoding="utf-16le", check=True)
        return True
    except Exception:
        return False


def _read_module_accuracy() -> str:
    """读取 signals/module_accuracy.md 准确率报告，注入 AI prompt 作为参考。
    报告由 _backtest_modules_accuracy.py 生成，建议每周末更新一次。
    报告超过 14 天会标注"过期"提示，但仍注入。"""
    p = Path(SIGNALS_DIR) / "module_accuracy.md"
    if not p.exists():
        return ""
    try:
        content = p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    # 拒绝旧版含进化/quant 模块的缓存报告，防止已删除的数据重新进入 AI 上下文。
    if "quant_signal" in content or "### quant" in content or "进化规则" in content:
        return ""
    age_days = (datetime.now().timestamp() - p.stat().st_mtime) / 86400
    stale = f"\n⚠ 此报告已 {age_days:.0f} 天未更新，建议跑 backtest.bat → 选 6 刷新\n" if age_days > 14 else ""
    return content + stale


def _read_trade_postmortem() -> str:
    """读取 signals/trade_postmortem.md 交易复盘, 注入 AI prompt 作参考.
    由 _trade_postmortem.py 每周生成 (weekly.bat). 超过 14 天标 stale."""
    p = Path(SIGNALS_DIR) / "trade_postmortem.md"
    if not p.exists():
        return ""
    try:
        content = p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    if not content:
        return ""
    age_days = (datetime.now().timestamp() - p.stat().st_mtime) / 86400
    stale = f"\n⚠ 此复盘已 {age_days:.0f} 天未更新, 建议跑 weekly.bat 刷新\n" if age_days > 14 else ""
    return content + stale


def _read_manual_moomoo_ai(today: str) -> str:
    """读今日手动从 moomoo app 复制粘贴进来的 AI 回答（可选输入）。

    位置：signals/manual_moomoo_ai_YYYY-MM-DD.txt 或 .md
    内容：用户在 moomoo app 里问 AI 后，把回答粘贴到这个文件。
    没有则返回空字符串。
    """
    for ext in ("txt", "md"):
        p = Path(SIGNALS_DIR) / f"manual_moomoo_ai_{today}.{ext}"
        if p.exists():
            try:
                content = p.read_text(encoding="utf-8").strip()
                if content:
                    return content
            except Exception:
                pass
    return ""


def generate_ai_prompt(mode: str = "review") -> tuple[Path | None, bool]:
    """
    生成 AI 提问稿并复制到剪贴板。
    返回 (output_file_path, clipboard_ok)。
    """
    if mode not in _PROMPTS:
        mode = "review"

    raw_log = _read_today_log()
    if not raw_log.strip():
        return None, False

    clean_log = _strip_prior_ai_outputs(_denoise(raw_log))
    trimmed   = _trim_to_last_cycles(clean_log)

    today = _market_date()
    # OUTPUT_LANG=ja → 日本語テンプレートを使用（run_ja.bat 経由）
    import os
    ja_mode = os.environ.get("OUTPUT_LANG", "").lower() == "ja"
    templates = _PROMPTS_JA if ja_mode else _PROMPTS
    if ja_mode:
        trimmed = _sanitize_ja_text(trimmed)

    # 注入模块准确率报告（历史回测 250 天，1d/5d/10d/20d）
    # 让 AI 判断信号矛盾时知道"该信谁、信什么周期"
    module_acc = _read_module_accuracy()
    if module_acc:
        header = ("\n\n=== 模块历史准确率（历史 250 日回测，参考） ===\n"
                  if not ja_mode else
                  "\n\n=== モジュール過去精度（過去250日バックテスト） ===\n")
        trimmed = trimmed + header + module_acc + "\n=== END 模块准确率 ===\n"

    # 交易复盘 (weekly _trade_postmortem.py 生成) — 让 AI 看到实盘表现校准建议
    postmortem = _read_trade_postmortem()
    if postmortem:
        header = ("\n\n=== 近期交易复盘（weekly, 参考） ===\n"
                  if not ja_mode else
                  "\n\n=== 直近取引の振り返り (weekly) ===\n")
        trimmed = trimmed + header + postmortem + "\n=== END 交易复盘 ===\n"

    # 可选：手动 moomoo AI 输入（用户在 app 里问 AI 后粘贴的回答）
    manual_ai = _read_manual_moomoo_ai(today)
    if manual_ai:
        header = ("\n\n=== moomoo AI 视角（用户手动从 moomoo 客户端复制） ===\n"
                  if not ja_mode else
                  "\n\n=== moomoo AI 観点 (ユーザーが moomoo app からコピー) ===\n")
        trimmed = trimmed + header + manual_ai + "\n=== END moomoo AI ===\n"

    tickers_block = _render_tickers_block(ja_mode=ja_mode)
    tickers_short = " / ".join(_ticker_short_names())
    prompt = templates[mode].format(date=today, log=trimmed,
                                     tickers=tickers_block,
                                     tickers_short=tickers_short)
    if ja_mode:
        prompt = _sanitize_ja_text(f"{_JA_LANGUAGE_GUARD}\n\n{prompt}")

    # 保存到文件
    out_dir  = Path(SIGNALS_DIR)
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"ai_prompt_{mode}_{today}.md"
    out_path.write_text(prompt, encoding="utf-8")

    # 复制到剪贴板
    clip_ok = _copy_to_clipboard(prompt)
    return out_path, clip_ok


# ── 自动调用本地 AI CLI（默认 Codex；可显式切回 Claude）──────────────────────
def _hidden_cli_subprocess_kwargs() -> dict:
    """Prevent background CLI processes from opening a Windows console window."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def _find_claude_cli() -> str | None:
    """Windows: claude.exe / claude.cmd / claude；按优先级查 PATH。"""
    for name in ("claude.exe", "claude.cmd", "claude"):
        found = shutil.which(name)
        if found:
            return found
        try:
            r = subprocess.run(
                ["where", name], capture_output=True, text=True, timeout=5,
                **_hidden_cli_subprocess_kwargs(),
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip().splitlines()[0]
        except Exception:
            continue
    return None


def _find_codex_cli() -> str | None:
    """Windows: codex.exe / codex.cmd / codex；按优先级查 PATH。"""
    known = Path.home() / "AppData" / "Local" / "OpenAI" / "Codex" / "bin" / "codex.exe"
    if known.exists():
        return str(known)
    for name in ("codex.exe", "codex.cmd", "codex"):
        found = shutil.which(name)
        if found:
            return found
        try:
            r = subprocess.run(
                ["where", name], capture_output=True, text=True, timeout=5,
                **_hidden_cli_subprocess_kwargs(),
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip().splitlines()[0]
        except Exception:
            continue
    return None


def query_claude_cli(prompt: str, timeout: int = 300) -> tuple[str | None, str]:
    """
    用本地 claude CLI 非交互模式跑一次查询。
    走 Claude Code 当前登录账号（订阅 OAuth）—— 不算 API 费用。
    返回 (output, status)。status: "ok" / "not_installed" / "timeout" / "error:..."
    """
    cli_path = _find_claude_cli()
    if not cli_path:
        return None, "not_installed"
    try:
        # 用 stdin 喂 prompt 绕开 Windows 命令行长度限制（~32K）
        result = subprocess.run(
            [cli_path, "-p"],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_hidden_cli_subprocess_kwargs(),
        )
        if result.returncode == 0:
            return result.stdout.strip(), "ok"
        err = _redact_cli_text((result.stderr or result.stdout or "").strip())
        return None, f"error: exit={result.returncode} stderr={err[:500]}"
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception as e:
        return None, f"error: {_redact_cli_text(str(e))}"


def _is_claude_quota_status(status: str) -> bool:
    """Claude quota/rate-limit errors should fall back to Codex CLI."""
    s = (status or "").lower()
    quota_kw = (
        "quota",
        "hit your limit",
        "you've hit your limit",
        "your limit",
        "usage limit",
        "limit reached",
        "rate limit",
        "rate_limit",
        "too many requests",
        "429",
        "insufficient_quota",
        "insufficient credits",
        "credit balance",
        "billing",
        "exceeded",
        "resets",
    )
    return any(kw in s for kw in quota_kw)


_SECRET_TEXT_PATTERNS = (
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{12,}\b", re.I),
    re.compile(
        r"\b(OPENAI_API_KEY|CODEX_API_KEY|CODEX_ACCESS_TOKEN|ANTHROPIC_API_KEY)\s*=\s*([^\s;]+)",
        re.I,
    ),
)
_CODEX_ENV_ALLOWLIST = {
    "ALLUSERSPROFILE", "APPDATA", "CODEX_HOME", "COMSPEC", "HOME",
    "HOMEDRIVE", "HOMEPATH", "LANG", "LC_ALL", "LOCALAPPDATA",
    "NO_COLOR", "PATH", "PATHEXT", "PROGRAMDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "PROGRAMW6432", "PYTHONIOENCODING", "PYTHONUTF8",
    "SSL_CERT_DIR", "SSL_CERT_FILE", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP",
    "TERM", "TMP", "TMPDIR", "USERPROFILE", "WINDIR", "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
}


def _redact_cli_text(value: str) -> str:
    """Keep provider errors useful without ever persisting credential values."""
    text = str(value or "")
    text = _SECRET_TEXT_PATTERNS[0].sub("[REDACTED]", text)
    text = _SECRET_TEXT_PATTERNS[1].sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    return text


def _codex_safe_env() -> dict[str, str]:
    """Build a minimal environment for saved CLI auth with no application secrets."""
    return {
        name: value
        for name, value in os.environ.items()
        if name.upper() in _CODEX_ENV_ALLOWLIST
    }


# ── AI 模型分级 ────────────────────────────────────────────────────────
# 按 complexity 选 model, 简单任务 (news 解析 / regime 确认) 用便宜, 深度分析用高端.
# env var 允许 override, 默认按用户 codex CLI 全局设置 (若 CODEX_MODEL_* 空).
#   simple  → CODEX_MODEL_SIMPLE  (news_analyzer / fed_watch / policy_toolkit / jp_extractor)
#   medium  → CODEX_MODEL_MEDIUM  (claude_gate / bond_ai_interpret)
#   complex → CODEX_MODEL_COMPLEX (ai_prompt.print_analysis 主分析)
_COMPLEXITY_MODEL_ENV = {
    "simple":  "CODEX_MODEL_SIMPLE",
    "medium":  "CODEX_MODEL_MEDIUM",
    "complex": "CODEX_MODEL_COMPLEX",
}


def _resolve_codex_model(complexity: str) -> str | None:
    """按 complexity 找 model. 未设则 None (走 codex CLI 全局默认)."""
    env_key = _COMPLEXITY_MODEL_ENV.get(complexity)
    if not env_key:
        return None
    val = os.environ.get(env_key, "").strip()
    return val or None


def query_codex_cli(prompt: str, timeout: int = 300, *,
                    web_search: bool = False,
                    complexity: str = "medium") -> tuple[str | None, str]:
    """
    用 Codex CLI 的 ``codex exec`` 非交互模式跑一次查询。

    安全边界：在空临时目录、只读 sandbox、ephemeral session 中运行；不继承
    API key/token/secret/password 类环境变量，只复用本机 Codex CLI 已保存登录。

    complexity: simple / medium / complex —— 按此挑 model (需 CODEX_MODEL_* env 配置).
    """
    cli_path = _find_codex_cli()
    if not cli_path:
        return None, "codex_not_installed"

    model = _resolve_codex_model(complexity)

    try:
        with tempfile.TemporaryDirectory(prefix="codex_ai_run_") as run_dir:
            out_path = Path(run_dir) / "last_message.txt"
            prefix = (
                "以下の投資分析依頼に直接回答してください。ファイル変更やコマンド実行は不要です。"
                "最終分析本文だけを出力してください。"
                if _is_ja_mode()
                else "请直接回答下面的投资分析请求。不要修改文件，不要运行命令，只输出最终分析正文。"
            )
            codex_prompt = f"{prefix}\n\n{prompt}"
            command = [cli_path]
            if web_search:
                # Global flag: switch Codex from cached search to live search.
                command.append("--search")
            command.extend([
                    "exec",
                    "--ephemeral",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--skip-git-repo-check",
                    "--sandbox", "read-only",
                    "--cd", run_dir,
                    "--output-last-message", str(out_path),
                ])
            if model:
                command.extend(["--model", model])
            command.append("-")
            result = subprocess.run(
                command,
                input=codex_prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=run_dir,
                env=_codex_safe_env(),
                **_hidden_cli_subprocess_kwargs(),
            )
            output = ""
            try:
                output = out_path.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                output = ""
            if result.returncode == 0 and output:
                return output, "ok"
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip(), "ok"
            err = _redact_cli_text((result.stderr or result.stdout or "").strip())
            return None, f"codex_error: exit={result.returncode} stderr={err[:500]}"
    except subprocess.TimeoutExpired:
        return None, "codex_timeout"
    except Exception as e:
        return None, f"codex_error: {_redact_cli_text(str(e))}"


_AI_CLI_PROVIDERS = {"codex", "claude"}


def get_ai_cli_policy() -> dict[str, str]:
    """Return the centrally configured CLI provider order.

    ``AI_CLI_PRIMARY`` defaults to Codex. ``AI_CLI_FALLBACK`` defaults to
    ``none`` so a transient Codex failure cannot silently spend Claude quota,
    especially from the 30-minute public snapshot job.  Operators can opt in
    to the old behavior with ``AI_CLI_PRIMARY=claude`` and
    ``AI_CLI_FALLBACK=codex``, or allow exceptional Claude fallback with
    ``AI_CLI_FALLBACK=claude``.
    """
    primary = (
        os.environ.get("AI_CLI_PRIMARY")
        or os.environ.get("AI_CLI_PROVIDER")  # early compatibility name
        or "codex"
    ).strip().lower()
    fallback = os.environ.get("AI_CLI_FALLBACK", "none").strip().lower()
    if primary not in _AI_CLI_PROVIDERS:
        primary = "codex"
    if fallback not in _AI_CLI_PROVIDERS or fallback == primary:
        fallback = "none"
    return {"primary": primary, "fallback": fallback}


def _query_named_cli(provider: str, prompt: str, timeout: int,
                     web_search: bool = False,
                     complexity: str = "medium") -> tuple[str | None, str]:
    if provider == "codex":
        return query_codex_cli(prompt, timeout=timeout, web_search=web_search,
                               complexity=complexity)
    return query_claude_cli(prompt, timeout=timeout)


def query_ai_cli(
    prompt: str,
    timeout: int = 300,
    *,
    fallback_on_unavailable: bool = False,
    web_search: bool = False,
    complexity: str = "medium",
) -> tuple[str | None, str, str, str]:
    """Query the configured local CLI, Codex-first by default.

    Returns ``(output, status, provider, fallback_reason)``. Claude is never
    called by the default policy. ``fallback_on_unavailable`` remains as a
    compatibility escape hatch for callers using an older environment: when
    the primary CLI is missing and no fallback was configured, try the other
    CLI once.
    """
    policy = get_ai_cli_policy()
    primary = policy["primary"]
    fallback = policy["fallback"]

    output, status = _query_named_cli(primary, prompt, timeout, web_search, complexity)
    if output:
        return output, status, primary.title(), ""

    unavailable = status in {
        "not_installed", "claude_not_installed", "codex_not_installed",
    }
    if fallback == "none" and fallback_on_unavailable and unavailable:
        fallback = "claude" if primary == "codex" else "codex"
    if fallback == "none":
        return None, status, primary.title(), ""

    fallback_reason = f"{primary}: {_redact_cli_text(status)}"
    output, fallback_status = _query_named_cli(
        fallback, prompt, timeout, web_search, complexity
    )
    if output:
        return output, fallback_status, fallback.title(), fallback_reason
    combined = f"{fallback_reason}; {fallback}={fallback_status}"
    return None, _redact_cli_text(combined), fallback.title(), fallback_reason


def auto_analyze(mode: str = "review") -> dict:
    """
    一站式：生成 prompt → 按统一策略调用本地 AI CLI → 保存结果。
    返回 {prompt_path, analysis_path, status, output}
    """
    prompt_path, clip_ok = generate_ai_prompt(mode)
    if not prompt_path:
        return {"status": "no_log"}

    prompt = prompt_path.read_text(encoding="utf-8")
    # 主 AI 分析 (morning / review / deep) — 完整今日 log 深度推理, complex 档
    output, status, provider, fallback_reason = query_ai_cli(prompt, complexity="complex")

    today = _market_date()
    analysis_path = Path(SIGNALS_DIR) / f"ai_analysis_{mode}_{today}.md"

    if output and _is_ja_mode():
        output = _sanitize_ja_text(output)

    if output:
        analysis_path.write_text(
            f"# {provider} 分析 — {mode} — {today}\n\n{output}\n",
            encoding="utf-8")
        # 同时存一份时间戳快照，供 dashboard 展示历史（与 snap.bat 命名一致）
        try:
            ts_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            snapshot_copy = Path(SIGNALS_DIR) / f"ai_analysis_snapshot_{ts_stamp}.md"
            snapshot_copy.write_text(
                f"# {provider} 分析（orchestrator {mode}）— {ts_stamp}\n\n{output}\n",
                encoding="utf-8")
        except Exception:
            pass
        # 提取并保存 AI 输出的结构化目标 JSON（A 方案：paper_trader 消费）
        try:
            targets_path = _save_ai_targets(output, today)
        except Exception:
            targets_path = None

    return {
        "prompt_path":   prompt_path,
        "analysis_path": analysis_path if output else None,
        "targets_path":  targets_path if output else None,
        "status":        status,
        "output":        output,
        "provider":      provider,
        "fallback_reason": fallback_reason,
        "clip_ok":       clip_ok,
    }


# ── A 方案：AI 结构化目标 JSON 提取 + 保存 ───────────────────────────────
import json as _json_mod
_TARGETS_JSON_RE = re.compile(r"```json\s*(\{[\s\S]+?\})\s*```", re.MULTILINE)
_AI_TARGETS_PATH_PREFIX = "ai_targets_"


def _extract_targets_block(ai_output: str) -> dict | None:
    """从 AI 输出里抠出 ```json``` 围栏内含 'targets' 数组的 dict。"""
    if not ai_output:
        return None
    for m in _TARGETS_JSON_RE.finditer(ai_output):
        try:
            obj = _json_mod.loads(m.group(1))
        except Exception:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("targets"), list):
            return obj
    return None


def _normalize_target(t: dict) -> dict | None:
    """规范化单条 target，丢弃缺关键字段的。"""
    if not isinstance(t, dict): return None
    tk = (t.get("ticker") or "").upper().strip()
    if not tk:
        return None
    # ticker 加 "US." 前缀（统一）
    if not tk.startswith("US."):
        tk_full = f"US.{tk}"
    else:
        tk_full = tk
    action = (t.get("action") or "hold").lower().strip()
    if action not in ("watch_buy", "buy", "hold", "reduce", "sell", "skip"):
        action = "hold"

    def _num(v):
        try:
            return float(v) if v is not None else None
        except Exception:
            return None
    entry = _num(t.get("entry_ref"))
    stop = _num(t.get("stop_ref"))
    target = _num(t.get("target_ref"))
    use_limit = bool(t.get("use_limit", False)) if entry else False
    # stop 必须低于 entry（如果都有）
    if entry and stop and stop >= entry:
        stop = None  # 不合理就丢
    return {
        "ticker": tk_full,
        "action": action,
        "entry_ref": entry,
        "stop_ref": stop,
        "target_ref": target,
        "use_limit": use_limit,
        "size_hint": (t.get("size_hint") or "normal").lower().strip(),
        "notes": (t.get("notes") or "").strip()[:140],
    }


def _save_ai_targets(ai_output: str, today: str) -> Path | None:
    """从 AI 输出抠 JSON 并保存到 signals/ai_targets_<date>.json。

    paper_trader 启动时会读这个文件，按 entry_ref/stop_ref/use_limit 改下单。
    没有 JSON / 解析失败时返回 None（不影响下单，paper_trader 走默认流程）。
    """
    obj = _extract_targets_block(ai_output)
    if not obj:
        return None
    normalized = []
    for t in obj.get("targets") or []:
        nt = _normalize_target(t)
        if nt:
            normalized.append(nt)
    if not normalized:
        return None
    payload = {
        "version": obj.get("version", 1),
        "ts": obj.get("ts") or datetime.now().isoformat(),
        "source_date": today,
        "targets": normalized,
    }
    path = Path(SIGNALS_DIR) / f"{_AI_TARGETS_PATH_PREFIX}{today}.json"
    path.write_text(_json_mod.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def load_ai_target(ticker: str) -> dict | None:
    """paper_trader 用：读今日 ai_targets，返回指定 ticker 的 target dict 或 None。"""
    today = _market_date()
    path = Path(SIGNALS_DIR) / f"{_AI_TARGETS_PATH_PREFIX}{today}.json"
    if not path.exists():
        return None
    try:
        data = _json_mod.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    tk_full = ticker if ticker.startswith("US.") else f"US.{ticker}"
    for t in data.get("targets") or []:
        if t.get("ticker") == tk_full:
            return {
                **t,
                "_source_date": data.get("source_date") or today,
                "_source_ts": data.get("ts"),
                "_source_mtime": path.stat().st_mtime,
            }
    return None


def cli_main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "review"
    auto = "--auto" in sys.argv or "-a" in sys.argv

    if auto:
        print(f"[{mode}] 生成提问稿并调用 AI CLI 分析（默认 Codex，不自动回退 Claude）...")
        r = auto_analyze(mode)
        if r.get("status") == "no_log":
            print("未找到今日 log，先跑一遍 run.bat")
            return
        print(f"  提问稿: {r['prompt_path']}")
        if r["status"] == "ok":
            print(f"  使用模型CLI: {r.get('provider', 'Codex')}")
            if r.get("fallback_reason"):
                print(f"  主 CLI 不可用，已按显式策略 fallback: {r['fallback_reason']}")
            print(f"  分析结果: {r['analysis_path']}")
            print("\n" + "="*60)
            print(r["output"])
            print("="*60)
        elif str(r["status"]).endswith("not_installed"):
            print("  未找到配置的 AI CLI；提问稿已复制到剪贴板，可手动粘贴到网页")
        elif str(r["status"]).endswith("timeout"):
            print("  AI CLI 超时；提问稿可手动粘贴到网页")
        else:
            print(f"  AI CLI 调用失败: {r['status']}；提问稿可手动粘贴")
        return

    # 默认行为：只生成提问稿 + 复制到剪贴板
    path, clip_ok = generate_ai_prompt(mode)
    if path:
        print(f"AI 提问稿已生成: {path}")
        if clip_ok:
            print("已复制到剪贴板，直接 Ctrl+V 粘贴到 Claude.ai / ChatGPT 即可")
        else:
            print("剪贴板复制失败，请手动打开文件复制内容")
        print("\n提示: 加 --auto 让本地 AI CLI（默认 Codex）自动跑分析")
        print("      python ai_prompt.py review --auto")
    else:
        print("未找到今日 log，先跑一遍 run.bat")


if __name__ == "__main__":
    cli_main()
