# Tests the LLM cluster + persistence fix end to end.
# Run after changes to verify both email and dashboard are deduplicated.
#
# Usage:
#   .\scripts\test_llm_cluster_fix.ps1                      # default: my-portfolio
#   .\scripts\test_llm_cluster_fix.ps1 -Board ai-cn         # any board key
#   .\scripts\test_llm_cluster_fix.ps1 -Board all           # every board
param(
    [string]$Board = "my-portfolio"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root

$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Host "ERROR: $Py not found. Did you run scripts\setup.ps1 first?" -ForegroundColor Red
    exit 1
}

Write-Host "=== Step 1: Re-cluster (uses LLM, ~5 sec/board) ===" -ForegroundColor Cyan
& $Py run.py --apply-llm-cluster $Board --force
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: --apply-llm-cluster failed (exit=$LASTEXITCODE)" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "`n=== Step 2: Check llm_cluster_id was assigned ===" -ForegroundColor Cyan
$inspectScript = @"
from app.db import SessionLocal
from app.models import Article
from sqlalchemy import select, and_
from datetime import datetime, timedelta, timezone

board = '$Board'
cutoff = datetime.now(timezone.utc) - timedelta(hours=72)
with SessionLocal() as db:
    if board == 'all':
        stmt = select(Article).where(Article.fetched_at >= cutoff).order_by(Article.board_key, Article.llm_cluster_id, Article.id)
    else:
        stmt = select(Article).where(and_(Article.board_key == board, Article.fetched_at >= cutoff)).order_by(Article.llm_cluster_id, Article.id)
    rows = db.execute(stmt).scalars().all()

print(f'Total articles: {len(rows)}')

by_board = {}
for r in rows:
    by_board.setdefault(r.board_key, []).append(r)

for bkey, brows in by_board.items():
    print(f'\n=== board={bkey} (rows={len(brows)}) ===')
    by_lcid = {}
    for r in brows:
        by_lcid.setdefault(r.llm_cluster_id, []).append(r)
    nulls = by_lcid.pop(None, [])
    print(f'Distinct llm_cluster_id values: {len(by_lcid)}')
    if nulls:
        print(f'WARNING: {len(nulls)} articles still have llm_cluster_id=NULL')
    print('--- Top 5 largest LLM clusters (should be merged groups) ---')
    for lcid, group in sorted(by_lcid.items(), key=lambda kv: -len(kv[1]))[:5]:
        print(f'\nlcid={lcid}  size={len(group)}')
        for art in group[:5]:
            title = (art.title or '')[:80]
            print(f'  - {title}  (src={art.source_label})')
"@
& $Py -c $inspectScript
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: inspection script failed (exit=$LASTEXITCODE)" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "`n=== Step 3: Build digest, verify counts dropped ===" -ForegroundColor Cyan
& $Py run.py --digest-preview

Write-Host "`n=== Step 4: Spot-check dashboard ===" -ForegroundColor Cyan
Write-Host "Open http://127.0.0.1:8001 then click 主题板块 tab and pick: $Board" -ForegroundColor Yellow
Write-Host "If web server isn't running, start it in a new terminal:" -ForegroundColor Yellow
Write-Host "  .\.venv\Scripts\python.exe run.py" -ForegroundColor Yellow
