# Datadog AI Observability 学習用ミニ・エージェントアプリ

Anthropic SDK で作った小さなエージェント(計算ツール + 疑似Web検索ツール)を、
Datadog の **AI Observability(旧 LLM Observability)** で監視するデモです。

## 仕組み

```
質問 ──> [run_agent (@agent スパン)]
            │
            ├─ Claude API 呼び出し ← ddtrace が自動計装 (LLM スパン)
            ├─ calculator / web_search 実行 (@tool スパン)
            ├─ ツール結果を添えて再度 Claude API 呼び出し
            └─ 最終回答
                        │
                        └──> トレースを ap1.datadoghq.com へ直接送信 (agentless モード)
```

- Anthropic への LLM 呼び出しは **自動計装**(プロンプト・応答・トークン数・レイテンシが記録される)
- エージェントループは `@agent`、各ツールは `@tool` デコレータで手動計装
- Datadog Agent のインストールは不要(`agentless_enabled=True` で API キーだけで送信)

## 必要なもの

1. **Anthropic API キー** — https://console.anthropic.com → Settings → API Keys
2. **Datadog API キー** — https://ap1.datadoghq.com → 左下の組織名 → Organization Settings → API Keys

## セットアップと実行

### Windows (PowerShell) の場合

```powershell
cd D:\datadog-ai-observ\datadog-agent-app
pip install -r requirements.txt

$env:ANTHROPIC_API_KEY = "sk-ant-ここにキー"
$env:DD_API_KEY        = "ここにDatadogのAPIキー"
# 以下は省略可 (デフォルト値)
# $env:DD_SITE            = "ap1.datadoghq.com"
# $env:DD_LLMOBS_ML_APP   = "mini-agent-demo"
# $env:ANTHROPIC_MODEL    = "claude-haiku-4-5"

python agent_app.py "bitbankってどんな会社? 1BTCが1200万円のとき0.025BTCは何円?"
```

`$env:` で設定した環境変数はそのPowerShellウィンドウを閉じるまで有効です(恒久設定にはなりません)。

### macOS / Linux (bash) の場合

```bash
pip install -r requirements.txt

export ANTHROPIC_API_KEY=sk-ant-...
export DD_API_KEY=<DatadogのAPIキー>
# 以下は省略可 (デフォルト値)
export DD_SITE=ap1.datadoghq.com
export DD_LLMOBS_ML_APP=mini-agent-demo
export ANTHROPIC_MODEL=claude-haiku-4-5

python agent_app.py "bitbankってどんな会社? 1BTCが1200万円のとき0.025BTCは何円?"
```

## Datadog での確認方法

1. https://ap1.datadoghq.com/llm/traces を開く
2. ML App `mini-agent-demo` のトレースを選択
3. 見どころ:
   - **Agent スパン** (`run_agent`) — エージェント全体の入出力と所要時間
   - **LLM スパン** — 各 Claude 呼び出しのプロンプト/応答、入出力トークン数、推定コスト
   - **Tool スパン** (`calculator` / `web_search`) — ツールの入出力
   - LLM → ツール → LLM のループがウォーターフォールで見えること

## 学習ポイント

- `LLMObs.enable(...)` — SDK の初期化(agentless モード)
- `@agent` / `@tool`(ほかに `@workflow` / `@task` / `@retrieval` もある)
- `LLMObs.annotate(input_data=..., output_data=...)` — スパンへの入出力の記録
- `LLMObs.flush()` — 短命スクリプトでの送信完了待ち
