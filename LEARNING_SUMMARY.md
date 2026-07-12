# Datadog AI Observability 学習まとめ (2026-07-12)

Anthropic SDK 製のミニ・エージェントアプリを自作し、Datadog **AI Observability**(旧称 LLM Observability)のトライアル (ap1.datadoghq.com) で監視するまでを1日で実践した記録。

## 1. AI Observability とは何か / 何を監視するのか

- 監視対象は **「自分たちが開発・計装した LLM アプリケーション」**。Claude Code のような既製の対話型コーディングエージェントの利用そのものを監視する製品ではない。
- SDK(Python の `ddtrace`)で計装すると、以下が Datadog に送られる:
  - LLM 呼び出し1回ごとのトレース(プロンプト全文・応答・入出力トークン数・レイテンシ・推定コスト)
  - エージェントワークフロー全体のエンドツーエンドトレース(LLM → ツール実行 → LLM のループ構造)
  - 品質評価 (Evaluations)、コスト集計 (Cost)、利用パターン分類 (Patterns) など

## 2. 作ったもの

```
質問 ──> run_agent (@agent スパン)
           ├─ anthropic.request  ← ddtrace が自動計装 (LLM スパン)
           ├─ web_search / calculator (@tool スパン)
           ├─ ツール結果を添えて再度 anthropic.request
           └─ 最終回答
                      └──> agentless モードで ap1.datadoghq.com へ直接送信
```

- 言語: Python / モデル: `claude-haiku-4-5` / ML App 名: `mini-agent-demo`
- ツール: `calculator`(AST による安全な数式評価)と `web_search`(外部依存なしの疑似検索)
- 計装のポイント:
  - `LLMObs.enable(ml_app=..., api_key=..., site="ap1.datadoghq.com", agentless_enabled=True)` — **Datadog Agent のインストール不要**(agentless)
  - Anthropic SDK の呼び出しは**自動計装**(コード変更不要で LLM スパンが生成される)
  - `@agent` / `@tool` デコレータで手動スパンを追加(ほかに `@workflow` `@task` `@retrieval` がある)
  - `LLMObs.annotate(input_data=..., output_data=...)` でスパンに入出力を記録
  - 短命スクリプトでは終了前に `LLMObs.flush()` で送信完了を保証

必要だったキーは2つ: **Anthropic API キー**(console.anthropic.com、要クレジット購入)と **Datadog API キー**(Organization Settings > API Keys)。

## 3. トレース画面 (AI Observability > Traces) で読み取れたこと

- ウォーターフォールで `run_agent`(3.04s)の内訳が見え、**所要時間はほぼ全て LLM 推論**(1.45s + 1.59s)。ツールは百数十マイクロ秒。
  - → エージェントが遅いとき、ボトルネックが LLM かツール(外部API)かをここで切り分けられる。
- Input Tokens が 1.04K と質問の長さに比べて大きい。**システムプロンプト + ツール定義(JSON スキーマ)が毎回送られる「見えない固定費」**がエージェントのコストを支配する。
- Estimated Cost が Input 0.1¢ / Output 0.06¢ のように**スパン単位でコスト表示**される。
- Output Messages から、モデルが1ターンで `web_search` と `calculator` の**2ツールを並列要求**していたことが分かった(コンソール出力だけでは分からなかった)。
- Chat/Span トグルで、同じスパンを会話形式でも生データでも確認できる。

## 4. Evaluations (LLM-as-a-judge) で分かったこと

- CONFIGURE > Evaluations > Create Evaluation からマネージド評価を有効化。LLM 判定型の評価は**判定用 LLM のプロバイダーアカウント接続**(Anthropic/OpenAI/Bedrock の API キーを Datadog に登録)が必要。
- 評価は**有効化後に届いた新しいトレースに対して**実行される(過去分には付かない)。
- 実験結果: 「今日の東京の天気を教えて」という(疑似検索では答えられない)質問に対し、`failure-to-answer` 評価が **"Redirection Response"** と分類。REASONING 欄に「リアルタイム天気情報を取得できないと明言し、外部サイト (jma.go.jp, tenki.jp 等) へ誘導したため」という**判定根拠まで記録**された。
- トレース一覧は Failed Evals でフィルタ可能。評価失敗を Monitor(アラート)にすることもできる。
- **トレースの「形」から挙動の違いが読める**: bitbank の質問は 5 spans(ツール2回呼び出し)、天気の質問は 2 spans(ツールを試さず即「無理」と回答)。Patterns 機能はこの形状の違いでトレースをクラスタリングする。

## 5. ハマりどころメモ (Windows / 契約まわり)

- **PowerShell の `curl` は Invoke-WebRequest のエイリアス**。本物を使うなら `curl.exe`。行継続は `\` ではなくバッククォート。
- **PowerShell 5.1 の文字化け問題**: 送信は `[System.Text.Encoding]::UTF8.GetBytes($body)` でバイト列にして解決。受信は Invoke-RestMethod が UTF-8 応答を Latin-1 として誤解釈するため、`Invoke-WebRequest` の `RawContentStream` を UTF-8 でデコードするか、PowerShell 7 (`pwsh`) を使う。
- **Claude Max プラン (claude.ai) と API (platform.claude.com) は別会計**。Max を契約しても API クレジットは含まれない。Max プランの確認場所は claude.ai > Settings > Billing。
- Hallucination 評価は LLM に渡したコンテキストの annotation が SDK 側で必要なため、今回のアプリではまだ動かない(次回の課題)。

## 6. 次にやるなら

- Hallucination 評価のためのコンテキスト annotation(`@retrieval` スパン)
- `web_search` を bitbank 公開 API など本物のツールに置き換え
- Cost / Patterns / Playground / Experiments 画面の探索
- 評価失敗やレイテンシ悪化の Monitor(アラート)作成

## リポジトリ構成

| ファイル | 内容 |
|---|---|
| `agent_app.py` | エージェント本体 (Anthropic SDK + ddtrace LLMObs 計装) |
| `requirements.txt` | 依存パッケージ (anthropic, ddtrace) |
| `README.md` | セットアップ・実行手順 (Windows/macOS/Linux) |
| `LEARNING_SUMMARY.md` | このファイル |
