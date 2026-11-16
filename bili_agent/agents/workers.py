"""5개 워커의 정의: 시스템 프롬프트 + 도구 스키마 + 도구 구현(핸들러) + 제출 스키마.

모든 핸들러는 (workspace, **tool_input) → JSON 직렬화 가능한 dict 를 돌려준다.
핸들러는 결정적 코드(tools/)만 호출하며, LLM 은 핸들러 결과를 읽고 판단만 한다.
"""
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .. import config
from ..tools import bilibili_api, metrics
from ..workspace import Workspace

Handler = Callable[..., Dict[str, Any]]


def tool(name: str, description: str, props: Optional[Dict] = None, required: Optional[List[str]] = None) -> Dict:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": props or {}, "required": required or []}}


GAME_ID_PROP = {"game_id": {"type": "string",
                            "description": "표준 game_id(예: league_of_legends). 생략하면 전체 게임 풀."}}
STR_LIST = {"type": "array", "items": {"type": "string"}}


@dataclass
class WorkerSpec:
    name: str
    prompt_file: str
    tools: List[Dict]
    handlers: Dict[str, Handler]
    submit_tool: Dict
    needs_data: bool = True  # True 면 collector 실행 전에는 호출 불가

    @property
    def system_prompt(self) -> str:
        return (config.PROMPT_DIR / self.prompt_file).read_text(encoding="utf-8")

    @property
    def all_tools(self) -> List[Dict]:
        return self.tools + [self.submit_tool]


# ── 공용 핸들러 ────────────────────────────────────────────

def _game_label(ws: Workspace, title: str) -> List[str]:
    return [g["ko"] for g in ws.alias.find_in_text(title)]


def h_get_game_scores(ws: Workspace, top: int = 8) -> Dict:
    scores = metrics.score_games(ws.videos, ws.keywords, ws.alias)
    ws.cache["game_scores"] = scores
    return {"formula": "Score_bili = 수요 + 아웃라이어 + 신선도 − 경쟁도 (각 0~1, 동일 가중치)",
            "games": scores[:top]}


def h_get_top_titles(ws: Workspace, n: int = 10, game_id: Optional[str] = None) -> Dict:
    pool = ws.videos_for(game_id)
    ranked = metrics.rank_by_engagement(pool, top=n)
    by_bvid = {v["bvid"]: v for v in pool}
    rows = []
    for r in ranked["top"]:
        v = by_bvid[r["bvid"]]
        rows.append({"bvid": r["bvid"], "title": r["title"], "view": r["view"],
                     "engagement_multiple": round(r["engagement_multiple"], 2),
                     "type": metrics.classify_type(r["title"]), "games_ko": _game_label(ws, r["title"]),
                     "in_official_activity": bool(v.get("mission_id"))})
    return {"pool_size": len(pool), "titles": rows}


# ── collector ────────────────────────────────────────────

def h_resolve_game(ws: Workspace, name: str) -> Dict:
    g = ws.alias.resolve(name) or next(iter(ws.alias.find_in_text(name)), None)
    if not g:
        return {"found": False, "name": name, "note": "별칭 사전에 없음"}
    return {"found": True, "game_id": g["game_id"], "zh": g["zh"], "ko": g["ko"], "en": g["en"]}


def h_collect_popular_games(ws: Workspace, pages: int = config.POPULAR_PAGES) -> Dict:
    data = bilibili_api.fetch_popular_game_videos(pages=min(max(int(pages), 1), 10))
    added = ws.add_videos(data["videos"])
    ws.add_source("종합 인기(게임 카테고리)", data)
    req = ws.requested_game
    return {"origin": data["origin"], "snapshot_file": data.get("snapshot_file"),
            "live_error": data.get("live_error"), "game_videos_added": added,
            "pool_size": len(ws.videos),
            "videos_for_requested_game": len(ws.videos_for(req["game_id"])) if req else None,
            "sample_titles": [v["title"] for v in data["videos"][:5]]}


def h_collect_trending(ws: Workspace) -> Dict:
    data = bilibili_api.fetch_trending_keywords()
    ws.keywords = data["keywords"]
    ws.add_source("실시간 핫서치", data)
    game_related = [k["keyword"] for k in ws.keywords if ws.alias.find_in_text(k["keyword"])]
    return {"origin": data["origin"], "count": len(ws.keywords),
            "top10": [k["keyword"] for k in ws.keywords[:10]], "game_related": game_related}


def h_list_unmatched_titles(ws: Workspace, n: int = 20) -> Dict:
    """별칭 사전으로 게임을 못 찾은 영상 제목 (LLM 이 새 별칭 후보를 제안하는 데 사용)."""
    rows = [{"title": v["title"], "tname": v.get("tname_v2") or v.get("tname"), "view": v["view"]}
            for v in ws.videos if not ws.alias.find_in_text(v["title"])]
    rows.sort(key=lambda r: -r["view"])
    return {"unmatched": len(rows), "pool_size": len(ws.videos), "titles": rows[:n]}


def h_load_ranking_snapshot(ws: Workspace) -> Dict:
    data = bilibili_api.load_ranking_snapshot()
    if data is None:
        return {"available": False,
                "note": "data/snapshots/ranking_game_*.json 없음. 랭킹 API는 봇 차단(-352)이라 자동 수집하지 않음."}
    added = ws.add_videos(data["videos"])
    ws.add_source("게임 카테고리 랭킹(스냅샷)", data)
    return {"available": True, "snapshot_file": data["snapshot_file"], "videos": len(data["videos"]),
            "added": added, "error": data.get("error")}


# ── trend_radar ──────────────────────────────────────────

def h_get_hot_keyword_signals(ws: Workspace) -> Dict:
    sig = metrics.hot_keyword_signals(ws.keywords, ws.videos, ws.alias)
    ws.cache["hot_keyword_signals"] = sig
    return {"total_keywords": len(ws.keywords), "game_related": len(sig), "signals": sig}


def h_get_event_signals(ws: Workspace) -> Dict:
    ev = metrics.event_signals(ws.videos, ws.alias)
    ws.cache["event_signals"] = ev
    share = sum(r["videos"] for r in ev) / max(len(ws.videos), 1)
    return {"activity_video_share": round(share, 3), "events": ev[:10]}


# ── engagement_analyst ───────────────────────────────────

def h_get_engagement_ranking(ws: Workspace, game_id: Optional[str] = None, top: int = 10) -> Dict:
    return metrics.rank_by_engagement(ws.videos_for(game_id), top=top)


def h_get_duration_type_distribution(ws: Workspace, game_id: Optional[str] = None) -> Dict:
    dist = metrics.duration_and_type_distribution(ws.videos_for(game_id))
    ws.cache["distribution:%s" % (game_id or "all")] = dist
    return dist


# ── title_doctor ─────────────────────────────────────────

def h_get_title_benchmark(ws: Workspace, game_id: Optional[str] = None) -> Dict:
    bench = metrics.title_benchmark(ws.videos_for(game_id))
    ws.cache["title_benchmark"] = bench
    return bench


def h_diagnose_title(ws: Workspace, title: str) -> Dict:
    bench = ws.cache.get("title_benchmark") or metrics.title_benchmark(ws.videos)
    diag = metrics.diagnose_title(title, bench)
    ws.cache["title_diagnosis"] = diag
    return diag


# ── 워커 명세 ─────────────────────────────────────────────

GET_SCORES = tool("get_game_scores", "게임별 소재 점수(Score_bili)와 구성 요소를 점수 순으로 반환.",
                  {"top": {"type": "integer", "description": "상위 몇 개(기본 8)"}})
GET_TOP_TITLES = tool("get_top_titles", "반응률 상위 영상의 실제 제목·조회수·배수·유형.",
                      {"n": {"type": "integer", "description": "개수(기본 10)"}, **GAME_ID_PROP})


def submit(props: Dict, required: List[str]) -> Dict:
    return tool("submit_findings", "작업 결과를 서브 슈퍼바이저에게 제출하고 종료한다. 반드시 마지막에 한 번 호출.",
                {"summary_ko": {"type": "string", "description": "한국어 요약"}, **props},
                ["summary_ko"] + required)


WORKERS: Dict[str, WorkerSpec] = {
    "collector": WorkerSpec(
        name="collector", prompt_file="collector.md", needs_data=False,
        tools=[
            tool("collect_popular_games", "종합 인기 목록에서 게임 카테고리 영상을 수집해 작업 공간에 저장.",
                 {"pages": {"type": "integer", "description": "1~10 페이지(페이지당 50개, 기본 5)"}}),
            tool("collect_trending", "빌리빌리 실시간 핫서치 키워드(최대 30개) 수집."),
            tool("load_ranking_snapshot", "사람이 저장한 게임 카테고리 랭킹 스냅샷이 있으면 불러온다."),
            tool("resolve_game", "게임 이름(한·중·영 아무 표기)을 별칭 사전으로 표준화.",
                 {"name": {"type": "string"}}, ["name"]),
            tool("list_unmatched_titles", "별칭 사전으로 게임을 찾지 못한 영상 제목(조회수 순).",
                 {"n": {"type": "integer", "description": "개수(기본 20)"}}),
        ],
        handlers={"collect_popular_games": h_collect_popular_games, "collect_trending": h_collect_trending,
                  "load_ranking_snapshot": h_load_ranking_snapshot, "resolve_game": h_resolve_game,
                  "list_unmatched_titles": h_list_unmatched_titles},
        submit_tool=submit({"data_quality_notes": STR_LIST, "alias_candidates": {
            "type": "array", "description": "별칭 사전에 추가를 제안할 게임(사람 검토 후 반영)",
            "items": {"type": "object", "properties": {
                "zh": {"type": "string"}, "ko": {"type": "string"}, "en": {"type": "string"},
                "aliases": STR_LIST, "evidence_title": {"type": "string"}},
                "required": ["zh", "evidence_title"]}}}, ["data_quality_notes"]),
    ),
    "trend_radar": WorkerSpec(
        name="trend_radar", prompt_file="trend_radar.md",
        tools=[tool("get_hot_keyword_signals", "핫서치 중 게임 관련 키워드와 선행 신호 여부."),
               tool("get_event_signals", "공식 활동(mission_id) 참여 영상을 게임별로 묶은 결과."),
               GET_SCORES],
        handlers={"get_hot_keyword_signals": h_get_hot_keyword_signals,
                  "get_event_signals": h_get_event_signals, "get_game_scores": h_get_game_scores},
        submit_tool=submit({"events": {"type": "array", "items": {"type": "object", "properties": {
            "game_zh": {"type": "string"}, "game_ko": {"type": "string"},
            "kind": {"type": "string", "enum": ["official_activity", "hot_keyword", "leading_signal"]},
            "evidence": {"type": "string"}, "recommendation_ko": {"type": "string"}},
            "required": ["game_zh", "kind", "evidence", "recommendation_ko"]}}}, ["events"]),
    ),
    "engagement_analyst": WorkerSpec(
        name="engagement_analyst", prompt_file="engagement_analyst.md",
        tools=[tool("get_engagement_ranking", "반응률 상위 영상과 풀 중앙값 대비 배수.",
                    {**GAME_ID_PROP, "top": {"type": "integer"}}),
               tool("get_duration_type_distribution", "길이 구간·유형별 개수와 조회수 중앙값.", GAME_ID_PROP),
               GET_SCORES],
        handlers={"get_engagement_ranking": h_get_engagement_ranking,
                  "get_duration_type_distribution": h_get_duration_type_distribution,
                  "get_game_scores": h_get_game_scores},
        submit_tool=submit({
            "opportunities": {"type": "array", "items": {"type": "object", "properties": {
                "game_zh": {"type": "string"}, "game_ko": {"type": "string"}, "reason_ko": {"type": "string"}},
                "required": ["game_zh", "reason_ko"]}},
            "duration_advice": {"type": "object", "properties": {
                "recommended_bucket": {"type": "string"}, "reason_ko": {"type": "string"}},
                "required": ["recommended_bucket", "reason_ko"]}},
            ["opportunities", "duration_advice"]),
    ),
    "title_doctor": WorkerSpec(
        name="title_doctor", prompt_file="title_doctor.md",
        tools=[tool("get_title_benchmark", "현재 인기 풀의 제목 통계.", GAME_ID_PROP),
               tool("diagnose_title", "제목 하나를 벤치마크와 비교 진단.", {"title": {"type": "string"}}, ["title"]),
               GET_TOP_TITLES],
        handlers={"get_title_benchmark": h_get_title_benchmark, "diagnose_title": h_diagnose_title,
                  "get_top_titles": h_get_top_titles},
        submit_tool=submit({"issues_ko": STR_LIST, "rewrite_suggestions_zh": STR_LIST},
                           ["issues_ko", "rewrite_suggestions_zh"]),
    ),
    "localizer": WorkerSpec(
        name="localizer", prompt_file="localizer.md",
        tools=[GET_TOP_TITLES],
        handlers={"get_top_titles": h_get_top_titles},
        submit_tool=submit({"cards": {"type": "array", "items": {"type": "object", "properties": {
            "source_bvid": {"type": "string"}, "title_zh": {"type": "string"}, "title_ko": {"type": "string"},
            "slang_note_ko": {"type": "string"}, "why_ko": {"type": "string"}, "adapt_tip_ko": {"type": "string"}},
            "required": ["source_bvid", "title_zh", "title_ko", "why_ko"]}}}, ["cards"]),
    ),
}
