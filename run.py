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
from datetime import datetime
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


def _run_snapshot(*, publish: bool) -> int:
    """Build ``docs/index.html`` and, when ``publish=True``, also commit + push.

    Returns 0 on success, non-zero on failure. We treat this as a side-channel
    step that should never bubble up an exception into the parent ``--board``
    run — any failure prints ``[snapshot] ERROR ...`` and returns non-zero so
    the caller can decide what to do (the daily scheduled task already logs
    process exit codes, so a non-zero here surfaces in ``logs/scheduled.log``).
    """
    from app.snapshot import build_snapshot

    try:
        result = build_snapshot()
    except Exception as exc:  # noqa: BLE001 - never crash the parent run
        print(
            f"[snapshot] ERROR build failed: {type(exc).__name__}: {exc}",
            flush=True,
        )
        return 1

    print(
        f"[snapshot] wrote {result['path']} "
        f"({result['bytes'] / 1024:.1f} KB, "
        f"{result['total']} articles across {len(result['boards'])} boards)",
        flush=True,
    )
    for key, n in result["boards"].items():
        print(f"[snapshot]   {key:14s} {n:>3} articles", flush=True)

    if not publish:
        return 0

    return _git_publish_docs(Path(result["path"]).parent)


def _git_publish_docs(docs_dir: Path) -> int:
    """``git add docs/ && git commit -m ... && git push origin HEAD``.

    All git invocations run with UTF-8 output decoding so Chinese commit
    messages / file paths don't blow up on Windows code pages. Each step
    prints a ``[snapshot]`` prefixed status line so the daily log is readable.

    Idempotent: if ``git status --porcelain docs/`` is empty, we print
    "nothing to publish" and return 0.
    """
    import subprocess

    def _run(
        argv: list[str], *, cwd: Path | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        # encoding="utf-8" + errors="replace" guarantees we never crash on
        # decoding (Windows default cp936 mangles utf-8 commit messages).
        return subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=check,
        )

    try:
        top = _run(
            ["git", "rev-parse", "--show-toplevel"], cwd=docs_dir.parent
        ).stdout.strip()
        if not top:
            print("[snapshot] ERROR git rev-parse returned empty path", flush=True)
            return 1
        repo_root = Path(top)
    except FileNotFoundError:
        print(
            "[snapshot] ERROR git not found on PATH — install Git or skip --snapshot-publish",
            flush=True,
        )
        return 1
    except subprocess.CalledProcessError as e:
        print(
            f"[snapshot] ERROR not inside a git repo: {e.stderr.strip() or e.stdout.strip()}",
            flush=True,
        )
        return 1

    docs_rel = docs_dir.resolve().relative_to(repo_root)

    try:
        status = _run(
            ["git", "status", "--porcelain", "--", str(docs_rel)],
            cwd=repo_root,
        ).stdout
    except subprocess.CalledProcessError as e:
        print(
            f"[snapshot] ERROR git status failed: {e.stderr.strip() or e.stdout.strip()}",
            flush=True,
        )
        return 1

    if not status.strip():
        print(
            "[snapshot] nothing to publish (docs/ unchanged since last commit)",
            flush=True,
        )
        return 0

    iso_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    commit_msg = f"snapshot {iso_date}"

    try:
        _run(["git", "add", "--", str(docs_rel)], cwd=repo_root)
        print(f"[snapshot] git add {docs_rel}", flush=True)
    except subprocess.CalledProcessError as e:
        print(
            f"[snapshot] ERROR git add failed: {e.stderr.strip() or e.stdout.strip()}",
            flush=True,
        )
        return 1

    try:
        commit_res = _run(
            ["git", "commit", "-m", commit_msg], cwd=repo_root, check=False
        )
    except subprocess.CalledProcessError as e:
        # Shouldn't happen with check=False, but be safe.
        print(
            f"[snapshot] ERROR git commit raised: {e.stderr.strip() or e.stdout.strip()}",
            flush=True,
        )
        return 1
    if commit_res.returncode != 0:
        # Common cause: nothing actually staged because the changes were
        # already-tracked-but-identical. Treat as success.
        out = (commit_res.stdout + commit_res.stderr).lower()
        if "nothing to commit" in out or "nothing added" in out:
            print(
                "[snapshot] git commit: nothing to commit (no diff after add)",
                flush=True,
            )
            return 0
        print(
            f"[snapshot] ERROR git commit failed:\n{commit_res.stdout}{commit_res.stderr}",
            flush=True,
        )
        return 1
    print(f"[snapshot] git commit -m '{commit_msg}'", flush=True)

    try:
        push_res = _run(
            ["git", "push", "origin", "HEAD"], cwd=repo_root, check=False
        )
    except subprocess.CalledProcessError as e:
        print(
            f"[snapshot] ERROR git push raised: {e.stderr.strip() or e.stdout.strip()}",
            flush=True,
        )
        return 1
    if push_res.returncode != 0:
        print(
            f"[snapshot] ERROR git push failed:\n{push_res.stdout}{push_res.stderr}",
            flush=True,
        )
        return 1
    print("[snapshot] git push origin HEAD — DONE", flush=True)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="TrendRadarCN launcher")
    parser.add_argument(
        "--host",
        default=os.getenv("TRENDRADAR_HOST", "0.0.0.0"),
        help="Bind address. 0.0.0.0 = listen on all interfaces so other "
        "devices on your LAN (phone/tablet) can reach the dashboard. Use "
        "127.0.0.1 to lock down to localhost only.",
    )
    parser.add_argument("--port", type=int, default=int(os.getenv("TRENDRADAR_PORT", "8001")))
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
        "--backfill-summaries",
        action="store_true",
        help="Summarize all articles that don't yet have an LLM summary "
        "(in boards with summarizer: llm). Combine with --force to "
        "re-summarize everything.",
    )
    parser.add_argument(
        "--reset-summaries",
        metavar="KEY",
        help="Clear llm_summary for the given board key (or 'all'). Use "
        "before --backfill-summaries to re-generate every summary from scratch.",
    )
    parser.add_argument(
        "--apply-llm-cluster",
        metavar="KEY",
        help="Run LLM semantic clustering on existing data for board KEY "
        "(or 'all') and persist the result to articles.llm_cluster_id. "
        "After this, both the email digest and the dashboard read the same "
        "merged groups. Useful after changing the LLM prompt to re-cluster "
        "historical articles. Combine with --force to NULL out previous "
        "llm_cluster_id values first.",
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
    parser.add_argument(
        "--digest-preview",
        action="store_true",
        help="Build the email digest (hours=72) and print subject, per-board "
        "counts, and the first 3 plain-text items per board. Does NOT send "
        "mail — useful to verify LLM event clustering without spending an "
        "SMTP send.",
    )
    parser.add_argument(
        "--digest-send",
        action="store_true",
        help="Build the email digest (hours=72) from EXISTING data and send "
        "it via SMTP. Does NOT re-crawl. Useful for testing email rendering "
        "or the GitHub Pages link without waiting 5 min for a full crawl.",
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="Generate docs/index.html (a static, self-contained dashboard "
        "snapshot) and exit. Suitable for serving via GitHub Pages so the "
        "dashboard works from any network (incl. inside a 126/Gmail webmail "
        "click). Does NOT push to git — use --snapshot-publish for that.",
    )
    parser.add_argument(
        "--snapshot-publish",
        action="store_true",
        help="Like --snapshot, then `git add docs/ && git commit && git push "
        "origin HEAD`. Combine with --board to wire into the daily scheduled "
        "task. Idempotent: when docs/ is unchanged, prints 'nothing to "
        "publish' and exits 0.",
    )
    args = parser.parse_args()

    if args.digest_preview:
        from app.digest import build_digest

        print("[digest-preview] building 72h digest...", flush=True)
        digest = build_digest(hours=72)
        print(f"\nSubject: {digest['subject']}", flush=True)
        print(f"Total rendered groups: {digest['total_articles']}", flush=True)
        print("\nPer-board counts:", flush=True)
        for key, count in digest["board_counts"].items():
            stats = digest.get("llm_cluster_stats", {}).get(key) or {}
            if stats.get("used_llm"):
                cost = stats.get("cost_usd", 0.0) or 0.0
                in_tok = stats.get("prompt_tokens", 0)
                out_tok = stats.get("completion_tokens", 0)
                line = (
                    f"  {key:14s} {count:>3} groups  "
                    f"(llm-cluster: input={stats.get('n_input', 0)} "
                    f"groups={stats.get('n_groups', 0)} "
                    f"in={in_tok} out={out_tok} usd={cost:.5f})"
                )
            elif stats.get("persisted"):
                line = (
                    f"  {key:14s} {count:>3} groups  "
                    f"(persisted llm_cluster_id: input={stats.get('n_input', 0)})"
                )
            else:
                err = stats.get("error")
                tail = f"  (no LLM cluster: {err})" if err else "  (no LLM cluster)"
                line = f"  {key:14s} {count:>3} groups{tail}"
            print(line, flush=True)
        # Print the first 3 plain-text rows of each board section.
        print("\n--- Sample (first 3 items per board) ---\n", flush=True)
        text_body = digest["text"]
        # Split on the "## " section headers and replay the first 3 rows.
        sections = text_body.split("\n\n## ")
        for i, section in enumerate(sections[1:], start=1):
            header_line, _, body = section.partition("\n")
            print(f"## {header_line}", flush=True)
            # Items are 3 lines each (title / summary / url) joined by "\n  • "
            # — simplest robust way is to split on the bullet marker.
            items = body.split("\n  • ")
            for item in items[:3]:
                item = item.strip("\n")
                if not item:
                    continue
                if not item.startswith("•") and not item.startswith("  •"):
                    item = "  • " + item
                print(item, flush=True)
            print("", flush=True)
        return

    if args.digest_send:
        from app.digest import build_digest
        from app.mailer import is_configured, send_mail

        if not is_configured():
            print(
                "ERROR: SMTP not configured. Required env vars: "
                "SMTP_HOST, SMTP_USER, SMTP_PASS, SMTP_TO",
                flush=True,
            )
            sys.exit(1)
        print("[digest-send] building 72h digest from existing data...", flush=True)
        digest = build_digest(hours=72)
        print(
            f"[digest-send] subject: {digest['subject']}  "
            f"groups: {digest['total_articles']}",
            flush=True,
        )
        for key, count in digest["board_counts"].items():
            stats = digest.get("llm_cluster_stats", {}).get(key) or {}
            if stats.get("used_llm"):
                print(
                    f"[digest-send] {key:14s} {count:>3} groups  "
                    f"(llm-cluster: {stats.get('n_input', 0)}→"
                    f"{stats.get('n_groups', 0)} usd={stats.get('cost_usd', 0):.5f})",
                    flush=True,
                )
            else:
                print(f"[digest-send] {key:14s} {count:>3} groups", flush=True)
        try:
            send_mail(
                subject=digest["subject"],
                html=digest["html"],
                text=digest["text"],
            )
            print(f"[digest-send] OK: sent to {os.getenv('SMTP_TO')}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[digest-send] ERROR: {type(e).__name__}: {e}", flush=True)
            sys.exit(1)
        return

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

    if args.reset_summaries:
        from app.boards import load_boards
        from app.boards.service import reset_board_summaries
        from app.db import init_db

        init_db()
        targets = (
            [b.key for b in load_boards()]
            if args.reset_summaries == "all"
            else [args.reset_summaries]
        )
        total = 0
        for key in targets:
            n = reset_board_summaries(key)
            total += n
            print(f"  reset[{key}]: cleared llm_summary on {n} rows", flush=True)
        print(
            f"\n完成。共清空 {total} 篇文章的 LLM 摘要。"
            f"现在跑 `python run.py --backfill-summaries` 重新生成摘要。",
            flush=True,
        )
        return

    if args.apply_llm_cluster:
        from app.boards import load_boards
        from app.boards.service import (
            apply_llm_clustering,
            reset_board_llm_clusters,
        )
        from app.db import init_db

        init_db()
        targets = (
            [b.key for b in load_boards()]
            if args.apply_llm_cluster == "all"
            else [args.apply_llm_cluster]
        )
        if args.force:
            print("--force: 先清空所有目标板块的 llm_cluster_id…", flush=True)
            for key in targets:
                n = reset_board_llm_clusters(key)
                print(
                    f"  reset[{key}]: cleared llm_cluster_id on {n} rows",
                    flush=True,
                )
            print("", flush=True)

        async def _apply_all() -> dict[str, dict[str, object]]:
            out: dict[str, dict[str, object]] = {}
            for key in targets:
                out[key] = await apply_llm_clustering(key, hours=72, verbose=True)
            return out

        results = asyncio.run(_apply_all())
        total_updated = 0
        total_cost = 0.0
        for key, stats in results.items():
            cost = float(stats.get("cost_usd", 0.0) or 0.0)
            total_cost += cost
            total_updated += int(stats.get("n_updated", 0) or 0)
            err = stats.get("error")
            print(
                f"  [llm-cluster] {key:14s} input={stats.get('n_input', 0)} "
                f"groups={stats.get('n_groups', 0)} "
                f"updated={stats.get('n_updated', 0)} "
                f"cost=${cost:.5f}"
                + (f"  err={err}" if err else ""),
                flush=True,
            )
        print(
            f"\n完成。共更新 {total_updated} 篇文章的 llm_cluster_id, "
            f"LLM 成本 ${total_cost:.5f}。",
            flush=True,
        )
        return

    if args.backfill_summaries:
        from app.boards import load_boards
        from app.boards.service import (
            backfill_summaries,
            reset_board_summaries,
        )
        from app.db import init_db

        init_db()
        if args.force:
            print("--force: 先清空所有板块的现有 LLM 摘要…", flush=True)
            total_reset = 0
            for b in load_boards():
                if b.summarizer != "llm":
                    continue
                n = reset_board_summaries(b.key)
                print(
                    f"  reset[{b.key}]: cleared {n} rows",
                    flush=True,
                )
                total_reset += n
            print(f"  共清空 {total_reset} 行\n", flush=True)
        print("正在为没有 LLM 摘要的文章生成摘要…", flush=True)
        summary = asyncio.run(backfill_summaries(force=args.force, verbose=True))
        total_sum = sum(s["summarized"] for s in summary.values())
        total_cost = sum(
            (s.get("stats") or {}).get("cost_usd", 0.0) for s in summary.values()
        )
        cost_line = (
            f"  LLM 成本: ${total_cost:.5f}\n" if total_cost > 0 else ""
        )
        print(
            f"\n完成。共生成 {total_sum} 篇 LLM 摘要。\n{cost_line}",
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
                    for bkey, stats in (digest.get("llm_cluster_stats") or {}).items():
                        if not stats.get("used_llm"):
                            continue
                        cost = stats.get("cost_usd", 0.0) or 0.0
                        print(
                            f"[email] llm-cluster[{bkey}]: "
                            f"input={stats.get('n_input', 0)} "
                            f"groups={stats.get('n_groups', 0)} "
                            f"in={stats.get('prompt_tokens', 0)} "
                            f"out={stats.get('completion_tokens', 0)} "
                            f"usd={cost:.5f}",
                            flush=True,
                        )
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

        # Snapshot AFTER email: a publish failure (e.g. transient git push
        # error) shouldn't prevent the email from going out, and a successful
        # snapshot is a no-op when the DB didn't change. Order: board → email
        # → snapshot, matching the deliverable spec.
        if args.snapshot or args.snapshot_publish:
            _run_snapshot(publish=args.snapshot_publish)
        return

    # Standalone snapshot path: --snapshot / --snapshot-publish without --board.
    # Useful for "just regenerate the static page from current DB state" runs,
    # and for the very first manual publish to seed docs/ on the main branch.
    if args.snapshot or args.snapshot_publish:
        from app.db import init_db

        init_db()
        rc = _run_snapshot(publish=args.snapshot_publish)
        if rc != 0:
            sys.exit(rc)
        return

    import uvicorn

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
