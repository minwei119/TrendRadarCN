from app.db import SessionLocal
from app.models import Article

with SessionLocal() as s:
    rows = (
        s.query(Article)
         .filter(Article.board_key == "ai-frontier",
                 Article.llm_summary.isnot(None))
         .order_by(Article.fetched_at.desc())
         .limit(10)
         .all()
    )
    for r in rows:
        print("=" * 78)
        print("T:", r.title[:100])
        print("O:", (r.summary or "")[:120].replace("\n", " "))   # 原始摘要
        print("L:", r.llm_summary[:160])                          # LLM 摘要
