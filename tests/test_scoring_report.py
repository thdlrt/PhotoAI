from __future__ import annotations

import html
from pathlib import Path

from landscape_culler.scoring import _write_html_report


def _item(
    tmp_path: Path,
    *,
    name: str,
    rating: int,
    score: float,
    group_id: int,
    group_size: int,
    keywords: list[str] | None = None,
) -> dict:
    preview = tmp_path / "预览图" / f"group-{group_id}-{rating}.jpg"
    preview.parent.mkdir(exist_ok=True)
    preview.write_bytes(b"preview")
    return {
        "path": str(tmp_path / "待处理<&\"" / f"{name}.ARW"),
        "preview": str(preview),
        "rating": rating,
        "score": score,
        "group_id": group_id,
        "group_size": group_size,
        "keywords": keywords or ["AI|候选"],
    }


def test_report_shows_all_ratings_grouped_and_sorted(tmp_path: Path) -> None:
    payload = {
        "input_root": "X:\\camera\\temp\\处理中<&\"",
        "image_count": 4,
        "candidate_count": 2,
        "strong_count": 1,
        "results": [
            _item(tmp_path, name="零星", rating=0, score=0.9, group_id=2, group_size=3),
            _item(tmp_path, name="三星", rating=3, score=0.1, group_id=2, group_size=3),
            _item(tmp_path, name="四星", rating=4, score=0.2, group_id=2, group_size=3),
            _item(tmp_path, name="单张", rating=3, score=0.5, group_id=1, group_size=1),
        ],
    }
    report = tmp_path / "report.html"

    _write_html_report(report, payload)

    document = report.read_text(encoding="utf-8")
    assert document.count('class="card rating-') == 4
    assert 'class="card rating-0"' in document
    assert "0★ 未入选" in document
    assert document.index('data-group-id="1"') < document.index('data-group-id="2"')
    group_two = document[document.index('data-group-id="2"') :]
    assert group_two.index("四星.ARW") < group_two.index("三星.ARW") < group_two.index("零星.ARW")
    assert "单张组 · 参与全局比较" in document
    assert "按全局候选统一评分" in document
    assert 'data-filter="all"' in document
    assert 'data-filter="rejected"' in document
    assert "已显示 4 张" in document


def test_report_escapes_text_and_uses_local_file_uri(tmp_path: Path) -> None:
    dangerous_keyword = '<script>alert("keyword")</script>'
    item = _item(
        tmp_path,
        name="中文<&\"",
        rating=0,
        score=-0.25,
        group_id=7,
        group_size=1,
        keywords=[dangerous_keyword],
    )
    payload = {
        "input_root": '<script>alert("root")</script>',
        "image_count": 1,
        "candidate_count": 0,
        "strong_count": 0,
        "results": [item],
    }
    report = tmp_path / "report.html"

    _write_html_report(report, payload)

    document = report.read_text(encoding="utf-8")
    assert dangerous_keyword not in document
    assert html.escape(dangerous_keyword, quote=True) in document
    assert '<script>alert("root")</script>' not in document
    assert html.escape(payload["input_root"], quote=True) in document
    preview_uri = Path(item["preview"]).as_uri()
    assert f'href="{html.escape(preview_uri, quote=True)}"' in document
    assert f'src="{html.escape(preview_uri, quote=True)}"' in document
    assert "中文" in document
