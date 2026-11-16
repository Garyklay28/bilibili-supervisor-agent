"""CLI 데모: 총괄 슈퍼바이저를 흉내 내어 BiliTask 하나를 보내고 BiliReport 를 출력한다.

예)
  python main.py --intent trend_scan
  python main.py --intent game_deep_dive --game 英雄联盟
  python main.py --intent title_check --title "【S16】BLG夺冠全过程复盘"
  python main.py --intent trend_scan --dry-run      # API 키 없이 데이터 파이프라인만
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from bili_agent.agents.supervisor import BiliSupervisor
from bili_agent.contracts import INTENTS, BiliTask


def main() -> int:
    ap = argparse.ArgumentParser(description="빌리빌리 서브 슈퍼바이저 PoC")
    ap.add_argument("--intent", choices=INTENTS, default="trend_scan")
    ap.add_argument("--game", help="게임 이름(한·중·영)")
    ap.add_argument("--title", help="title_check 용 중국어 제목")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true", help="LLM 호출 없이 기본 계획을 코드로 실행")
    ap.add_argument("--out", help="보고서 JSON 저장 경로(기본: outputs/report_<시각>.json)")
    args = ap.parse_args()

    task = BiliTask(intent=args.intent, game=args.game, user_title=args.title, period_days=args.days)
    sup = BiliSupervisor()
    report = sup.run_dry(task) if args.dry_run else sup.run(task)
    data = report.to_dict()

    print("\n=== 에이전트 실행 기록 ===")
    for row in data["agent_trace"]:
        print("%6.1fs %-20s %-28s %s%s" % (row["t"], row["agent"], row["event"],
                                          "" if row["ok"] else "[ERROR] ", row["detail"]))
    print("\n=== 요약 ===\n" + data["summary_ko"])
    if data["warnings"]:
        print("\n=== 경고 ===\n- " + "\n- ".join(data["warnings"]))

    out = Path(args.out) if args.out else Path("outputs") / ("report_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n보고서 저장: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
