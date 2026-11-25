"""에이전트 1 · 수집 (기획안 12장 '1 수집')

데이터 우선순위
  1) Aside CSV (data/aside/)  — 랭킹·화제 태그·공식 활동까지 있는 실제 브라우저 수집 결과
  2) 공개 API                  — 종합 인기(게임 카테고리)·실시간 핫서치 (Aside 가 없거나 오래됐을 때)
  3) 랭킹 스냅샷 JSON          — 사람이 브라우저로 저장한 ranking/v2 원본
LLM 이 하는 일: 어떤 출처를 쓸지 판단하고, 출처·신선도·표본 크기의 문제를 보고한다.
DB(13장): raw_snapshots
"""
from typing import Dict

from ... import config
from ...tools import aside_import, bilibili_api
from ...workspace import Workspace
from ..base import STR_LIST, WorkerSpec, submit, tool


def _snapshot(ws: Workspace, source: str, meta: Dict, count: int) -> None:
    ws.cache.setdefault("raw_snapshots", []).append({
        "source": source, "origin": meta.get("origin"), "file": meta.get("file"),
        "fetched_at": meta.get("fetched_at"), "count": count})


def h_load_aside(ws: Workspace) -> Dict:
    """Aside CSV 를 읽는다. 오래된 파일(ASIDE_MAX_AGE_H 초과)은 작업 공간에 넣지 않고 알려만 준다."""
    out = {"max_age_h": config.ASIDE_MAX_AGE_H}
    vids = aside_import.load_videos()
    if vids is None:
        out["videos"] = {"available": False, "note": "data/aside/*videos*.csv 없음"}
    elif vids["age_h"] > config.ASIDE_MAX_AGE_H:
        out["videos"] = {"available": True, "used": False, "stale": True, "age_h": vids["age_h"], "file": vids["file"]}
    else:
        added = ws.add_raw(vids["videos"])
        ws.add_source("Aside 영상", vids)
        _snapshot(ws, "aside", vids, added)
        out["videos"] = {"available": True, "used": True, "file": vids["file"], "age_h": vids["age_h"],
                         "encoding": vids["encoding"], "rows_total": vids["rows_total"],
                         "rows_non_game_dropped": vids["rows_non_game"], "game_videos_added": added,
                         "partition_column_found": vids["partition_known"],
                         "unmapped_columns": vids["unmapped_columns"][:10]}
    kws = aside_import.load_keywords()
    if kws and kws["age_h"] <= config.ASIDE_MAX_AGE_H and kws["keywords"]:
        ws.keywords = kws["keywords"]
        ws.add_source("Aside 핫키워드", kws)
        out["keywords"] = {"used": True, "count": len(kws["keywords"]), "file": kws["file"]}
    else:
        out["keywords"] = {"used": False, "reason": "없음" if kws is None else "오래됨 또는 비어 있음"}
    evs = aside_import.load_events()
    if evs and evs["age_h"] <= config.ASIDE_MAX_AGE_H:
        ws.aside_events = evs["events"]
        ws.add_source("Aside 공식 활동·화제", evs)
        out["events"] = {"used": True, "count": len(evs["events"]), "top3": [e["name"] for e in evs["events"][:3]]}
    else:
        out["events"] = {"used": False}
    return out


def h_collect_popular_games(ws: Workspace, pages: int = config.POPULAR_PAGES) -> Dict:
    data = bilibili_api.fetch_popular_game_videos(pages=min(max(int(pages), 1), 10))
    added = ws.add_raw(data["videos"])
    ws.add_source("공개 API · 종합 인기(게임)", data)
    _snapshot(ws, "api", data, added)
    return {"origin": data["origin"], "snapshot_file": data.get("snapshot_file"), "live_error": data.get("live_error"),
            "game_videos_added": added, "pool_size": len(ws.raw_videos)}


def h_collect_trending(ws: Workspace) -> Dict:
    if ws.keywords:
        return {"skipped": True, "reason": "이미 Aside 핫키워드가 있음", "count": len(ws.keywords)}
    data = bilibili_api.fetch_trending_keywords()
    ws.keywords = data["keywords"]
    ws.add_source("공개 API · 실시간 핫서치", data)
    return {"origin": data["origin"], "count": len(ws.keywords), "top10": [k["keyword"] for k in ws.keywords[:10]]}


def h_load_ranking_snapshot(ws: Workspace) -> Dict:
    data = bilibili_api.load_ranking_snapshot()
    if data is None:
        return {"available": False}
    added = ws.add_raw(data["videos"])
    ws.add_source("랭킹 스냅샷(사람 저장)", data)
    _snapshot(ws, "snapshot", data, added)
    return {"available": True, "file": data["snapshot_file"], "added": added, "error": data.get("error")}


SPEC = WorkerSpec(
    name="collector", stage="1 수집", db_tables=["raw_snapshots"], prompt_file="w1_collector.md",
    tools=[
        tool("load_aside", "Aside 브라우저가 내보낸 CSV(영상·핫키워드·공식 활동)를 읽는다. 1순위 데이터."),
        tool("collect_popular_games", "공개 API: 종합 인기 목록의 게임 카테고리 영상 (Aside 가 없거나 오래됐을 때).",
             {"pages": {"type": "integer", "description": "1~10 (페이지당 50개)"}}),
        tool("collect_trending", "공개 API: 실시간 핫서치 30개 (Aside 핫키워드가 없을 때만)."),
        tool("load_ranking_snapshot", "사람이 저장한 게임 카테고리 랭킹 JSON(ranking_game_*.json)이 있으면 추가."),
    ],
    handlers={"load_aside": h_load_aside, "collect_popular_games": h_collect_popular_games,
              "collect_trending": h_collect_trending, "load_ranking_snapshot": h_load_ranking_snapshot},
    submit_tool=submit({"data_quality_notes": STR_LIST}, ["data_quality_notes"]),
)
