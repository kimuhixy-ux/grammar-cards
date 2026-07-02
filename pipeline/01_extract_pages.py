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
}

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


def build_messages(book: str, page: int, image_path: Path) -> list:
    return [{
        "role": "user",
        "content": [
            build_image_content(image_path),
            {"type": "text", "text": EXTRACTION_PROMPT.format(book=book, page=page)},
        ],
    }]


def extract_page(client: anthropic.Anthropic, book: str, page: int, image_path: Path) -> tuple:
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        messages=build_messages(book, page, image_path),
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

    print(f"[{args.book}] {args.start}-{args.end}ページを{args.dpi}dpiで画像化中...")
    pages = rasterize(args.book, args.start, args.end, args.dpi)

    total_in, total_out = 0, 0
    for page in range(args.start, args.end + 1):
        cache_path = CACHE_DIR / f"{args.book}_{page:04d}.json"
        if cache_path.exists() and not args.force:
            print(f"  page {page}: キャッシュ済み、スキップ")
            continue
        image_path = pages.get(page)
        if image_path is None:
            print(f"  page {page}: 画像が見つかりません、スキップ", file=sys.stderr)
            continue
        try:
            data, usage = extract_page(client, args.book, page, image_path)
        except Exception as e:
            print(f"  page {page}: エラー {e}", file=sys.stderr)
            continue
        total_in += usage.get("input_tokens", 0)
        total_out += usage.get("output_tokens", 0)
        cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        flag, category = summarize_extraction(data)
        print(f"  page {page}: {flag} {category} "
              f"(in={usage.get('input_tokens')} out={usage.get('output_tokens')})")

    print(f"\n合計トークン: input={total_in}, output={total_out}")


# ---------------------------------------------------------------------------
# batch-submit: Batches APIでページ範囲をまとめて投入する（Phase 1用）
# ---------------------------------------------------------------------------

def cmd_batch_submit(args):
    client = get_client()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[{args.book}] {args.start}-{args.end}ページを{args.dpi}dpiで画像化中...")
    pages = rasterize(args.book, args.start, args.end, args.dpi)

    target_pages = [
        p for p in range(args.start, args.end + 1)
        if args.force or not (CACHE_DIR / f"{args.book}_{p:04d}.json").exists()
    ]
    if not target_pages:
        print("対象ページは全てキャッシュ済みです。投入するページはありません。")
        return

    missing = [p for p in target_pages if p not in pages]
    if missing:
        print(f"警告: 画像が見つからないページをスキップします: {missing}", file=sys.stderr)
        target_pages = [p for p in target_pages if p in pages]

    state = load_batch_state(args.book)
    chunk_size = args.chunk_size
    for i in range(0, len(target_pages), chunk_size):
        chunk = target_pages[i:i + chunk_size]
        requests = [
            Request(
                custom_id=custom_id_for(args.book, page),
                params=MessageCreateParamsNonStreaming(
                    model=MODEL,
                    max_tokens=1500,
                    messages=build_messages(args.book, page, pages[page]),
                ),
            )
            for page in chunk
        ]
        batch = client.messages.batches.create(requests=requests)
        state["batches"].append({
            "batch_id": batch.id,
            "pages": chunk,
            "submitted_at": str(batch.created_at),
            "collected": False,
        })
        print(f"  バッチ投入: {batch.id}（{len(chunk)}ページ: {chunk[0]}-{chunk[-1]}）")

    save_batch_state(args.book, state)
    print(f"\n[{args.book}] {len(target_pages)}ページを"
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
