#!/usr/bin/env python3
"""赛后预测跟踪系统 — 存储预测 → 比对结果 → 生成准确率报告

用法:
  # 录入预测（由 orchestrator 在生成预测文章时调用）
  python prediction_tracker.py --record 2026-07-13 '{"home":"巴西","away":"阿根廷","prediction":"巴西胜","score":"2-1"}'

  # 比对实际结果（由 orchestrator 在第二天的数据采集后调用）
  python prediction_tracker.py --compare 2026-07-14

  # 生成准确率报告文章
  python prediction_tracker.py --report 2026-07-14

  # 查看未结算的预测
  python prediction_tracker.py --pending
"""

import json, os, sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", PROJECT_ROOT / "output"))
PREDICTION_LOG = OUTPUT_DIR / "predictions.json"


def _ensure_log():
    if not PREDICTION_LOG.exists():
        PREDICTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        PREDICTION_LOG.write_text(json.dumps({"predictions": [], "updated_at": None}, ensure_ascii=False, indent=2))


def load_predictions():
    _ensure_log()
    try:
        return json.loads(PREDICTION_LOG.read_text())
    except Exception:
        return {"predictions": [], "updated_at": None}


def save_predictions(data):
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    PREDICTION_LOG.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def record_prediction(date_str, match_home, match_away, prediction, score_hint="", article_idx=0):
    """Record a prediction for future comparison.

    Args:
        date_str: 预测发布日期 (YYYY-MM-DD, 比赛在之后发生)
        match_home: 主队名
        match_away: 客队名
        prediction: "主胜"/"客胜"/"平局" 或自定义
        score_hint: 预测比分 (如 "2-1")
        article_idx: 预测文章的序号
    """
    data = load_predictions()
    pred = {
        "id": f"pred-{len(data['predictions'])+1}",
        "predicted_on": date_str,
        "match_home": match_home,
        "match_away": match_away,
        "prediction": prediction,
        "score_hint": score_hint,
        "article_index": article_idx,
        "status": "pending",      # pending / correct / wrong / draw
        "actual_home_score": None,
        "actual_away_score": None,
        "settled_on": None,
    }
    data["predictions"].append(pred)
    save_predictions(data)
    print(f"  ✅ 预测已记录: {match_home} vs {match_away} → {prediction}")
    return pred


def settle_prediction(pred, match_data):
    """比对预测与实际赛果，更新预测状态。

    Args:
        pred: 预测 dict
        match_data: collect_real_matches 返回的数据 (含 all_fixtures 的已结束比赛)
    Returns:
        True 如果找到了匹配的比赛并结算, False 如果没找到
    """
    home, away = pred["match_home"], pred["match_away"]
    for f in match_data.get("all_fixtures", []):
        f_home = f.get("home_team", "")
        f_away = f.get("away_team", "")
        hg = f.get("home_score")
        ag = f.get("away_score")
        status = f.get("status", "")
        if not (hg is not None and ag is not None):
            continue
        if status not in ("FT", "AET", "PEN"):
            continue
        # 匹配球队 (双向匹配，不区分主客场顺序)
        if (home in (f_home, f_away) and away in (f_home, f_away)) or \
           (away in (f_home, f_away) and home in (f_home, f_away)):
            # 判定预测是否准确
            pred["actual_home_score"] = hg
            pred["actual_away_score"] = ag
            if pred["prediction"] in ("主胜", f"{home}胜"):
                pred["status"] = "correct" if hg > ag else "wrong"
            elif pred["prediction"] in ("客胜", f"{away}胜"):
                pred["status"] = "correct" if ag > hg else "wrong"
            elif pred["prediction"] in ("平局", "平"):
                pred["status"] = "correct" if hg == ag else "wrong"
            elif pred["score_hint"]:
                predicted_hg, *rest = pred["score_hint"].split("-")
                try:
                    pred["status"] = "correct" if int(predicted_hg) == hg and int(rest[0]) == ag else "wrong"
                except (ValueError, IndexError):
                    pred["status"] = "wrong"
            else:
                # 无法判断 — 保留 pending
                pred["status"] = "pending"
            pred["settled_on"] = datetime.now().strftime("%Y-%m-%d")
            print(f"  🔄 结算: {home} {hg}-{ag} {away} → {pred['status']}")
            return True
    return False


def compare_all_pending(match_data):
    """将所有 pending 的预测与 match_data 中的实际赛果比对。"""
    data = load_predictions()
    settled = 0
    for pred in data["predictions"]:
        if pred.get("status") == "pending":
            if settle_prediction(pred, match_data):
                settled += 1
    if settled:
        save_predictions(data)
        print(f"  ✅ {settled} 条预测已结算")
    else:
        print(f"  ℹ️ 无可结算的预测（比赛可能还未结束）")
    return settled


def get_pending_predictions():
    """返回所有 pending 状态的预测。"""
    data = load_predictions()
    return [p for p in data["predictions"] if p.get("status") == "pending"]


def generate_accuracy_report(date_str, match_data=None):
    """生成准确率报告文本（不发布，返回 markdown 文本）。"""
    data = load_predictions()
    all_preds = [p for p in data["predictions"] if p.get("status") != "pending"]
    if not all_preds:
        # 尝试结算
        if match_data:
            compare_all_pending(match_data)
            all_preds = [p for p in data["predictions"] if p.get("status") != "pending"]

    total = len(all_preds)
    correct = sum(1 for p in all_preds if p.get("status") == "correct")
    wrong = sum(1 for p in all_preds if p.get("status") == "wrong")

    if total == 0:
        print("  ℹ️ 暂无已结算的预测")
        return None

    accuracy = correct / total * 100
    verdict = "👑 老六封神" if accuracy >= 70 else "😅 老六打脸" if accuracy < 40 else "📊 五五开"

    report = f"""# {verdict}：老六预测准确率 {accuracy:.0f}%

过去一周，老六做了 {total} 场预测，对了 **{correct}** 场 ({accuracy:.0f}%)，错了 **{wrong}** 场 ({(100-accuracy):.0f}%)。

## 预测逐场回顾

| 比赛 | 预测 | 实际赛果 | 结果 |
|------|------|---------|------|
"""
    for p in all_preds:
        home = p["match_home"]
        away = p["match_away"]
        pred = p["prediction"]
        ah = p.get("actual_home_score", "?")
        aa = p.get("actual_away_score", "?")
        result = p.get("status", "?")
        emoji = "✅" if result == "correct" else "❌" if result == "wrong" else "➖"
        report += f"| {home} vs {away} | {pred} | {ah}-{aa} | {emoji} {result} |\n"

    if accuracy >= 70:
        report += "\n这周手气不错！下周继续带兄弟们吃肉。"
    elif accuracy >= 40:
        report += "\n马马虎虎，下周得用点力了。"
    else:
        report += "\n这周脸被打肿了，下周老六闭关修炼，杀回来！"

    report += "\n\n💬 **你觉得老六下周应该押哪些队？评论区见！**"
    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="预测跟踪系统")
    parser.add_argument("--record", nargs=2, metavar=("DATE", "JSON"), help="录入预测")
    parser.add_argument("--compare", metavar="DATE", help="比对实际结果")
    parser.add_argument("--report", metavar="DATE", help="生成准确率报告")
    parser.add_argument("--pending", action="store_true", help="查看未结算预测")
    args = parser.parse_args()

    if args.record:
        date_str, pred_json = args.record
        pred = json.loads(pred_json)
        record_prediction(date_str,
                          pred.get("home", ""),
                          pred.get("away", ""),
                          pred.get("prediction", ""),
                          pred.get("score_hint", ""),
                          pred.get("article_idx", 0))
    elif args.compare:
        from data_collector import collect_real_matches
        md = collect_real_matches(args.compare)
        compare_all_pending(md)
    elif args.report:
        report = generate_accuracy_report(args.report)
        if report:
            print(report)
    elif args.pending:
        pending = get_pending_predictions()
        if pending:
            print(f"\n📋 未结算预测 ({len(pending)} 条)")
            for p in pending:
                print(f"  {p['match_home']} vs {p['match_away']} → {p['prediction']} (来自 {p['predicted_on']})")
        else:
            print("  ✅ 所有预测已结算")
    else:
        parser.print_help()
