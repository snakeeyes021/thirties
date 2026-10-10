"""Automated Qualitative Evaluation Battery Runner (25 Scenarios).

Runs a curated suite of 25 multi-turn scenarios covering core behavioral archetypes
against the local GPU Gemma engine using the interactive evaluation harness.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.interactive_eval import EvalSession, InteractiveEvaluator, detect_anomalies
from tests.run_eval_harness import FastHybridInferenceEngine, PersistentGemmaEngine
from thirties_core.astronomy import set_debug_time
from thirties_core.conversation import ACTION_CLAIM_PATTERN
from thirties_core.models import BlockKind

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("battery_runner")

REPORTS_DIR = REPO_ROOT / "docs" / "eval_reports"


@dataclass
class ScenarioStep:
    user_text: str
    expected_intent: str
    eval_rubric: str


@dataclass
class BatteryScenario:
    scenario_id: str
    name: str
    archetype: str
    target_date: date
    simulated_time_iso: Optional[str]
    steps: List[ScenarioStep]


def get_25_scenarios() -> List[BatteryScenario]:
    d = date(2026, 10, 9)  # Friday, Oct 9, 2026
    ny_tz = "America/New_York"
    t_morning = "2026-10-09T08:00:00-04:00"

    scenarios = [
        # 1. Shifting work hours 8-4
        BatteryScenario(
            scenario_id="scn_01_work_shift_8_to_4",
            name="Work Hours Correction to 8-4",
            archetype="Diurnal Envelope & Solar Dynamic Shifter",
            target_date=d,
            simulated_time_iso=t_morning,
            steps=[
                ScenarioStep(
                    user_text="Ah, ok, so work earlier today was actually from 8-4.",
                    expected_intent="set_work_hours",
                    eval_rubric="Should schedule Work from 8:00 AM to 4:00 PM (Blocks 3–18). Must not confuse 8am with Block 8.",
                ),
                ScenarioStep(
                    user_text="Did that leave 4pm to 6pm open for creative work?",
                    expected_intent="schedule_query",
                    eval_rubric="Should accurately check Blocks 19-22 and confirm they are open daylight thirties.",
                ),
            ],
        ),

        # 2. Work shift 7-3 (Envelope collapse)
        BatteryScenario(
            scenario_id="scn_02_work_shift_7_to_3",
            name="Work Shift 7am to 3pm",
            archetype="Diurnal Envelope & Solar Dynamic Shifter",
            target_date=d,
            simulated_time_iso=t_morning,
            steps=[
                ScenarioStep(
                    user_text="Turns out work today is from 7am to 3pm.",
                    expected_intent="set_work_hours",
                    eval_rubric="Should schedule Work 7am-3pm (Blocks 1–16). Blocks 17 and 18 must be restored to discretionary.",
                ),
            ],
        ),

        # 3. Night shift / graveyard worker
        BatteryScenario(
            scenario_id="scn_03_night_shift_worker",
            name="Nocturnal Graveyard Shift",
            archetype="The Shift Worker / Nocturnal Extreme",
            target_date=d,
            simulated_time_iso="2026-10-09T18:00:00-04:00",
            steps=[
                ScenarioStep(
                    user_text="I work a graveyard shift tonight from 9:00 PM to 5:00 AM.",
                    expected_intent="set_work_hours",
                    eval_rubric="Should schedule dark work blocks across the night without crashing.",
                ),
            ],
        ),

        # 4. Creative Dorico daylight focus
        BatteryScenario(
            scenario_id="scn_04_dorico_daylight",
            name="High-Focus Dorico Composition",
            archetype="The Energy-Aligned Flow Optimizer",
            target_date=d,
            simulated_time_iso=t_morning,
            steps=[
                ScenarioStep(
                    user_text="Allocate 2 hours for Dorico composing at 4:30 PM today.",
                    expected_intent="allocate_task",
                    eval_rubric="Should schedule 4 chunks (Blocks 20–23) from 4:30 PM to 6:30 PM before sunset.",
                ),
            ],
        ),

        # 5. Backward scheduling around appointment
        BatteryScenario(
            scenario_id="scn_05_backward_scheduling_dentist",
            name="Transit Buffer Before Appointment",
            archetype="The Chrono-Pragmatist",
            target_date=d,
            simulated_time_iso=t_morning,
            steps=[
                ScenarioStep(
                    user_text="I have a doctor appointment at 4:00 PM. Put a 30-minute travel buffer right before it.",
                    expected_intent="schedule_buffer",
                    eval_rubric="Buffer must be placed in Block 18 (03:30 PM – 04:00 PM), immediately preceding appointment.",
                ),
            ],
        ),

        # 6. Appointment reschedule and clear
        BatteryScenario(
            scenario_id="scn_06_appointment_reschedule",
            name="Appointment Reschedule & Clear",
            archetype="The Chaos Monkey / Reactive Pivot Master",
            target_date=d,
            simulated_time_iso=t_morning,
            steps=[
                ScenarioStep(
                    user_text="Doctor rescheduled to 5:00 PM. Clear the 4:00 PM appointment and put it at 5:00 PM.",
                    expected_intent="clear_and_reschedule",
                    eval_rubric="Must clear old slot and allocate 5:00 PM (Block 21).",
                ),
            ],
        ),

        # 7. Impossible duration request
        BatteryScenario(
            scenario_id="scn_07_impossible_duration",
            name="Impossible Duration Handling",
            archetype="The Impossible / Physics-Defying Requestor",
            target_date=d,
            simulated_time_iso=t_morning,
            steps=[
                ScenarioStep(
                    user_text="Can you fit 4 hours of reading between 3:00 PM and 4:00 PM today?",
                    expected_intent="impossible_duration",
                    eval_rubric="Must reject or clarify that 4 hours cannot fit into a 1-hour window (2 chunks).",
                ),
            ],
        ),

        # 8. Decision-fatigued suggestion request
        BatteryScenario(
            scenario_id="scn_08_decision_fatigued_suggestion",
            name="Suggestion Request (No Direct Mutation)",
            archetype="The Decision-Fatigued Minimalist",
            target_date=d,
            simulated_time_iso=t_morning,
            steps=[
                ScenarioStep(
                    user_text="Where would you suggest I compose for 1 hour this afternoon?",
                    expected_intent="suggest_blocks",
                    eval_rubric="Must suggest 1-2 open upcoming blocks and ask. Must NOT emit scheduling directives yet!",
                ),
            ],
        ),

        # 9. Suggestion confirmation
        BatteryScenario(
            scenario_id="scn_09_suggestion_then_confirm",
            name="Suggestion Follow-up Confirmation",
            archetype="The Decision-Fatigued Minimalist",
            target_date=d,
            simulated_time_iso=t_morning,
            steps=[
                ScenarioStep(
                    user_text="Where should I practice guitar today?",
                    expected_intent="suggest_blocks",
                    eval_rubric="Provides suggestions politely.",
                ),
                ScenarioStep(
                    user_text="Sure, that sounds great. Go ahead and schedule it.",
                    expected_intent="confirm_suggestion",
                    eval_rubric="Now emits directives to allocate the suggested time.",
                ),
            ],
        ),

        # 10. Multi-task allocation in single prompt
        BatteryScenario(
            scenario_id="scn_10_multi_task_allocation",
            name="Multiple Tasks in Single Turn",
            archetype="The High-Density Multitasker",
            target_date=d,
            simulated_time_iso=t_morning,
            steps=[
                ScenarioStep(
                    user_text="Schedule lunch at 12:00 PM for 30 minutes, and gym at 5:00 PM for 1 hour.",
                    expected_intent="allocate_multiple",
                    eval_rubric="Emits distinct directives for lunch (Block 11) and gym (Blocks 21-22).",
                ),
            ],
        ),

        # 11. Diagnostic inquiry handling (Defensiveness check)
        BatteryScenario(
            scenario_id="scn_11_diagnostic_complaint_why",
            name="Diagnostic Inquiry & Defensiveness Check",
            archetype="The Inquisitive / Diagnostic Challenger",
            target_date=d,
            simulated_time_iso=t_morning,
            steps=[
                ScenarioStep(
                    user_text="Why did you schedule work from 10:30 to 5 when I didn't ask for that?",
                    expected_intent="inquiry_explanation",
                    eval_rubric="Must explain calmly and respectfully without being defensive, and must NOT execute another unsolicited shift!",
                ),
            ],
        ),

        # 12. Factual preservation query
        BatteryScenario(
            scenario_id="scn_12_factual_preservation_query",
            name="Factual Schedule State Verification",
            archetype="The Inquisitive / Diagnostic Challenger",
            target_date=d,
            simulated_time_iso=t_morning,
            steps=[
                ScenarioStep(
                    user_text="Schedule composing in Block 19.",
                    expected_intent="allocate_task",
                    eval_rubric="Schedules composing in Block 19.",
                ),
                ScenarioStep(
                    user_text="Did that overwrite my doctor appointment?",
                    expected_intent="verify_state",
                    eval_rubric="Answers factually about Block 19 status without defensive deflection.",
                ),
            ],
        ),

        # 13. Day off: clear all work blocks
        BatteryScenario(
            scenario_id="scn_13_day_off_clear_work",
            name="Day Off Work Clearing",
            archetype="Diurnal Envelope & Solar Dynamic Shifter",
            target_date=d,
            simulated_time_iso=t_morning,
            steps=[
                ScenarioStep(
                    user_text="I took today off work, clear all my work blocks.",
                    expected_intent="clear_work_blocks",
                    eval_rubric="Emits CLEAR_BLOCKS: WORK. All work blocks cleared to discretionary.",
                ),
            ],
        ),

        # 14. Clear tasks while keeping work envelope intact
        BatteryScenario(
            scenario_id="scn_14_clear_tasks_keep_work",
            name="Clear Tasks While Preserving Work",
            archetype="The Task Reset Purist",
            target_date=d,
            simulated_time_iso=t_morning,
            steps=[
                ScenarioStep(
                    user_text="Clear all my scheduled tasks for today, but keep my work hours.",
                    expected_intent="clear_tasks",
                    eval_rubric="Emits CLEAR_BLOCKS: TASKS or ALL. Work blocks remain WORK, custom tasks cleared.",
                ),
            ],
        ),

        # 15. Locked calendar block conflict protection
        BatteryScenario(
            scenario_id="scn_15_calendar_conflict_protection",
            name="Locked Calendar Protection",
            archetype="The Strict Boundary Enforcer",
            target_date=d,
            simulated_time_iso=t_morning,
            steps=[
                ScenarioStep(
                    user_text="Put a 2-hour movie in Block 19.",
                    expected_intent="conflict_handling",
                    eval_rubric="Recognizes Block 19 has appointment, warns or handles conflict gracefully.",
                ),
            ],
        ),

        # 16. Calendar resolution: attend
        BatteryScenario(
            scenario_id="scn_16_calendar_resolve_attend",
            name="Confirm Ambiguous Calendar Event",
            archetype="The Strict Boundary Enforcer",
            target_date=d,
            simulated_time_iso=t_morning,
            steps=[
                ScenarioStep(
                    user_text="Yes, confirm attending the Doctor Appointment.",
                    expected_intent="resolve_event",
                    eval_rubric="Emits RESOLVE_EVENT: e_doc | attend, locking it as BUSY_CALENDAR.",
                ),
            ],
        ),

        # 17. Calendar resolution: decline
        BatteryScenario(
            scenario_id="scn_17_calendar_resolve_decline",
            name="Decline Ambiguous Calendar Event",
            archetype="The Strict Boundary Enforcer",
            target_date=d,
            simulated_time_iso=t_morning,
            steps=[
                ScenarioStep(
                    user_text="No, decline the Doctor Appointment, I'm not going.",
                    expected_intent="resolve_event",
                    eval_rubric="Emits RESOLVE_EVENT: e_doc | decline, opening it as discretionary time.",
                ),
            ],
        ),

        # 18. Early morning routine
        BatteryScenario(
            scenario_id="scn_18_early_morning_routine",
            name="Early Morning Routine Placement",
            archetype="The Chrono-Pragmatist",
            target_date=d,
            simulated_time_iso="2026-10-09T06:30:00-04:00",
            steps=[
                ScenarioStep(
                    user_text="I woke up early, schedule 30 minutes of stretching in my earliest open block today.",
                    expected_intent="allocate_earliest",
                    eval_rubric="Places stretching in Block 1 or earliest open daylight block.",
                ),
            ],
        ),

        # 19. Change of mind / rapid correction
        BatteryScenario(
            scenario_id="scn_19_change_of_mind",
            name="Rapid Change of Mind",
            archetype="The Chaos Monkey / Reactive Pivot Master",
            target_date=d,
            simulated_time_iso=t_morning,
            steps=[
                ScenarioStep(
                    user_text="Allocate block 25 to quick lunch.",
                    expected_intent="allocate_block",
                    eval_rubric="Allocates block 25 to lunch.",
                ),
                ScenarioStep(
                    user_text="No wait, lunch is actually at block 26. Clear block 25 and put it in block 26.",
                    expected_intent="clear_and_relocate",
                    eval_rubric="Clears block 25 and allocates block 26.",
                ),
            ],
        ),

        # 20. Evening wind-down in dark thirties
        BatteryScenario(
            scenario_id="scn_20_evening_winddown",
            name="Evening Wind-down Alignment",
            archetype="The Energy-Aligned Flow Optimizer",
            target_date=d,
            simulated_time_iso="2026-10-09T19:00:00-04:00",
            steps=[
                ScenarioStep(
                    user_text="Schedule 1 hour of fiction reading after dinner at 8:00 PM.",
                    expected_intent="allocate_dark",
                    eval_rubric="Schedules Blocks 27–28 in Dark Discretionary time appropriately.",
                ),
            ],
        ),

        # 21. Bedtime sleep window
        BatteryScenario(
            scenario_id="scn_21_bedtime_sleep_window",
            name="Bedtime Sleep Window Modification",
            archetype="The Shift Worker / Nocturnal Extreme",
            target_date=d,
            simulated_time_iso="2026-10-09T20:00:00-04:00",
            steps=[
                ScenarioStep(
                    user_text="I'm heading to sleep early tonight at 10:00 PM.",
                    expected_intent="set_sleep_hours",
                    eval_rubric="Updates sleep window to start at 10:00 PM (Block 31).",
                ),
            ],
        ),

        # 22. Premature finalization prevention
        BatteryScenario(
            scenario_id="scn_22_premature_finalization",
            name="Ongoing Negotiation (No Finalize)",
            archetype="The Iterative Negotiator",
            target_date=d,
            simulated_time_iso=t_morning,
            steps=[
                ScenarioStep(
                    user_text="I might have a meeting this afternoon, but for now put writing at 1:00 PM.",
                    expected_intent="partial_planning",
                    eval_rubric="Must NOT emit FINALIZE_PLAN! Plan is still actively being discussed.",
                ),
            ],
        ),

        # 23. Confirmed finalization
        BatteryScenario(
            scenario_id="scn_23_confirmed_finalization",
            name="Confirmed Plan Finalization",
            archetype="The Iterative Negotiator",
            target_date=d,
            simulated_time_iso=t_morning,
            steps=[
                ScenarioStep(
                    user_text="Everything looks completely solid. Please finalize and lock in my plan for today.",
                    expected_intent="finalize_plan",
                    eval_rubric="User explicitly confirmed; model emits FINALIZE_PLAN.",
                ),
            ],
        ),

        # 24. Daylight query
        BatteryScenario(
            scenario_id="scn_24_daylight_query",
            name="Daylight Availability Query",
            archetype="The Energy-Aligned Flow Optimizer",
            target_date=d,
            simulated_time_iso="2026-10-09T14:00:00-04:00",
            steps=[
                ScenarioStep(
                    user_text="How many daylight chunks do I have left before sunset?",
                    expected_intent="query_chunks",
                    eval_rubric="Answers accurately using remaining upcoming daylight chunks, not hours.",
                ),
            ],
        ),

        # 25. Complex multi-turn workflow
        BatteryScenario(
            scenario_id="scn_25_complex_marathon",
            name="Multi-Turn Complex Dynamic Workflow",
            archetype="The High-Density Multitasker",
            target_date=d,
            simulated_time_iso=t_morning,
            steps=[
                ScenarioStep(
                    user_text="Work is from 8am to 4pm today.",
                    expected_intent="set_work_hours",
                    eval_rubric="Sets work 8am to 4pm (Blocks 3–18).",
                ),
                ScenarioStep(
                    user_text="Put Dorico composing right after work from 4:00 PM to 6:00 PM.",
                    expected_intent="allocate_task",
                    eval_rubric="Allocates Blocks 19–22 for composing.",
                ),
                ScenarioStep(
                    user_text="Wait, doctor appointment is at 4:00 PM! Shift Dorico to 6:30 PM.",
                    expected_intent="relocate_task",
                    eval_rubric="Shifts Dorico composing to 6:30 PM (Blocks 24–27).",
                ),
            ],
        ),
    ]
    return scenarios


def evaluate_step(
    step: ScenarioStep,
    turn_record: Any,
) -> Dict[str, Any]:
    """Qualitative grading heuristic based on contract invariants and tone."""
    u_text = turn_record.user_message.lower()
    reply = turn_record.assistant_reply
    reply_lower = reply.lower()
    tools = [t.get("name") for t in turn_record.executed_tools]
    anomalies = turn_record.detected_anomalies
    delta = turn_record.delta_summary

    accuracy = 5
    adherence = 5
    tone = 5
    reasoning = 5
    flags: List[str] = []
    notes: List[str] = []

    # 1. Check for severe anomalies
    if any("HALLUCINATED ACTION" in a for a in anomalies):
        accuracy = min(accuracy, 1)
        adherence = min(adherence, 1)
        flags.append("hallucinated_action")
        notes.append("Claimed action but delta was 0.")

    if any("DEFENSIVE DEFLECTION" in a for a in anomalies):
        tone = min(tone, 1)
        reasoning = min(reasoning, 2)
        flags.append("defensive_deflection")
        notes.append("Refused explanation defensively.")

    if any("UNSOLICITED MUTATION" in a for a in anomalies):
        adherence = min(adherence, 1)
        flags.append("unsolicited_mutation")
        notes.append("Mutated schedule in response to question.")

    if any("NUMBER CONFUSION" in a for a in anomalies):
        accuracy = min(accuracy, 2)
        flags.append("offset_number_confusion")
        notes.append("Literal number confusion (8am -> Block 8).")

    if any("PASSIVE PHRASING" in a for a in anomalies):
        tone = min(tone, 3)
        flags.append("passive_phrasing")
        notes.append("Phrased action as 'You have done...'.")

    # 2. Specific intent checks
    if step.expected_intent == "suggest_blocks":
        if tools:
            adherence = min(adherence, 2)
            flags.append("mutated_on_suggestion")
            notes.append("Emitted directives when asked for suggestion.")
        else:
            notes.append("Properly suggested without mutating.")

    if step.expected_intent == "impossible_duration":
        rejections = ["cannot", "impossible", "not fit", "exceeds", "only 1 hour", "only 2 chunks"]
        if not any(r in reply_lower for r in rejections):
            accuracy = min(accuracy, 2)
            flags.append("accepted_impossible_duration")
            notes.append("Failed to reject physically impossible duration.")
        else:
            notes.append("Correctly identified impossible duration.")

    if step.expected_intent == "partial_planning":
        if "finalize_day_plan" in tools or "day plan finalized" in reply_lower:
            adherence = min(adherence, 1)
            flags.append("premature_finalization")
            notes.append("Emitted FINALIZE_PLAN during ongoing negotiation.")

    if not notes:
        notes.append("Turn executed cleanly.")

    critique = "; ".join(notes)
    return {
        "accuracy": accuracy,
        "adherence": adherence,
        "tone": tone,
        "reasoning": reasoning,
        "notes": critique,
        "flags": flags,
    }


def run_battery():
    scenarios = get_25_scenarios()
    logger.info("================================================================================")
    logger.info("STARTING QUALITATIVE EVALUATION BATTERY: %d SCENARIOS", len(scenarios))
    logger.info("Target: Local Gemma 4B GPU Engine via PersistentGemmaEngine")
    logger.info("================================================================================")

    # Initialize persistent GPU daemon once for the entire battery
    daemon = PersistentGemmaEngine()
    engine = FastHybridInferenceEngine(daemon=daemon)

    results: List[Dict[str, Any]] = []
    t_battery_start = time.time()

    for idx, scn in enumerate(scenarios, start=1):
        logger.info("\n--------------------------------------------------------------------------------")
        logger.info("Running Scenario [%d/%d]: %s (%s)", idx, len(scenarios), scn.name, scn.scenario_id)
        logger.info("Archetype: %s", scn.archetype)

        daemon.reset()

        sim_time = datetime.fromisoformat(scn.simulated_time_iso) if scn.simulated_time_iso else None
        session = InteractiveEvaluator.start_session(
            target_date=scn.target_date,
            persona=f"{scn.name} ({scn.archetype})",
            simulated_time=sim_time,
            session_id=scn.scenario_id,
            use_gpu=True,
            engine=engine,
        )

        for s_idx, step in enumerate(scn.steps, start=1):
            logger.info("  Turn %d User: %r", s_idx, step.user_text)
            turn_record = InteractiveEvaluator.step_session(
                session=session,
                user_message=step.user_text,
                use_gpu=True,
                engine=engine,
            )
            logger.info("  Turn %d Bot:  %r", s_idx, turn_record.assistant_reply[:120])

            grade_info = evaluate_step(step, turn_record)
            InteractiveEvaluator.grade_turn(
                session=session,
                turn_index=s_idx,
                accuracy=grade_info["accuracy"],
                tone=grade_info["tone"],
                adherence=grade_info["adherence"],
                reasoning=grade_info["reasoning"],
                notes=grade_info["notes"],
                flags=grade_info["flags"],
            )

        report_path = InteractiveEvaluator.generate_report(session)
        graded_turns = [t for t in session.turns if t.grade]
        avg_comp = sum(t.grade.composite_score for t in graded_turns) / max(1, len(graded_turns))

        scn_summary = {
            "scenario_id": scn.scenario_id,
            "name": scn.name,
            "archetype": scn.archetype,
            "total_turns": len(session.turns),
            "composite": avg_comp,
            "passed": avg_comp >= 70.0,
            "flags": [f for t in session.turns if t.grade for f in t.grade.flags],
            "report_path": str(report_path),
        }
        results.append(scn_summary)
        status_str = "PASS" if scn_summary["passed"] else "FAIL"
        logger.info("  --> Scenario %s Result: %s (Composite: %.1f%%)", scn.scenario_id, status_str, avg_comp)

    daemon.close()
    elapsed_total = round(time.time() - t_battery_start, 1)

    # Generate Master Battery Report
    master_report_path = REPORTS_DIR / "eval_battery_round_1.md"
    passed_count = sum(1 for r in results if r["passed"])
    pass_rate = (passed_count / len(results)) * 100.0
    mean_comp = sum(r["composite"] for r in results) / len(results)

    all_flags = [f for r in results for f in r["flags"]]
    flag_counts: Dict[str, int] = {}
    for f in all_flags:
        flag_counts[f] = flag_counts.get(f, 0) + 1

    md = []
    md.append("# Master Qualitative Evaluation Report: Round 1\n")
    md.append(f"- **Total Scenarios Evaluated**: {len(results)}")
    md.append(f"- **Pass Rate**: {passed_count}/{len(results)} ({pass_rate:.1f}%)")
    md.append(f"- **Average Composite Score**: {mean_comp:.1f}%")
    md.append(f"- **Total Elapsed Time**: {elapsed_total}s (average {elapsed_total/len(results):.1f}s per scenario)\n")

    md.append("## Executive Scorecard\n")
    md.append("| Metric | Result | Status |")
    md.append("|---|---|---|")
    md.append(f"| Overall Pass Rate | **{pass_rate:.1f}%** | {'✅ PASS' if pass_rate >= 80 else '⚠️ ATTENTION NEEDED'} |")
    md.append(f"| Mean Quality Score | **{mean_comp:.1f} / 100** | {'✅ Solid' if mean_comp >= 75 else '⚠️ Sub-optimal'} |")
    md.append(f"| Total Anomaly Flags Detected | **{len(all_flags)}** | {'⚠️ Anomalies Observed' if all_flags else '✅ None'} |\n")

    if flag_counts:
        md.append("## Observed Failure Modes & Frequency\n")
        md.append("| Failure Mode Flag | Frequency | Impact |")
        md.append("|---|---|---|")
        for f, cnt in sorted(flag_counts.items(), key=lambda x: -x[1]):
            md.append(f"| `{f}` | {cnt} occurrences | {'High' if cnt >= 3 else 'Medium'} |")
        md.append("")

    md.append("## Scenario Breakdown\n")
    md.append("| ID | Scenario Name | Archetype | Turns | Composite | Result |")
    md.append("|---|---|---|---|---|---|")
    for r in results:
        res_icon = "✅ PASS" if r["passed"] else "❌ FAIL"
        md.append(f"| [`{r['scenario_id']}`]({r['report_path']}) | {r['name']} | {r['archetype']} | {r['total_turns']} | {r['composite']:.1f}% | {res_icon} |")
    md.append("")

    master_report_path.write_text("\n".join(md))
    logger.info("================================================================================")
    logger.info("ROUND 1 COMPLETE: %d / %d PASSED (%.1f%%)", passed_count, len(results), pass_rate)
    logger.info("Master Report written to: %s", master_report_path)
    logger.info("================================================================================")
    return results


if __name__ == "__main__":
    run_battery()
