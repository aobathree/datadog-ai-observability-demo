"""
Datadog AI Observability (旧 LLM Observability) 学習用のミニ・エージェントアプリ。

構成:
  - Anthropic SDK でエージェントループ (LLM → ツール実行 → LLM ...) を実装
  - ツールは2つ: calculator (数式計算) / web_search (疑似Web検索、外部依存なし)
  - ddtrace の LLM Observability SDK で計装
      * Anthropic の LLM 呼び出しは自動計装 (LLM スパン)
      * エージェントループは @agent、各ツールは @tool で手動計装

実行方法 (README.md 参照):
  export ANTHROPIC_API_KEY=sk-ant-...
  export DD_API_KEY=<DatadogのAPIキー>
  python agent_app.py "bitbankってどんな会社? 1BTCが1200万円のとき0.025BTCは何円?"
"""

import ast
import operator
import os
import sys

import anthropic
from ddtrace.llmobs import LLMObs
from ddtrace.llmobs.decorators import agent, tool

# ---------------------------------------------------------------------------
# LLM Observability の初期化 (agentless モード: Datadog Agent 不要で直接送信)
# ---------------------------------------------------------------------------
LLMObs.enable(
    ml_app=os.getenv("DD_LLMOBS_ML_APP", "mini-agent-demo"),
    api_key=os.environ["DD_API_KEY"],
    site=os.getenv("DD_SITE", "ap1.datadoghq.com"),
    agentless_enabled=True,
)

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
MAX_TURNS = 10

client = anthropic.Anthropic()  # ANTHROPIC_API_KEY を環境変数から読む

# ---------------------------------------------------------------------------
# ツール1: 計算機 (安全な AST 評価。eval は使わない)
# ---------------------------------------------------------------------------
_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval(node):
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError(f"サポートされていない式です: {ast.dump(node)}")


@tool
def calculator(expression: str) -> str:
    """数式文字列を計算して結果を返す。"""
    try:
        result = _safe_eval(ast.parse(expression, mode="eval"))
        output = str(result)
    except Exception as e:  # noqa: BLE001
        output = f"計算エラー: {e}"
    # スパンに入出力を記録 (Datadog のトレース画面で見えるようになる)
    LLMObs.annotate(input_data=expression, output_data=output)
    return output


# ---------------------------------------------------------------------------
# ツール2: 疑似Web検索 (外部依存なしのローカル知識ベース)
# ---------------------------------------------------------------------------
_KNOWLEDGE_BASE = {
    "bitbank": (
        "bitbank(ビットバンク株式会社)は日本の暗号資産取引所。"
        "BTC/JPY をはじめ多数の現物取引ペアを提供し、公開API(REST/WebSocket)で"
        "ティッカーや板情報を取得できる。"
    ),
    "datadog": (
        "Datadog はクラウド監視の SaaS。AI Observability(旧 LLM Observability)では"
        "LLM アプリのトレース・トークン使用量・コスト・品質評価を監視できる。"
    ),
    "rust": (
        "Rust は所有権システムによりメモリ安全性を保証するシステムプログラミング言語。"
        "CLI ツールの実装では clap や tokio がよく使われる。"
    ),
    "claude": (
        "Claude は Anthropic が開発する LLM ファミリー。API 経由で tool use "
        "(function calling) を使ったエージェント構築が可能。"
    ),
}


@tool
def web_search(query: str) -> str:
    """疑似Web検索。ローカル知識ベースからキーワード一致で結果を返す。"""
    q = query.lower()
    hits = [text for key, text in _KNOWLEDGE_BASE.items() if key in q]
    output = (
        "\n".join(f"[検索結果] {h}" for h in hits)
        if hits
        else "検索結果が見つかりませんでした。"
    )
    LLMObs.annotate(input_data=query, output_data=output)
    return output


TOOL_FUNCTIONS = {"calculator": calculator, "web_search": web_search}

TOOLS_SPEC = [
    {
        "name": "calculator",
        "description": "四則演算・べき乗・剰余を含む数式を計算します。例: '12000000 * 0.025'",
        "input_schema": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "Python風の数式文字列"}
            },
            "required": ["expression"],
        },
    },
    {
        "name": "web_search",
        "description": "Webを検索して関連情報の要約を返します。固有名詞や事実の確認に使ってください。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "検索クエリ"}
            },
            "required": ["query"],
        },
    },
]

SYSTEM_PROMPT = (
    "あなたは質問に答えるアシスタントです。"
    "事実の確認には web_search ツールを、数値計算には必ず calculator ツールを使ってください。"
    "最後に日本語で簡潔に回答をまとめてください。"
)


# ---------------------------------------------------------------------------
# エージェントループ: LLM → ツール実行 → 結果を返して再度 LLM → ... → 最終回答
# ---------------------------------------------------------------------------
@agent
def run_agent(question: str) -> str:
    messages = [{"role": "user", "content": question}]
    final_text = ""

    for turn in range(MAX_TURNS):
        # ここは ddtrace が自動計装するので LLM スパンが自動で生成される
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS_SPEC,
            messages=messages,
        )

        # モデルのテキスト出力を表示
        for block in response.content:
            if block.type == "text":
                print(f"\n[assistant] {block.text}")
                final_text = block.text

        if response.stop_reason != "tool_use":
            break  # ツール呼び出しなし = 最終回答

        # ツール呼び出しを実行して tool_result として返す
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                fn = TOOL_FUNCTIONS[block.name]
                kwargs = dict(block.input)
                print(f"\n[tool call] {block.name}({kwargs})")
                result = fn(**kwargs)
                print(f"[tool result] {result}")
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    }
                )
        messages.append({"role": "user", "content": tool_results})

    # エージェントスパン全体の入出力を記録
    LLMObs.annotate(input_data=question, output_data=final_text)
    return final_text


def main() -> None:
    question = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "bitbankってどんな会社ですか? また、1BTCが1200万円のとき 0.025 BTC は何円になりますか?"
    )
    print(f"[question] {question}")
    answer = run_agent(question)
    print(f"\n===== 最終回答 =====\n{answer}")

    # 短命プロセスなので明示的に Datadog へ送信を完了させる
    LLMObs.flush()
    print("\n(トレースを Datadog に送信しました → ap1.datadoghq.com の AI Observability > Traces で確認)")


if __name__ == "__main__":
    main()
