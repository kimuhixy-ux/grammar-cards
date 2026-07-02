#!/usr/bin/env python3
"""Phase 3: grammar_items.json -> 各文法項目からフラッシュカードを生成する。

サブコマンド:
  sync          同期APIで少数項目を試す（PoC用）
  batch-submit  Batches APIで項目範囲をまとめて投入する（本番用、投入後は放置してよい）
  batch-collect 投入済みバッチの結果を回収し、項目単位のキャッシュJSONに書き出す
  build         キャッシュ済みの全項目からカードを集約し、cards.json を生成する
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "pipeline" / "output"
CARDS_CACHE_DIR = ROOT / "pipeline" / "cache" / "cards"
ITEMS_PATH = OUTPUT_DIR / "grammar_items.json"
CARDS_PATH = OUTPUT_DIR / "cards.json"
BATCH_STATE_PATH = ROOT / "pipeline" / "cache" / ".batch_cards.json"

MODEL = "claude-sonnet-4-6"
BATCH_CHUNK_SIZE = 250

TYPE_ABBREV = {
    "fill_blank": "fb",
    "multiple_choice": "mc",
    "reorder": "ro",
    "translate_ja_to_en": "tr",
}

GENERATION_PROMPT = """あなたは日本の英文法学習アプリのためのフラッシュカード作成者です。
以下の文法項目の情報から、Leitner式間隔反復で出題するカードを3〜5問作成してください。

文法カテゴリ: {grammar_category}
解説: {explanation_ja}
例文:
{examples_block}

出題タイプは以下の4種類です。内容に無理なく当てはまるものを選び、できれば複数タイプを混ぜてください
（すべての種類を無理に含める必要はありません）:

- fill_blank（空所補充）: 英文中の1箇所を ___ にした問題。answerは空所に入る語句、choicesはnull。
- multiple_choice（4択）: 英文または文法知識を問う4択問題。choicesは4つの選択肢の配列、
  answerはchoicesの中の正解の文字列と完全一致させること。
- reorder（整序英作文）: 日本語の意味に合うように語句を並べ替えて英文を完成させる問題。
  questionには日本語の意味を書き、choicesにはシャッフルされた語句の配列を入れる。
  answerは正しく並べた完全な英文。
- translate_ja_to_en（和文英訳）: questionに日本語文、answerに自然な英訳を書く。choicesはnull。

各カードには日本語で簡潔なexplanation（なぜその答えになるかの文法的理由）を付けてください。
例文をそのまま使ってよいですが、learnerにとって自然な難易度になるよう調整してください。

以下のJSON配列の形式で、**JSONのみ**を出力してください（コードフェンスや説明文は不要）:

[
  {{"type": "fill_blank", "question": "...", "answer": "...", "choices": null, "explanation": "..."}},
  {{"type": "multiple_choice", "question": "...", "choices": ["...", "...", "...", "..."], "answer": "...", "explanation": "..."}}
]
"""


def load_env():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value)


def get_client() -> anthropic.Anthropic:
    load_env()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY が見つかりません（.env を確認してください）", file=sys.stderr)
        sys.exit(1)
    return anthropic.Anthropic(api_key=api_key)


def load_items() -> list[dict]:
    if not ITEMS_PATH.exists():
        print(f"{ITEMS_PATH} が見つかりません。先に 02_merge_items.py を実行してください。", file=sys.stderr)
        sys.exit(1)
    return json.loads(ITEMS_PATH.read_text())


def parse_json_response(text: str) -> list:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(json)?\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return json.loads(text)


def build_examples_block(item: dict, limit: int = 6) -> str:
    lines = []
    for ex in (item.get("example_sentences") or [])[:limit]:
        en, ja = ex.get("en", ""), ex.get("ja", "")
        lines.append(f"- {en} ({ja})" if ja else f"- {en}")
    return "\n".join(lines) if lines else "（例文なし）"


def build_messages(item: dict) -> list:
    prompt = GENERATION_PROMPT.format(
        grammar_category=item["grammar_category"],
        explanation_ja=item["explanation_ja"],
        examples_block=build_examples_block(item),
    )
    return [{"role": "user", "content": prompt}]


def finalize_cards(item: dict, raw_cards: list) -> list[dict]:
    """モデル出力（type/question/answer/choices/explanation のみ）に
    id/book/page/grammar_category を付与してCLAUDE.mdのカード形式に整形する。"""
    source = (item.get("sources") or [{}])[0]
    book, page = source.get("book", ""), source.get("page")
    seq_by_type: dict[str, int] = {}
    cards = []
    for raw in raw_cards:
        card_type = raw.get("type")
        abbrev = TYPE_ABBREV.get(card_type)
        if abbrev is None:
            continue  # 未知のtypeは無視（スキーマ逸脱への防御）
        seq_by_type[card_type] = seq_by_type.get(card_type, 0) + 1
        cards.append({
            "id": f"{item['id']}-{abbrev}-{seq_by_type[card_type]:02d}",
            "book": book,
            "page": page,
            "grammar_category": item["grammar_category"],
            "type": card_type,
            "question": raw.get("question", ""),
            "answer": raw.get("answer", ""),
            "choices": raw.get("choices"),
            "explanation": raw.get("explanation", ""),
        })
    return cards


def item_cache_path(item_id: str) -> Path:
    return CARDS_CACHE_DIR / f"{item_id}.json"


# ---------------------------------------------------------------------------
# sync: 少数項目を同期APIで試す（PoC用）
# ---------------------------------------------------------------------------

def cmd_sync(args):
    client = get_client()
    CARDS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    items = load_items()[args.start:args.end]

    total_in, total_out = 0, 0
    for item in items:
        cache_path = item_cache_path(item["id"])
        if cache_path.exists() and not args.force:
            print(f"  {item['id']}: キャッシュ済み、スキップ")
            continue
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=2000,
                messages=build_messages(item),
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            raw_cards = parse_json_response(text)
        except Exception as e:
            print(f"  {item['id']}: エラー {e}", file=sys.stderr)
            continue
        cards = finalize_cards(item, raw_cards)
        cache_path.write_text(json.dumps(cards, ensure_ascii=False, indent=2))
        total_in += resp.usage.input_tokens
        total_out += resp.usage.output_tokens
        types = ",".join(c["type"] for c in cards)
        print(f"  {item['id']}: {item['grammar_category']} -> {len(cards)}枚 [{types}] "
              f"(in={resp.usage.input_tokens} out={resp.usage.output_tokens})")

    print(f"\n合計トークン: input={total_in}, output={total_out}")


# ---------------------------------------------------------------------------
# batch-submit / batch-collect
# ---------------------------------------------------------------------------

def load_batch_state() -> dict:
    if BATCH_STATE_PATH.exists():
        return json.loads(BATCH_STATE_PATH.read_text())
    return {"batches": []}


def save_batch_state(state: dict) -> None:
    BATCH_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def cmd_batch_submit(args):
    client = get_client()
    CARDS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    all_items = load_items()

    target_items = [
        item for item in all_items
        if args.force or not item_cache_path(item["id"]).exists()
    ]
    if not target_items:
        print("対象項目は全てキャッシュ済みです。投入する項目はありません。")
        return

    state = load_batch_state()
    chunk_size = args.chunk_size
    for i in range(0, len(target_items), chunk_size):
        chunk = target_items[i:i + chunk_size]
        requests = [
            Request(
                custom_id=item["id"],
                params=MessageCreateParamsNonStreaming(
                    model=MODEL,
                    max_tokens=2000,
                    messages=build_messages(item),
                ),
            )
            for item in chunk
        ]
        batch = client.messages.batches.create(requests=requests)
        state["batches"].append({
            "batch_id": batch.id,
            "item_ids": [item["id"] for item in chunk],
            "submitted_at": str(batch.created_at),
            "collected": False,
        })
        print(f"  バッチ投入: {batch.id}（{len(chunk)}項目: {chunk[0]['id']}-{chunk[-1]['id']}）")

    save_batch_state(state)
    print(f"\n{len(target_items)}項目を"
          f"{sum(1 for b in state['batches'] if not b['collected'])}バッチで投入しました。")
    print("完了まで最大24時間かかることがあります。放置してよい設計です。")
    print("結果を回収するには: python3 pipeline/03_generate_cards.py batch-collect")


def cmd_batch_collect(args):
    client = get_client()
    CARDS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    items_by_id = {item["id"]: item for item in load_items()}
    state = load_batch_state()

    pending_batches = [b for b in state["batches"] if not b["collected"]]
    if not pending_batches:
        print("未回収のバッチはありません。")
        return

    total_succeeded, total_errored, total_pending = 0, 0, 0
    for b in pending_batches:
        batch = client.messages.batches.retrieve(b["batch_id"])
        status = batch.processing_status
        counts = batch.request_counts
        print(f"  {b['batch_id']}: status={status} "
              f"(succeeded={counts.succeeded} errored={counts.errored} "
              f"processing={counts.processing} canceled={counts.canceled} expired={counts.expired})")

        if status != "ended":
            total_pending += len(b["item_ids"])
            continue

        for result in client.messages.batches.results(b["batch_id"]):
            item_id = result.custom_id
            item = items_by_id.get(item_id)
            if item is None:
                print(f"    {item_id}: grammar_items.jsonに見つかりません、スキップ", file=sys.stderr)
                total_errored += 1
                continue
            if result.result.type == "succeeded":
                msg = result.result.message
                text = "".join(block.text for block in msg.content if block.type == "text")
                try:
                    raw_cards = parse_json_response(text)
                except Exception as e:
                    print(f"    {item_id}: JSON parse error: {e}", file=sys.stderr)
                    total_errored += 1
                    continue
                cards = finalize_cards(item, raw_cards)
                item_cache_path(item_id).write_text(json.dumps(cards, ensure_ascii=False, indent=2))
                total_succeeded += 1
            else:
                print(f"    {item_id}: {result.result.type}", file=sys.stderr)
                total_errored += 1
        b["collected"] = True

    save_batch_state(state)
    print(f"\n回収結果: succeeded={total_succeeded} errored={total_errored} pending={total_pending}")
    if total_pending:
        print("未完了のバッチがあります。しばらく待ってから再度 batch-collect を実行してください。")


# ---------------------------------------------------------------------------
# build: キャッシュ済みの全項目からカードを集約し、cards.json を生成する
# ---------------------------------------------------------------------------

def cmd_build(args):
    items = load_items()
    all_cards = []
    missing = []
    for item in items:
        cache_path = item_cache_path(item["id"])
        if not cache_path.exists():
            missing.append(item["id"])
            continue
        all_cards.extend(json.loads(cache_path.read_text()))

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CARDS_PATH.write_text(json.dumps(all_cards, ensure_ascii=False, indent=2))

    by_type: dict[str, int] = {}
    for c in all_cards:
        by_type[c["type"]] = by_type.get(c["type"], 0) + 1

    print(f"カード総数: {len(all_cards)}枚")
    for t, n in by_type.items():
        print(f"  {t}: {n}枚")
    if missing:
        print(f"\n未生成の項目: {len(missing)}件（batch-collect未完了、または未投入）", file=sys.stderr)
    print(f"出力: {CARDS_PATH}")


def main():
    parser = argparse.ArgumentParser(description="grammar_items.jsonからフラッシュカードを生成する")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="同期APIで少数項目を試す（PoC用）")
    p_sync.add_argument("--start", type=int, default=0)
    p_sync.add_argument("--end", type=int, default=5)
    p_sync.add_argument("--force", action="store_true")
    p_sync.set_defaults(func=cmd_sync)

    p_submit = sub.add_parser("batch-submit", help="Batches APIで項目を投入する")
    p_submit.add_argument("--force", action="store_true")
    p_submit.add_argument("--chunk-size", type=int, default=BATCH_CHUNK_SIZE)
    p_submit.set_defaults(func=cmd_batch_submit)

    p_collect = sub.add_parser("batch-collect", help="投入済みバッチの結果を回収する")
    p_collect.set_defaults(func=cmd_batch_collect)

    p_build = sub.add_parser("build", help="キャッシュ済みカードを集約してcards.jsonを生成する")
    p_build.set_defaults(func=cmd_build)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
