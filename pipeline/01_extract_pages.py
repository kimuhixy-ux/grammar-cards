#!/usr/bin/env python3
"""Phase 0/1: PDFページ -> PNG画像化 -> Vision APIで構造化JSON抽出。

サブコマンド:
  sync          同期APIで少数ページを抽出する（Phase 0 PoC用）
  batch-submit  Batches APIでページ範囲をまとめて投入する（Phase 1用、投入後は放置してよい）
  batch-collect 投入済みバッチの結果を回収し、キャッシュJSONに書き出す
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

ROOT = Path(__file__).resolve().parent.parent
PAGES_DIR = ROOT / "pipeline" / "pages"
CACHE_DIR = ROOT / "pipeline" / "cache"
SOURCE_DIR = ROOT / "source_pdfs"

MODEL = "claude-haiku-4-5-20251001"
BATCH_CHUNK_SIZE = 250  # 1バッチあたりの最大ページ数（256MB/バッチ上限に対する安全マージン）

BOOKS = {
    "forest": SOURCE_DIR / "forest.pdf",
    "chigasaki": SOURCE_DIR / "chigasaki.pdf",
    "ex_grammar": SOURCE_DIR / "ex_grammar.pdf",
}

# ex_grammar（ENGLISH EX Grammar & Usage）は「偶数ページ=設問、奇数ページ=解答＆重要ポイント」
# の2ページ1組で構成されている（本文中の複数箇所で確認済み）。設問ページ単独では正解が
# わからないため、2ページをまとめて1回のVision API呼び出しで抽出する。
BOOK_PAGE_GROUP = {
    "ex_grammar": 2,
}


def group_size_for(book: str) -> int:
    return BOOK_PAGE_GROUP.get(book, 1)


def page_groups(start: int, end: int, size: int) -> list[list[int]]:
    pages = list(range(start, end + 1))
    return [pages[i:i + size] for i in range(0, len(pages), size)]


EXTRACTION_PROMPT = """あなたは日本の英文法参考書のページ画像から内容を構造化抽出するアシスタントです。
これはCanon複合機でスキャンされた画像PDFの1ページ（{book}の{page}ページ目）です。

以下の点に注意してください:
- レイアウトの崩れ（縦書き/横書きの混在、圏点、囲み枠、表組み、見開き2ページが1画像になっている場合など）は無視し、
  本文の説明文と例文の区別だけを正確に行うこと。
- 紙が薄いことによる裏写り（次ページ/前ページの文字がうっすら透けて見えるもの）は無視すること。
- 目次・扉・索引・広告・白紙・巻末付録の一覧など、具体的な文法解説を含まないページは
  has_grammar_content を false にし、他のフィールドは空でよい。
- explanation_ja は原文の逐語転載を避け、要点を自分の言葉で要約すること（著作権配慮のため）。
- example_sentences の英文は原文のまま、日本語訳は原文があればそれを使ってよい（例文は学習目的の最小限の引用）。
- 1ページに複数の文法項目が含まれる場合は、最も主要な1つを grammar_category とし、
  explanation_ja でまとめて要約してよい。

以下のJSON形式で、**JSONのみ**を出力してください（コードフェンスや説明文は不要）:

{{
  "page": {page},
  "book": "{book}",
  "section_title": "章・セクションのタイトル（不明なら空文字）",
  "grammar_category": "文法項目名（例: 仮定法、関係代名詞など。文法内容がなければ空文字）",
  "explanation_ja": "本文の要約説明（文法内容がなければ空文字）",
  "example_sentences": [{{"en": "...", "ja": "..."}}],
  "has_grammar_content": true
}}
"""

EXTRACTION_PROMPT_PAIRED = """あなたは日本の英文法問題集（ENGLISH EX Grammar & Usage）のページ画像から
内容を構造化抽出するアシスタントです。これはCanon複合機でスキャンされた画像PDFの、
連続する2ページです。1枚目の画像は{book}の{page1}ページ目（設問ページ）、
2枚目の画像は{page2}ページ目（「解答＆重要ポイント」ページ、通常は設問ページの直後）です。

まれに、これらが問題集の前付け（はじめに・目次・章の導入解説など）で、
個別の番号付き設問になっていないことがあります。

以下の点に注意してください:
- 設問ページには番号付きの設問（例: 17, 18, 19...）があり、日本語文とその英訳（空欄補充・
  選択肢・下線部訂正など）が示されます。解答ページには同じ番号で正解と文法・語法の解説が
  示されます。必ず両ページの同じ番号同士を対応づけ、各設問について
  「正解を反映した、括弧やスラッシュを一切含まない、自然な1文の完成形の英文」を
  example_sentences の en に、対応する日本語文を ja に入れてください。
  **設問ページの ( )( ) や (A, B) のような空欄・選択肢の記号、解答ページの [ ] 内の
  正解表記や e-, c- のようなヒント文字は、最終的な en には絶対に残さないこと。**
  正解の語句をそのまま文中に組み込んで、括弧・スラッシュ・ハイフンのない
  読める1文にすること。
  具体例:
    設問「The next train (arrives, arrive) at 9:00.」＋解答「[arrives]」
      → en: "The next train arrives at 9:00."
    設問「I woke up as the lecture ( ) (e-).」＋解答「[was ending]」
      → en: "I woke up as the lecture was ending."
    設問「"Hurry up." "( )( )."」＋解答「[I'm coming]」
      → en: "\\"Hurry up.\\" \\"I'm coming.\\""
    下線部訂正問題（間違いがあれば訂正、なければそのまま）も同様に、
      訂正後の正しい英文だけを en に入れる。
- 設問1件につき、JSONリストの要素を1つ作成すること（1ページ組に複数設問があれば
  複数要素を返す）。
- explanation_ja は解答ページの解説を要点のみ自分の言葉で要約すること
  （原文の逐語転載は避ける。著作権配慮のため）。
- grammar_category は各設問が扱う文法・語法項目を簡潔に表す文字列にすること
  （例: 「現在進行形（未来の予定）」「as though/if の使い分け」など）。同じ項目が
  複数ページ・複数書籍にまたがる場合に統合しやすいよう、一般的で簡潔な表現にすること。
- 紙が薄いことによる裏写りは無視すること。
- 前付け・扉・目次・索引・白紙など具体的な設問を含まないページ組の場合は、
  has_grammar_content: false の要素を1つだけ持つリストを返すこと。

以下のJSON形式のリストで、**JSONのみ**を出力してください（コードフェンスや説明文は不要）:

[
  {{
    "page": {page1},
    "book": "{book}",
    "section_title": "章・セクションのタイトル（不明なら空文字）",
    "grammar_category": "文法項目名",
    "explanation_ja": "解答ページの解説の要約",
    "example_sentences": [{{"en": "正解を反映した完成形の英文", "ja": "対応する日本語文"}}],
    "has_grammar_content": true
  }}
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


def rasterize(book: str, first: int, last: int, dpi: int) -> dict[int, Path]:
    out_dir = PAGES_DIR / book
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / book
    subprocess.run(
        [
            "pdftoppm", "-png",
            "-f", str(first), "-l", str(last),
            "-r", str(dpi),
            str(BOOKS[book]), str(prefix),
        ],
        check=True,
    )
    pages = {}
    for path in out_dir.glob(f"{book}-*.png"):
        m = re.search(r"-(\d+)\.png$", path.name)
        if m:
            pages[int(m.group(1))] = path
    return pages


def parse_json_response(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(json)?\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return json.loads(text)


def build_image_content(image_path: Path) -> dict:
    image_bytes = image_path.read_bytes()
    b64 = base64.standard_b64encode(image_bytes).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}


def build_messages(book: str, page_group: list[int], images: dict[int, Path]) -> list:
    content = [build_image_content(images[p]) for p in page_group]
    if len(page_group) == 1:
        prompt = EXTRACTION_PROMPT.format(book=book, page=page_group[0])
    else:
        prompt = EXTRACTION_PROMPT_PAIRED.format(book=book, page1=page_group[0], page2=page_group[1])
    content.append({"type": "text", "text": prompt})
    return [{"role": "user", "content": content}]


def extract_group(client: anthropic.Anthropic, book: str, page_group: list[int], images: dict[int, Path]) -> tuple:
    max_tokens = 1500 if len(page_group) == 1 else 3000
    resp = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=build_messages(book, page_group, images),
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
    data = parse_json_response(text)
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
    }
    return data, usage


def summarize_extraction(data) -> tuple[str, str]:
    """dict（通常1ページ1項目）またはlist（見開きで複数ページ分が1画像に写り、
    モデルが複数項目を返した場合）の両方に対応して flag と grammar_category 表示を作る。"""
    items = data if isinstance(data, list) else [data]
    flag = "✓" if any(item.get("has_grammar_content") for item in items) else "-"
    category = "; ".join(item.get("grammar_category", "") for item in items if item.get("grammar_category"))
    return flag, category


def custom_id_for(book: str, page: int) -> str:
    return f"{book}-{page:04d}"


def page_from_custom_id(custom_id: str) -> tuple[str, int]:
    book, page_str = custom_id.rsplit("-", 1)
    return book, int(page_str)


def batch_state_path(book: str) -> Path:
    return CACHE_DIR / f".batch_{book}.json"


def load_batch_state(book: str) -> dict:
    path = batch_state_path(book)
    if path.exists():
        return json.loads(path.read_text())
    return {"book": book, "batches": []}


def save_batch_state(book: str, state: dict) -> None:
    batch_state_path(book).write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# sync: 少数ページを同期APIで抽出する（Phase 0 PoC用）
# ---------------------------------------------------------------------------

def cmd_sync(args):
    client = get_client()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    size = group_size_for(args.book)

    print(f"[{args.book}] {args.start}-{args.end}ページを{args.dpi}dpiで画像化中...")
    pages = rasterize(args.book, args.start, args.end, args.dpi)

    total_in, total_out = 0, 0
    for group in page_groups(args.start, args.end, size):
        cache_path = CACHE_DIR / f"{args.book}_{group[0]:04d}.json"
        if cache_path.exists() and not args.force:
            print(f"  page {group}: キャッシュ済み、スキップ")
            continue
        missing = [p for p in group if p not in pages]
        if missing:
            print(f"  page {group}: 画像が見つかりません({missing})、スキップ", file=sys.stderr)
            continue
        try:
            data, usage = extract_group(client, args.book, group, pages)
        except Exception as e:
            print(f"  page {group}: エラー {e}", file=sys.stderr)
            continue
        total_in += usage.get("input_tokens", 0)
        total_out += usage.get("output_tokens", 0)
        cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        flag, category = summarize_extraction(data)
        print(f"  page {group}: {flag} {category} "
              f"(in={usage.get('input_tokens')} out={usage.get('output_tokens')})")

    print(f"\n合計トークン: input={total_in}, output={total_out}")


# ---------------------------------------------------------------------------
# batch-submit: Batches APIでページ範囲をまとめて投入する（Phase 1用）
# ---------------------------------------------------------------------------

def cmd_batch_submit(args):
    client = get_client()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    size = group_size_for(args.book)

    print(f"[{args.book}] {args.start}-{args.end}ページを{args.dpi}dpiで画像化中...")
    pages = rasterize(args.book, args.start, args.end, args.dpi)

    target_groups = [
        g for g in page_groups(args.start, args.end, size)
        if args.force or not (CACHE_DIR / f"{args.book}_{g[0]:04d}.json").exists()
    ]
    if not target_groups:
        print("対象ページは全てキャッシュ済みです。投入するページはありません。")
        return

    missing_groups = [g for g in target_groups if any(p not in pages for p in g)]
    if missing_groups:
        print(f"警告: 画像が見つからないページ組をスキップします: {missing_groups}", file=sys.stderr)
        target_groups = [g for g in target_groups if g not in missing_groups]

    state = load_batch_state(args.book)
    chunk_size = args.chunk_size
    for i in range(0, len(target_groups), chunk_size):
        chunk = target_groups[i:i + chunk_size]
        requests = [
            Request(
                custom_id=custom_id_for(args.book, group[0]),
                params=MessageCreateParamsNonStreaming(
                    model=MODEL,
                    max_tokens=1500 if len(group) == 1 else 3000,
                    messages=build_messages(args.book, group, pages),
                ),
            )
            for group in chunk
        ]
        batch = client.messages.batches.create(requests=requests)
        state["batches"].append({
            "batch_id": batch.id,
            "pages": [g[0] for g in chunk],
            "submitted_at": str(batch.created_at),
            "collected": False,
        })
        print(f"  バッチ投入: {batch.id}（{len(chunk)}組: {chunk[0][0]}-{chunk[-1][-1]}）")

    save_batch_state(args.book, state)
    total_pages = sum(len(g) for g in target_groups)
    print(f"\n[{args.book}] {total_pages}ページ（{len(target_groups)}組）を"
          f"{sum(1 for b in state['batches'] if not b['collected'])}バッチで投入しました。")
    print("完了まで最大24時間かかることがあります。放置してよい設計です。")
    print(f"結果を回収するには: python3 pipeline/01_extract_pages.py batch-collect --book {args.book}")


# ---------------------------------------------------------------------------
# batch-collect: 投入済みバッチの結果を回収し、キャッシュJSONに書き出す
# ---------------------------------------------------------------------------

def cmd_batch_collect(args):
    client = get_client()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    state = load_batch_state(args.book)

    pending_batches = [b for b in state["batches"] if not b["collected"]]
    if not pending_batches:
        print(f"[{args.book}] 未回収のバッチはありません。")
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
            total_pending += len(b["pages"])
            continue

        for result in client.messages.batches.results(b["batch_id"]):
            book, page = page_from_custom_id(result.custom_id)
            if result.result.type == "succeeded":
                msg = result.result.message
                text = "".join(block.text for block in msg.content if block.type == "text")
                try:
                    data = parse_json_response(text)
                except Exception as e:
                    print(f"    page {page}: JSON parse error: {e}", file=sys.stderr)
                    total_errored += 1
                    continue
                cache_path = CACHE_DIR / f"{book}_{page:04d}.json"
                cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
                flag, category = summarize_extraction(data)
                print(f"    page {page}: {flag} {category}")
                total_succeeded += 1
            else:
                print(f"    page {page}: {result.result.type}", file=sys.stderr)
                total_errored += 1
        b["collected"] = True

    save_batch_state(args.book, state)
    print(f"\n[{args.book}] 回収結果: succeeded={total_succeeded} errored={total_errored} pending={total_pending}")
    if total_pending:
        print("未完了のバッチがあります。しばらく待ってから再度 batch-collect を実行してください。")


def main():
    parser = argparse.ArgumentParser(description="PDFページをVision APIで構造化抽出する")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="同期APIで少数ページを抽出する（Phase 0 PoC用）")
    p_sync.add_argument("--book", required=True, choices=BOOKS.keys())
    p_sync.add_argument("--start", type=int, required=True)
    p_sync.add_argument("--end", type=int, required=True)
    p_sync.add_argument("--dpi", type=int, default=150)
    p_sync.add_argument("--force", action="store_true", help="キャッシュを無視して再抽出する")
    p_sync.set_defaults(func=cmd_sync)

    p_submit = sub.add_parser("batch-submit", help="Batches APIでページ範囲を投入する（Phase 1用）")
    p_submit.add_argument("--book", required=True, choices=BOOKS.keys())
    p_submit.add_argument("--start", type=int, required=True)
    p_submit.add_argument("--end", type=int, required=True)
    p_submit.add_argument("--dpi", type=int, default=150)
    p_submit.add_argument("--force", action="store_true", help="キャッシュを無視して再投入する")
    p_submit.add_argument("--chunk-size", type=int, default=BATCH_CHUNK_SIZE)
    p_submit.set_defaults(func=cmd_batch_submit)

    p_collect = sub.add_parser("batch-collect", help="投入済みバッチの結果を回収する")
    p_collect.add_argument("--book", required=True, choices=BOOKS.keys())
    p_collect.set_defaults(func=cmd_batch_collect)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
