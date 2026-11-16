"""빌리빌리 서브 슈퍼바이저.

총괄 슈퍼바이저(팀원 담당) ──BiliTask──▶ BiliSupervisor ──delegate──▶ 5개 워커
                           ◀──BiliReport──                ◀─findings──

- run()     : Claude 가 슈퍼바이저 역할로 워커 호출 순서를 스스로 결정 (실제 PoC)
- run_dry() : LLM 없이 intent별 기본 계획을 코드로 실행 (API 키 없이 데이터 파이프라인 확인·테스트용)
"""
import json
from typing import Dict, List, Optional

from .. import config
from ..contracts import BiliReport, BiliTask
from ..tools import metrics
from ..tools.alias_store import AliasStore
from ..workspace import Workspace
from .loop import LLM, run_tool_loop
from .workers import WORKERS, h_get_game_scores, h_get_top_titles

DEFAULT_PLANS = {
    "trend_scan": ["collector", "trend_radar", "engagement_analyst", "localizer"],
    "game_deep_dive": ["collector", "trend_radar", "engagement_analyst", "title_doctor", "localizer"],
    "title_check": ["collector", "title_doctor"],
    "event_watch": ["collector", "trend_radar"],
}

SUPERVISOR_TOOLS = [
    {"name": "delegate",
     "description": "워커 하나에게 일을 맡기고 그 결과(summary_ko + 구조화된 findings)를 받는다.",
     "input_schema": {"type": "object", "properties": {
         "worker": {"type": "string", "enum": list(WORKERS)},
         "instruction": {"type": "string", "description": "워커에게 줄 구체적인 지시(한국어). 워커는 이 대화를 볼 수 없다."}},
         "required": ["worker", "instruction"]}},
    {"name": "submit_report",
     "description": "최종 보고서를 총괄 슈퍼바이저에게 제출하고 종료한다. 반드시 마지막에 한 번 호출.",
     "input_schema": {"type": "object", "properties": {
         "summary_ko": {"type": "string", "description": "한국어 3~5문장, 이번 주 행동 제안 포함"},
         "warnings": {"type": "array", "items": {"type": "string"}}},
         "required": ["summary_ko"]}},
]


class BiliSupervisor:
    def __init__(self, llm: Optional[LLM] = None, alias_store: Optional[AliasStore] = None):
        self.llm = llm
        self.alias = alias_store or AliasStore.load()

    # ── LLM 슈퍼바이저 ─────────────────────────────────────
    def run(self, task: BiliTask) -> BiliReport:
        self.llm = self.llm or LLM()
        ws = Workspace(task, self.alias)
        ws.log("supervisor", "task", task.to_prompt().replace("\n", " | "))
        system = (config.PROMPT_DIR / "supervisor.md").read_text(encoding="utf-8")
        first = task.to_prompt() + "\n\n위 BiliTask를 처리하세요."
        result, warns = run_tool_loop(self.llm, ws, "supervisor", system, SUPERVISOR_TOOLS,
                                      {"delegate": self._delegate}, first, "submit_report",
                                      config.SUPERVISOR_MAX_STEPS)
        ws.warnings += warns
        if result is None:
            return build_report(ws, "슈퍼바이저가 보고서를 완성하지 못했습니다. warnings 를 확인하세요.")
        ws.warnings += result.get("warnings", [])
        return build_report(ws, result["summary_ko"])

    def _delegate(self, ws: Workspace, worker: str, instruction: str) -> Dict:
        spec = WORKERS.get(worker)
        if spec is None:
            raise ValueError("unknown worker %r" % worker)
        if spec.needs_data and not ws.collected:
            # 코드 수준 가드레일: 순서는 LLM 이 정하지만, 데이터 없이 분석하는 일은 막는다.
            raise RuntimeError("아직 수집된 데이터가 없습니다. 먼저 collector 를 호출하세요.")
        ws.log("supervisor", "delegate→" + worker, instruction)
        context = _worker_context(ws)
        findings, warns = run_tool_loop(self.llm, ws, worker, spec.system_prompt, spec.all_tools,
                                        spec.handlers, context + "\n\n[지시]\n" + instruction,
                                        "submit_findings", config.WORKER_MAX_STEPS)
        ws.warnings += warns
        if findings is None:
            return {"worker": worker, "ok": False, "warnings": warns}
        ws.findings[worker] = findings
        return {"worker": worker, "ok": True, "findings": findings}

    # ── LLM 없는 실행 (dry-run) ────────────────────────────
    def run_dry(self, task: BiliTask) -> BiliReport:
        """intent별 기본 계획을 코드로 실행. 문장은 템플릿이고, 숫자는 실제 계산 결과다."""
        ws = Workspace(task, self.alias)
        ws.log("supervisor(dry)", "task", task.to_prompt().replace("\n", " | "))
        gid = ws.requested_game["game_id"] if ws.requested_game else None
        for worker in DEFAULT_PLANS[task.intent]:
            ws.log("supervisor(dry)", "delegate→" + worker, "default plan")
            spec = WORKERS[worker]
            if worker == "collector":
                c = spec.handlers["collect_popular_games"](ws)
                spec.handlers["collect_trending"](ws)
                spec.handlers["load_ranking_snapshot"](ws)
                ws.findings[worker] = {"summary_ko": "게임 영상 %d개, 핫서치 %d개 수집(%s)." % (
                    len(ws.videos), len(ws.keywords), c["origin"]), "data_quality_notes": []}
            elif worker == "trend_radar":
                sig = spec.handlers["get_hot_keyword_signals"](ws)["signals"]
                ev = spec.handlers["get_event_signals"](ws)["events"]
                events = [{"game_zh": e["game_zh"], "kind": "official_activity",
                           "evidence": "활동 %s 참여 영상 %d개, 합계 조회수 %d" % (e["mission_id"], e["videos"], e["total_view"]),
                           "recommendation_ko": "(dry-run) 해당 공식 활동 태그로 참여 검토"} for e in ev[:3]]
                events += [{"game_zh": s["game_zh"], "kind": "leading_signal" if s["leading_signal"] else "hot_keyword",
                            "evidence": "핫서치 %d위 '%s', 풀 내 영상 %d개" % (s["rank"], s["keyword"], s["videos_in_pool"]),
                            "recommendation_ko": "(dry-run) 키워드 관련 소재 검토"} for s in sig[:3]]
                ws.findings[worker] = {"summary_ko": "(dry-run) 이벤트·핫키워드 신호 %d건." % len(events), "events": events}
            elif worker == "engagement_analyst":
                scores = h_get_game_scores(ws)["games"]
                dist = spec.handlers["get_duration_type_distribution"](ws, game_id=gid)
                ws.findings[worker] = {
                    "summary_ko": "(dry-run) 점수 상위 게임 %d개." % len(scores[:5]),
                    "opportunities": [{"game_zh": s["game_zh"], "game_ko": s["game_ko"],
                                       "reason_ko": "반응률 배수 %.2f, 풀 내 영상 %d개" % (s["engagement_multiple"], s["videos_in_pool"])}
                                      for s in scores[:5]],
                    "duration_advice": {"recommended_bucket": dist["best_median_bucket(count>=2)"] or "-",
                                        "reason_ko": "(dry-run) 2개 이상 구간 중 조회수 중앙값 최고"}}
            elif worker == "title_doctor":
                spec.handlers["get_title_benchmark"](ws, game_id=gid)
                if task.user_title:
                    spec.handlers["diagnose_title"](ws, title=task.user_title)
                ws.findings[worker] = {"summary_ko": "(dry-run) 제목 벤치마크 계산 완료.",
                                       "issues_ko": [], "rewrite_suggestions_zh": []}
            elif worker == "localizer":
                tops = h_get_top_titles(ws, n=5, game_id=gid)["titles"]
                ws.findings[worker] = {"summary_ko": "(dry-run) 번역은 LLM 실행 시 생성.", "cards": [
                    {"source_bvid": t["bvid"], "title_zh": t["title"], "title_ko": "(LLM 실행 시 번역)",
                     "why_ko": "반응률 배수 %.2f" % t["engagement_multiple"]} for t in tops]}
        return build_report(ws, "(dry-run) LLM 없이 기본 계획으로 실행한 결과입니다. 숫자는 실제 계산값입니다.")


def _worker_context(ws: Workspace) -> str:
    """워커에게 넘길 최소 맥락(작업·요청 게임·이전 워커 요약)."""
    lines = [ws.task.to_prompt()]
    if ws.requested_game:
        g = ws.requested_game
        lines.append("requested_game: game_id=%s zh=%s ko=%s" % (g["game_id"], g["zh"], g["ko"]))
    elif ws.task.game:
        lines.append("requested_game: '%s' 은(는) 별칭 사전에 없음" % ws.task.game)
    if ws.collected:
        lines.append("data: 게임 영상 %d개, 핫서치 키워드 %d개 수집됨" % (len(ws.videos), len(ws.keywords)))
    for name, f in ws.findings.items():
        lines.append("[%s 요약] %s" % (name, f.get("summary_ko", "")))
    return "\n".join(lines)


def _usage_with_cost(ws: Workspace) -> Dict:
    u = dict(ws.cache.get("usage") or {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0})
    price = config.PRICE_PER_MTOK.get(config.MODEL)
    if price:
        u["model"] = config.MODEL
        u["estimated_cost_usd"] = round(u["input_tokens"] / 1e6 * price[0] + u["output_tokens"] / 1e6 * price[1], 4)
    return u


def build_report(ws: Workspace, summary_ko: str) -> BiliReport:
    """워커 결과 + 결정적 계산값을 BiliReport 로 조립.

    점수 같은 숫자는 LLM 이 다시 옮겨 적지 않고 코드가 계산값을 붙인다(전사 오류 방지).
    """
    f = ws.findings
    scores = {s["game_zh"]: s for s in (ws.cache.get("game_scores") or
                                        (metrics.score_games(ws.videos, ws.keywords, ws.alias) if ws.videos else []))}
    opportunities = []
    for o in f.get("engagement_analyst", {}).get("opportunities", []):
        s = scores.get(o.get("game_zh"))
        opportunities.append(dict(o, score=s and s["score"], components=s and s["components"]))

    title_diag = None
    if "title_doctor" in f:
        title_diag = dict(ws.cache.get("title_diagnosis") or {"benchmark": ws.cache.get("title_benchmark")},
                          issues_ko=f["title_doctor"].get("issues_ko", []),
                          rewrite_suggestions_zh=f["title_doctor"].get("rewrite_suggestions_zh", []))

    duration = None
    if "engagement_analyst" in f:
        gid = ws.requested_game["game_id"] if ws.requested_game else "all"
        dist = ws.cache.get("distribution:%s" % gid) or ws.cache.get("distribution:all")
        duration = dict(f["engagement_analyst"].get("duration_advice", {}), distribution=dist)

    for note in f.get("collector", {}).get("data_quality_notes", []):
        ws.warnings.append("collector: " + note)

    return BiliReport(
        task_id=ws.task.task_id, intent=ws.task.intent, summary_ko=summary_ko,
        opportunities=opportunities,
        events=f.get("trend_radar", {}).get("events", []),
        title_diagnosis=title_diag, duration_advice=duration,
        overseas_trend_cards=f.get("localizer", {}).get("cards", []),
        alias_candidates=f.get("collector", {}).get("alias_candidates", []),
        data_sources=ws.data_sources, agent_trace=ws.trace, usage=_usage_with_cost(ws),
        warnings=list(dict.fromkeys(ws.warnings)),
    )
