"""Convenience launcher.

Usage:
    python run.py              # start the web server on port 8001
    python run.py --port 9000  # custom port
    python run.py --crawl      # one-shot crawl from CLI and exit

Environment:
    Auto-loads .env from the project root on startup (proxy, LLM key,
    Zhihu cookie, etc.). See .env.example for the template.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Load .env FIRST, before importing any app module that reads os.environ
# at import time (httpx proxies, LLM key, Zhihu cookie ...).
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path, override=False)
        print(f"[env] loaded {_env_path}")
except ImportError:
    pass  # python-dotenv not installed; user can still set env vars manually


def main() -> None:
    parser = argparse.ArgumentParser(description="TrendRadarCN launcher")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument(
        "--crawl",
        action="store_true",
        help="Run a one-shot crawl of all sources then exit",
    )
    parser.add_argument(
        "--board",
        metavar="KEY",
        help="Run one specific topic board and exit (e.g. --board stocks-cn). "
        "Use --board all to run every board.",
    )
    parser.add_argument(
        "--backfill-boards",
        action="store_true",
        help="Re-tag and re-cluster existing articles with the current "
        "boards.yaml config, then exit. Run this after you edit the tags "
        "block in boards.yaml.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="With --backfill-boards: clear existing tags first so EVERY "
        "row gets re-tagged (use after switching tagger, or changing tag "
        "schema). Without --force, backfill only touches NULL rows.",
    )
    parser.add_argument(
        "--reset-tags",
        metavar="KEY",
        help="NULL out the tags column for one board (or 'all'), so the "
        "next --backfill-boards re-tags from scratch. Useful after switching "
        "tagger: rule -> llm, or after a big tag schema change.",
    )
    parser.add_argument(
        "--test-llm",
        action="store_true",
        help="Send 3 hard-coded sample articles to the configured LLM "
        "endpoint and print the full request/response. Use this to debug "
        "key/network/model-name issues.",
    )
    parser.add_argument(
        "--email",
        action="store_true",
        help="After --board completes, build a digest of the last 24h of new "
        "articles and email it (config in .env: SMTP_HOST/USER/PASS/TO). "
        "Silently skipped if SMTP isn't configured — does NOT fail the board "
        "run. Designed for the daily scheduled task.",
    )
    parser.add_argument(
        "--test-email",
        action="store_true",
        help="Send a minimal test email to verify SMTP setup and exit. "
        "Exits non-zero if SMTP is misconfigured or send fails (so users "
        "can debug independently of any board run).",
    )
    args = parser.parse_args()

    if args.test_email:
        from app.mailer import is_configured, send_mail

        if not is_configured():
            print(
                "ERROR: SMTP not configured. Required env vars: "
                "SMTP_HOST, SMTP_USER, SMTP_PASS, SMTP_TO",
                flush=True,
            )
            sys.exit(1)
        try:
            send_mail(
                subject="TrendRadarCN · 测试邮件",
                html="<p>This is a test email from TrendRadarCN. SMTP setup is working.</p>",
                text="This is a test email from TrendRadarCN. SMTP setup is working.",
            )
            print(f"OK: test email sent to {os.getenv('SMTP_TO')}", flush=True)
        except Exception as e:  # noqa: BLE001 - report any send failure
            print(f"ERROR: send failed - {type(e).__name__}: {e}", flush=True)
            sys.exit(1)
        return

    if args.test_llm:
        import json as _json
        from app.boards.llm_tagger import (
            SYSTEM_PROMPT, api_key, base_url, build_user_prompt, is_configured, model,
        )
        import httpx

        if not is_configured():
            print("❌ TRENDRADAR_LLM_API_KEY / DEEPSEEK_API_KEY 都没设。", flush=True)
            return
        key = api_key()
        url = f"{base_url()}/v1/chat/completions"
        sample_batch = [
            (1, "英伟达发布 H200 芯片, 推理性能提升 40%", "新一代加速器, HBM3e 显存"),
            (2, "英伟达股价创新高, 市值突破 3.5 万亿美元", "黄仁勋接受采访谈未来"),
            (3, "OpenAI 完成 60 亿美元融资, 估值 1500 亿美元", "Microsoft 领投"),
        ]
        sample_tags = {
            "大模型": ["GPT", "LLM"],
            "AI芯片": ["H100", "MI300", "加速器"],
            "公司": ["OpenAI", "英伟达", "Anthropic"],
            "融资": ["融资", "估值", "投资"],
        }
        payload = {
            "model": model(),
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(sample_batch, sample_tags)},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        print(f"URL:    {url}", flush=True)
        print(f"Model:  {model()}", flush=True)
        print(f"Key:    {key[:8]}...{key[-4:]} (len={len(key)})", flush=True)
        print("\n--- 发送中 (timeout 30s) ---\n", flush=True)
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            print(f"HTTP {resp.status_code}", flush=True)
            try:
                data = resp.json()
                print(_json.dumps(data, ensure_ascii=False, indent=2)[:3000], flush=True)
            except Exception:
                print("Body (raw):", resp.text[:2000], flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"❌ {type(exc).__name__}: {exc}", flush=True)
        return

    if args.reset_tags:
        from app.boards import load_boards
        from app.boards.service import reset_board_tags
        from app.db import init_db

        init_db()
        targets = (
            [b.key for b in load_boards()]
            if args.reset_tags == "all"
            else [args.reset_tags]
        )
        total = 0
        for key in targets:
            n = reset_board_tags(key)
            total += n
            print(f"  reset[{key}]: cleared tags on {n} rows", flush=True)
        print(
            f"\n完成。共清空 {total} 篇文章的标签。"
            f"现在跑 `python run.py --backfill-boards` 重新打标签。",
            flush=True,
        )
        return

    if args.backfill_boards:
        from app.boards import load_boards
        from app.boards.service import backfill_tags_and_clusters, reset_board_tags
        from app.db import init_db

        init_db()
        if args.force:
            print("--force: 先清空所有板块的现有标签…", flush=True)
            total_reset = 0
            for b in load_boards():
                n = reset_board_tags(b.key)
                print(
                    f"  reset[{b.key}]: cleared {n} rows  (tagger={b.tagger})",
                    flush=True,
                )
                total_reset += n
            print(f"  共清空 {total_reset} 行\n", flush=True)
        print("正在回填老文章的标签与事件聚类…", flush=True)
        summary = asyncio.run(backfill_tags_and_clusters(verbose=True))
        total_tagged = sum(s["tagged"] for s in summary.values())
        total_clustered = sum(s["clustered"] for s in summary.values())
        total_cost = sum(
            (s["llm"] or {}).get("cost_usd", 0.0) for s in summary.values()
        )
        cost_line = (
            f"  LLM 成本: ${total_cost:.5f}\n" if total_cost > 0 else ""
        )
        print(
            f"\n完成。共补标签 {total_tagged} 篇，补聚类 {total_clustered} 篇。\n{cost_line}",
            flush=True,
        )
        return

    if args.crawl:
        from app.crawlers import iter_crawlers
        from app.db import init_db
        from app.service import crawl_all_iter

        init_db()
        total = len(list(iter_crawlers()))
        print(
            f"正在并发抓取 {total} 个源…（无法访问的源会超时+重试，最长可能要 1-2 分钟）\n",
            flush=True,
        )

        async def _run() -> None:
            done = 0
            async for r in crawl_all_iter():
                done += 1
                status = "OK " if r["status"] == "ok" else "ERR"
                err = r.get("error") or ""
                print(
                    f"  [{done}/{total}] [{status}] {r['source_key']:12s} "
                    f"items={r['item_count']:>3}  {err}",
                    flush=True,
                )

        asyncio.run(_run())
        print("\n完成。", flush=True)
        return

    if args.board:
        from app.boards import load_boards
        from app.boards.service import run_all_boards, run_board
        from app.db import init_db

        init_db()

        if args.board == "all":
            boards = load_boards()
            print(f"正在并发跑 {len(boards)} 个主题板…\n", flush=True)

            async def _run_all() -> None:
                done = 0
                async for r in run_all_boards():
                    done += 1
                    status = "OK " if r["status"] == "ok" else "ERR"
                    print(
                        f"  [{done}/{len(boards)}] [{status}] {r['board_key']:14s} "
                        f"feeds={r['feeds_ok']}/{r['feeds_total']} "
                        f"new={r['articles_new']:>3}  seen={r['articles_seen']:>3}",
                        flush=True,
                    )
                    for e in r.get("errors") or []:
                        print(f"      ! {e}", flush=True)

            asyncio.run(_run_all())
        else:
            print(f"正在跑主题板：{args.board} …\n", flush=True)
            r = asyncio.run(run_board(args.board))
            status = "OK " if r["status"] == "ok" else "ERR"
            print(
                f"  [{status}] {r['board_key']}  "
                f"feeds={r.get('feeds_ok')}/{r.get('feeds_total')}  "
                f"new={r.get('articles_new')}  seen={r.get('articles_seen')}",
                flush=True,
            )
            for e in r.get("errors") or []:
                print(f"  ! {e}", flush=True)
        print("\n完成。", flush=True)

        if args.email:
            from app.mailer import is_configured, send_mail

            if not is_configured():
                print(
                    "[email] SMTP not configured "
                    "(set SMTP_HOST/USER/PASS/TO in .env to enable). Skipping send.",
                    flush=True,
                )
            else:
                from app.digest import build_digest

                print(
                    f"[email] sending digest to {os.getenv('SMTP_TO')} ...",
                    flush=True,
                )
                try:
                    digest = build_digest(hours=24)
                    send_mail(digest["subject"], digest["html"], digest["text"])
                    print(
                        f"[email] sent: {digest['total_articles']} articles "
                        f"across {len(digest['board_counts'])} boards",
                        flush=True,
                    )
                except Exception as e:  # noqa: BLE001 - never fail the board run
                    print(
                        f"[email] WARN: send failed - {type(e).__name__}: {e}",
                        flush=True,
                    )
        return

    import uvicorn

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
