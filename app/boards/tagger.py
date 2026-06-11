"""Rule-based article tagging.

A board declares tags + keyword lists in boards.yaml; we substring-match
each article's title+summary against the keyword list and collect every tag
that fires. An article can carry multiple tags (e.g. both "港股" and "财报").
No ML, no LLM — just lowercase substring search, kept fast and explainable.
"""
from __future__ import annotations


def tag_article(text: str, tag_keywords: dict[str, list[str]]) -> list[str]:
    """Return the sorted, deduped list of tags whose keywords appear in text.

    Args:
      text: usually ``title + " " + summary`` of the article.
      tag_keywords: ``{tag_name: [keyword, ...]}`` — substring match, case-
        insensitive. Numbers in YAML are tolerated (stringified upstream).
    """
    if not text or not tag_keywords:
        return []
    haystack = text.lower()
    hit: list[str] = []
    for name, kws in tag_keywords.items():
        for kw in kws:
            kw_str = str(kw).lower().strip()
            if kw_str and kw_str in haystack:
                hit.append(name)
                break  # one keyword is enough; move on to next tag
    return sorted(set(hit))
