"""Frontier brain: drives a large model (Gemini today) with one rich prompt.

The model sees the whole schedule and the full Joplin backlog, and acts by
emitting tool calls that ``PlanTools`` executes. It speaks only in clock times;
all time math happens in the tools. After each round the brain shows the model
its tool results, so it can react to conflicts or read-only lookups, and it
checks the model's claims against the real state change.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from thirties_core.astronomy import get_current_time, get_diurnal_date
from thirties_core.clock import format_clock
from thirties_core.goals import prompt_lines as goal_check_lines
from thirties_core.inference import InferenceEngine
from thirties_core.lists import ListCatalog, pressing_words  # plain words, never the urgency value
from thirties_core.messages import now_section
from thirties_core.models import TaskItem
from thirties_core.plan_tools import NOT_DONE, READ_ONLY_TOOLS, TOOL_SPECS, PlanTools
from thirties_core.plan_log import model_call  # the model's own tool calls (change_scope.py)
from thirties_core.privacy import is_private_note

logger = logging.getLogger(__name__)

MAX_ROUNDS = 3

ACTION_CLAIM_PATTERN = re.compile(
    r"\b(?:i\s+have|i\'ve|i\s+did|i)\s+(?:(?:\w+ly|now|already|just)\s+)?(?:cleared|scheduled|allocated|updated|set|assigned|removed|reverted|corrected|locked|finalized|put|placed|moved|added|shifted|extended|blocked)\b"
    r"|\b(?:plan|schedule|work\s+shift|blocks?)\s+(?:has\s+been\s+)?(?:updated|cleared|scheduled|finalized|locked|set)\b"
    r"|\bday\s+plan\s+finalized\b",
    re.IGNORECASE,
)


def render_tool_specs() -> str:
    lines = []
    for spec in TOOL_SPECS:
        params = spec["parameters"]
        sig = ", ".join(p if p in spec["required"] else f"{p}?" for p in params)
        lines.append(f"- {spec['name']}({sig}): {spec['description']}")
        for p, desc in params.items():
            lines.append(f"    {p}: {desc}")
    return "\n".join(lines)


ROLE_BADGES = {
    "routine": " [Routine: done every day, in order]",
    "today": " [Today's list: what the user means to do today]",
    "menu": " [Menu: options to pick from when there's free time]",
    "list": " [List]",
    "project": " [Project: a one-off with an end]",
    "backlog": " [Project backlog: plan time on the project as one item ('<project> dev'), never single tickets]",
    "dropbox": " [Drop box: for new things with no obvious list; never suggest its items as they are]",
    "reference": " [Reference / wishlist: never suggest unless asked]",
}


class FrontierBrain:
    """Single-prompt brain for large-context frontier models."""

    def __init__(
        self,
        tools: PlanTools,
        inference_engine: InferenceEngine,
        tasks: Optional[List[TaskItem]] = None,
        active_notes: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.tools = tools
        self.inference_engine = inference_engine
        self.tasks = list(tasks or [])
        self.active_notes = list(active_notes or [])
        self.messages: List[Dict[str, Any]] = []
        self.reset()
        # --- a small helper call to the same engine and model (occasion_helper.py) ---
        from thirties_core.occasion_helper import offer
        offer(tools, self.ask_json)
        # --- end helper call ---

    def ask_json(self, system: str, user: str) -> Optional[Dict[str, Any]]:
        """One small call to the same engine and model as the main turn: the first JSON object of the reply, or
        None on any failure (an error, a timeout, the engine unavailable). It goes through the same engine, so it
        counts like any other call (the usage-limit fallback applies)."""
        from thirties_core.occasion_helper import first_json
        try:
            reply = self.inference_engine.chat(messages=[{"role": "system", "content": system},
                                                         {"role": "user", "content": user}])
        except Exception as e:
            logger.info("The helper call failed: %s", e)
            return None
        if not isinstance(reply, dict) or reply.get("error"):
            return None
        return first_json(reply.get("content"))

    def reset(self) -> None:
        """Start a fresh transcript with an up-to-date system prompt."""
        self.messages = [{"role": "system", "content": self.build_system_prompt()}]

    def _message_times(self) -> str:
        """For the prompt: when each earlier message the user saw was sent, so the model reads it rather than
        guessing. Drafts the user never saw and Python's own checks are left out."""
        tz = self.tools.day_plan.day_start.tzinfo
        rows = []
        for m in self.messages[1:]:
            text = " ".join(str(m.get("content") or "").split())
            if m.get("role") not in ("user", "assistant") or not m.get("at") or not text or m.get("hidden") \
                    or self._is_check(m):
                continue
            try:
                when = datetime.fromisoformat(str(m["at"])).astimezone(tz)
            except ValueError:
                continue
            rows.append(f"- {format_clock(when)}, {'the user' if m['role'] == 'user' else 'you'}: {text[:120]}")
        if not rows:
            return ""
        return ("WHEN EACH EARLIER MESSAGE WAS SENT (for you only: when you mention one, say it by this time, as its "
                "part of the day or its half hour, never to the minute; a message not listed has no recorded time, so "
                "never name one for it)\n" + "\n".join(rows) + "\n\n")

    def _is_today(self, now) -> bool:
        plan = self.tools.day_plan
        gen = self.tools.scheduler.config.general if self.tools.scheduler else None
        lat = gen.latitude if gen else 40.7128
        lon = gen.longitude if gen else -74.0060
        return plan.target_date == get_diurnal_date(now, lat=lat, lon=lon, tz_name=plan.day_start.tzinfo)

    def build_system_prompt(self) -> str:
        plan = self.tools.day_plan
        now = get_current_time(plan.day_start.tzinfo)
        is_today = self._is_today(now)
        date_str = plan.target_date.strftime("%A, %B %d, %Y")
        if is_today:
            when = f"Today, {date_str}. Current time: {format_clock(now)}."
        else:
            when = f"{date_str} (not today; today is {now.strftime('%A, %B %d')})."

        avail = plan.availability(now if is_today else None)
        services = self.tools.services
        assistant = self.tools.assistant
        from thirties_core.extras import prompt_section as extras_section  # first: it may add to the profile
        extras = extras_section(self.tools)  # observations, media queue, goal deadlines, people (extras.py)
        extras = f"{extras}\n\n" if extras else ""
        profile = services.profile.as_markdown() if services and not services.profile.is_empty() else "(empty so far)"
        stale = goal_check_lines(services.profile.sections(), self.tools.today()) if services else []
        if stale:  # A7: weeks-old goals fed stale suggestions (for a goal already finished)
            profile += ("\n\nGOALS TO CHECK (not checked in a while: before suggesting anything for one, ask the user "
                        "once whether it's still current, done or dropped, and record it with check_goal)\n"
                        + "\n".join(stale))
        memory = "\n".join(f"- {n.text} (id {n.id})" for n in services.memory.notes()) if services else ""
        stats = "\n".join(f"- {f}" for f in assistant.statistics_facts()) if assistant else ""
        weather = "\n".join(f"- {f}" for f in assistant.weather_facts()) if assistant else ""
        weather = weather or "- No forecast available."
        review_block = ""
        lasting = ""
        if assistant is not None and self.tools.scheduler:
            from thirties_core.item_facts import lasting_lines
            try:
                assistant.settle_moot()  # gone moot (lunch once dinner's begun) isn't waiting for an outcome
            except Exception as e:
                logger.warning("Settling moot items failed: %s", e)
            lines = lasting_lines(self.tools._lasting())
            if lines:
                lasting = ("EVERY DAY (what the user said holds for these items every day; the tools keep it)\n"
                           + "\n".join(f"- {line}" for line in lines) + "\n\n")
        if services and self.tools.scheduler:
            from thirties_core.stats import unreviewed_spans
            since = plan.day_start - timedelta(days=1)
            recent = self.tools.scheduler.state_db.get_spans_between(since, now)
            waiting = unreviewed_spans(recent, now, since)
            if waiting:
                review_block = ("WAITING FOR AN OUTCOME (record with log_outcome when the user mentions them; don't ask "
                                "about them after each report: what's not marked yet is shown with the status)\n"
                                + "\n".join(f"- {s.start_dt.strftime('%a')} {format_clock(s.start_dt)} – "
                                             f"{format_clock(s.end_dt)}: {s.title} id={s.id}" for s in waiting[-8:])
                                + "\n\n")

        day_setup = (assistant.routines.prompt_section() + assistant.proposals.prompt_section()) if assistant else ""
        if assistant:  # what the user chose to break down (break_down.py)
            from thirties_core.break_down import prompt_section as break_down_section
            day_setup += break_down_section(assistant)
        from thirties_core.not_yours import invite_lines  # names whose calendar someone else's invite is on
        invites = "\n".join(f"- {line}" for line in invite_lines(plan, self.tools.ambiguous_events)) or "None"
        from thirties_core.morning import POLICY as policy  # the one written rule for proposing (A1)
        from thirties_core.others_events import prompt_rule  # other people's calendar events (others_events.py)
        others_rule = prompt_rule()

        return f"""You are Thirties, the user's personal planning assistant: an astute executive assistant for their time. You help them plan, re-plan and review their day in conversation, absorb the messiness of real life, and keep their schedule accurate.

PLANNING DATE: {when}
Sunrise {format_clock(plan.sunrise)}, sunset {format_clock(plan.sunset)}. {self._sky(now)}
This day runs from {format_clock(plan.day_start)} until {format_clock(plan.day_end)} tomorrow morning, so tonight belongs to this day.
Open blocks (half-hour chunks) still ahead: {avail["daylight_open"]} in daylight, {avail["dark_open"]} in the dark.

{now_section(self.tools) if is_today else ""}{self._message_times()}SCHEDULE
{self.tools.schedule_text(upcoming_from=now if is_today else None)}

UNCONFIRMED CALENDAR INVITES
{invites}

{day_setup}{review_block}{lasting}BACKLOG
{self.build_backlog_summary()}

USER PROFILE (what you know about the user's life; keep it current with update_profile)
{profile}

YOUR MEMORY NOTES (how things work for this user; add with remember, delete with forget)
{memory}

STATISTICS (computed from what actually happened)
{stats}

WEATHER (from the forecast)
{weather}

{extras}HOW TO CHANGE THE SCHEDULE
Put each tool call on its own line, exactly in this form, then write your reply to the user as normal text:
CALL add_event {{"title": "Gardening", "start": "5:00 PM", "duration": "2 hours", "because": "two hours in the garden at 5"}}
CALL update_event {{"event": "<id from the schedule>", "shift": "+1 hour"}}
CALL lines are hidden from the user. After your calls run you will see their results and can adjust or finish your reply.

Tools:
{render_tool_specs()}

RULES
1. Change the schedule, or the structure of the user's lists in Joplin (break_down_task, edit_list_item), only when the user asks or agrees to a proposal (adding to a list: rule 9). When they ask for ideas or suggestions, propose and ask; don't change anything.
2. Every change needs a CALL. Never say something was scheduled, moved or removed without the CALL that does it. Tool results are notes for you, not the user: never quote them or mention tools; say what happened, or ask what you need to, in your own words.
3. Give times as clock times. Say AM or PM only where it could be mistaken: the first time if it isn't obvious from the current time, whenever a time crosses noon or midnight from the one before, and on a bare hour that's ambiguous. Never work out end times or durations yourself; the tools do all time math and report the exact result.
4. Use the statistics: propose durations from how long things really take the user, and if a heads-up says the day holds more than they usually get through, say so once, kindly, and offer what could move.
5. To change something already on the schedule, use update_event or remove_event with its id. For example, "work went til 4:30" means updating the work item's end. Don't recreate items, and don't touch items the user didn't mention.
6. A result starting with {NOT_DONE} means nothing changed. Tell the user plainly and offer a way forward. Calendar commitments are never overwritten.
7. Daylight and dark are physical facts, not rules about what belongs when. Only consider them when it physically matters (a daylight hike) or the user has said what they prefer.
8. Changes are live the moment you make them. There is no separate step to save, confirm, "lock in" or finalize a plan, so never offer one.
9. Scheduling versus lists. When the user names something to do, call match_backlog. "Same task": schedule it with that task_id. "Only similar": don't assume or correct them; ask whether it's that item (quote it) or something new. "Nothing": schedule it as a new item. Add to a list (add_task) when they ask, or when it's clearly a to-do they'll want to track (not meals or routines). Choose the list and section yourself, with a short reason, and say where it went; never ask the user where it goes. When no list is obvious, use the drop box. When they want it both added and scheduled, do both in one add_task call by giving it start (and duration), so the two stay linked. Every new item cites the user's exact words it comes from in because; its name is yours to choose. With no words of theirs to cite, don't add it: ask.
10. Backlog: bring it up only when the user is deciding how to use open time or asks for ideas, not after every change. Recommend specific, concrete actions, not whole notes or reference lists such as shopping lists. Each note says what it's for, and items say how pressing they are (wanted as soon as possible, wanted in the next few days, a due date; unmarked is no hurry): favour what's pressing, and never push someday or reference items. When the user says how pressing something is, record it with set_urgency. For an item deferred 3 or more times, be honest and kind once in a conversation: suggest a single 30-minute start, breaking it down, or dropping it. Don't repeat it if the user didn't take it up.
11. Answer in the user's terms, with only what this reply needs. Use their units: if they count in chunks or blocks, so do you ("you have two open chunks at 3:00 PM"); otherwise use clock times and hours. Mention how long something takes only when length or fit is the question. List a plan (a proposal too) one item per line, start time first, in the user's own short words ("- 4:00 PM – Feed fish"), with no durations; each item ends when the next begins, so add an end time only before a gap ("(until 7:00 PM)"). Never write a running paragraph of times, and never mention ids or block numbers. Be concise.
12. Track what actually happens. When the user says they did, finished or skipped something, record it with log_outcome for the items their words are about (doing Journal doesn't mean the shower happened). When their words tell of a done but not plainly which item (a bare "done" right after you listed several open items), mark the one they most likely mean and say so in a way they can undo ("Marked X done; say if you meant another"). Never make them confirm a done or repeat it. Started isn't finished: when they say what they're doing now or that something has begun ("I'm in the shower", "heading out for errands", "the dishwasher's going"), or ask for rest now, call doing_now; mark it done only once they say it's over. A gap they ask for (rest, a break, a nap) goes on as its own item named for it ("Rest"): it keeps its time, and what was planned there moves out. Ask about something not reported at most once; if they don't say, leave it. Pass started or finished only when they said the time; otherwise leave them out and Python works out when. For something worked on over time (project time), done means a session and it stays open; pass finished_for_good=true only when they say it's finished for good. After the user reports something, don't ask about the rest of the block or other items they didn't mention ("Did you get to X?"): what's not marked yet is shown with the status. If they mention how they feel, log_energy. When they say when they woke, went to sleep, or started or finished work (or answer the morning or end-of-work question), record it with log_day_times.
13. Learn. When the user tells you something lasting about their life (a habit, a goal, a preference), or asks you to remember it, add it with update_profile and mention that you did. When they tell you how something works for them (what a notebook or section means, how they like things done), save it with remember. Never save what you only guessed from a passing remark. Your notes and the profile are shown to you in every conversation.
14. Other days: every schedule tool takes a day ("tomorrow", "Monday", "Oct 20"). Use it for anything not on the day being planned; it goes on that day's plan and reaches the Today list that morning (say so). Things for a later day with no time: add_to_today with that day. Bigger picture: the user's lists are their goals, and each item belongs to a horizon (day, week, month, year, someday; set_horizon when they say so). A week plan is items dropped into the week (or onto a day of it), a month plan into the month, a year plan into the year; pin_to_period and unpin_from_period change one item, show_period answers "what's on this week?", and planning a week, month or year is propose_period (Python lists the candidates and their facts; you pick and say why), applied with apply_period_proposal right away (no approval step: tell the user in one line what went in; they correct it if it's wrong). Plan days from the week's plan. Past conversations are available through search_history, list_conversations and read_conversation; use them when the user refers to an earlier discussion.
15. One item per thing. Things done at the same time (an audiobook during dinner, working on two things at once) are separate items sharing the time: just add each at its time. Never join two things into one title.
16. A task that keeps getting put off, or that the user calls too big or hard to start, can be broken into 3 to 6 small, concrete steps. Propose them; once the user agrees, write them with break_down_task, and offer to schedule the first step.
17. "Remind me to X" is a thing to do: set_reminder puts it on the plan (and today's list) with a ping when it's time; if they give no time, choose a sensible one and say it. Only a bare message the user asks for as a ping or alarm ("ping me at 3 that the laundry's done") is ping_only; call that a ping or an alarm, never a "reminder". If they say there's no need to keep asking about something, or to stop check-ins, use mute_checkins. If they say a mute was wrong, or ask to resume, use unmute_checkins.
18. Mornings: an unmarked item from yesterday is unknown, not "not done", and never moves into today by itself. Meals, routines, appointments and anything tied to a time that's gone simply end. The schedule just IS the schedule: what you propose (propose_day, propose_leftovers) takes effect at once, with no approval step, and you tell the user as a notice, not a question. Leftovers are kept at a natural horizon (today's go straight on the plan) or proposed for dropping (applied by itself after that day). A drop only means \"stop rolling it over\": the item stays on its list, untouched. Archive it (leftover_action archive) only when the user explicitly wants it gone (\"delete it\", \"I'm never doing that\"); a bare \"drop it\" is a drop. Everything is in the app's Before today tab, and you act on the same records: when the user says one is done, keep it, drop it, accept it or bring it back, call leftover_action; to move one, place_leftover. A yes or 👍 accepts the proposals as they stand (apply_rollover). Ask only when something meant for today doesn't fit. Never ask about the same item again.
19. Time is told in half-hour blocks, like reading a piano roll. Items keep their real times (the tools show them, with the block each is in); blocks are how the day is shown. A schedule or list is told by blocks: each row is a block and what's in it, things at the same time joined with + ("- 10:30 – Personal call + Break"); when one thing ends and another begins within a block, the block's time, then each on its own line; consecutive blocks with the same things are one row. Asked about one item, give its real time and its block ("Your Uber is at 12:15, in the 12:00 block"). Never change a time the user gave: 12:15 is never "12:00". Exact minutes for everything only if asked (list_schedule exact=true). Never put the word "thirty" next to a time: say block or chunk. Things set by someone else's clock (appointments, rides, flights, shows, calls at a set time) are fixed: they keep their exact time and never slide; for an unclear case, say fixed=true or false when adding it. The time now is said as it is ("it's 8:41"), never as its block. Items may overlap, nest or coincide: adding or moving one never moves, trims or splits another unless the user asks ("push everything to make room": push_later), and things sharing time are told as one entry ("Garden app dev + Recipe app dev"). Only fixed things conflict, and never a daily routine: it keeps its time and shares it. When a result says they may not go together and the user didn't say, ask once ("Alongside the shower, or push the shower and what follows back?"); if the answer holds generally, record it with set_sharing. A schedule keeps list_schedule's sections in its order: the user's items, then If there's time, then Not yours today last (someone else's events: never part of the user's day, never planned around or asked about; "hide that one" is hide_for_today).
20. Weather: when suggesting or planning something outdoors (a walk, a run, yard work, errands on foot), check the forecast; don't suggest it into rain or bad weather, and if the user plans it there (a tool result says "Weather heads-up"), mention it once, briefly, with a better time if there is one.
21. Today's list versus the plan: the list is what the user means to do today (their Today note); the plan is when. Everything put on today's plan goes on the list by itself. When the user says they'll do something today without a time, or agrees to a day's list, put it on the list with add_to_today. Nothing on today is left without a time: everything on today's list has its time on the plan (Python finds the room). When the day is full, Python moves the lowest-priority item on to the next day with room and the tool says so in one line: pass that line on. Only when the user explicitly asks to keep something without a time ("if there's time", "if I get to it", "on deck", "no rush", "pencil it in"), call add_to_today with only_if_time=true: it goes under If there's time on today's list, the one section without times, and Thirties mentions it once when a free stretch fits. Mornings start from the list.
22. The plan changes only when the user asks or agrees. When they report running late or behind, record what happened (log_outcome) and leave the rest of the plan alone; speak up only if a fixed commitment (calendar, work) is at risk, once. "Push everything back" by an amount is push_back (it moves what's past and not marked too). When they ask for a new plan ("replan", "you sort it out"), call redraw: Python lays the rest of today out again in order, around fixed things, and says what doesn't fit; then show the new plan; what didn't fit waits for tomorrow by itself, so say where it went, and never offer to drop something because the day ran out (dropping is the user's idea, never the day's edge). Record constraints with set_item_facts as soon as the user states them (after X, not before a time, needs daylight, stays put) so every redraw keeps them; when they say it holds every day ("dinner takes an hour", "fish food is at 5", "Journal goes with the shower", "nothing outside until after the shower"), add every_day=true. Nothing is done until the user says so: items not reported are still to do. Whenever you say where the user is now ("you're in the 10:00 block…"), what's not marked yet (see NOW) goes right after it, under "Not marked yet:", one item per line, no times; never items of the block they're still in, and not again once the user has seen them.
23. Routines and daily anchors go on the plan by themselves each morning, at their usual times (ROUTINES AND DAILY ANCHORS shows what's on, each routine's schedule and placement): never ask whether to add them, and never say they haven't been scheduled. To place one differently today, or one that's only on request, use put_routine: give each thirty its start and the steps done in it, in the routine's order; that becomes its usual placement. When the user says it's how it always goes, set_routine confirm=true. Record how often a routine repeats, when it was last done, or where a part goes when its morning has no room (its fallback) with set_routine. A routine's parts keep their order: when an earlier part goes on late, the later parts follow right after it (Python places them so). Its fallback is your call, only when the morning really has no room for a part (it can't fit before a fixed start), or when the user asks. When the user says a routine part or item is in the wrong place or where it belongs, that is a request to move it: move what their words cover. To skip a routine, use skip_routine; if the user didn't say how, ask its three choices in one short question. Record meals and other things at a set time every day with set_anchor. Never add a routine to a list, as a new item, or as one block ("Morning routine"). Daily things and time on a project ("Garden app dev") don't carry over to the next day as unfinished.
24. When the user says their usual work or sleep hours (for some weekdays or all), record them with set_week; the plan's Work and Sleep follow. A change for one day only ("no work today", "my shift starts late today") changes that day: set_days_off for the day, or move that day's Work; Python makes the same change, said again on that weekday a week later, the usual week by itself. With no work on the plan, the user hasn't said they work that day: never assume they do.
25. Proposing the day: when DAY OPTIONS is shown, nothing has been proposed for the day yet. Once the user's request is handled, propose one short, concrete plan for the free time with propose_day: a few things that matter now (due dates and urgency first, not the whole backlog), each at a start in the free time, and say why in one line, in plain words the user would use (never an internal label such as for now, urgency, horizon or pool, nor a bare count such as 2 of 7: name the items). {policy} On changes, propose_day again. Don't propose again once they pass.
26. Something to do with no time given, or "later": call slot_options and pick a slot with a short reason (a quieter part of the day, not crammed against dinner); "later" is a real gap later on.
27. Showing the schedule: call show_schedule; the app puts the standard view (Now, The rest of today, Not marked yet, and If there's time when the user put something there) under your reply by itself, once. Never copy it, lay the day out yourself, or list rows; write only your own sentences. It's the default answer to a request to see the schedule, what's left or what's next. Pass whole_day=true only for a request that covers earlier today or the whole day. Whenever today's plan changes, the app adds the same view without a call.
28. Act, don't ask. When what the user wants is clear, do it and say in a few words what you did. Choose the obvious details yourself (a time, a length, which list) and say what you chose; they can change it. "Anywhere is fine" or "you decide" means you pick. Ask only when there are two real readings and guessing wrong would cost them something, and then ask one short question.
29. Never invent. Say only what the schedule, your tools and the conversation show. If you don't know why something happened (an item marked skipped, a copy, a change), say you don't know, check with item_history, and tell them what it shows; never make up a cause. Say you can do something only if one of your tools does it; if none does, say plainly that you can't yet. What you keep for yourself (memory notes) isn't something the user can open: say "I'll make a note of that", not that it's on a list they can see.
30. The user's notes: change their structure only when asked (edit_list_item: reword, split a line, add sub-items, move to another section or list). Done items move out of their lists into the month's Done note by themselves; if the user says something isn't done after all, reopen_item. When the user agrees to empty the drop box, dropbox_review shows each item with likely homes: propose one per line with a reason, and move each one they agree to. A list marked as sorted by time: offer once, when it fits, to split it by area (triage_list, then apply_triage_group per agreed group).
31. The app adds the standard view (Now, The rest of today, Not marked yet, then If there's time when it isn't empty) under replies. The app owns its sections and their format, and your wording never changes them; make no promises about them. Its Not marked yet part holds titles alone, without times, and appears whenever an item is unmarked; if the user dislikes how it looks, tell them plainly what the app shows, with no offer to turn it off. When the user asks outright to stop or bring back what's next after a change, or to have only the next few rows ("stop sending what's next", "show me what's next again"), call set_reply_extras, and only say it's done once that call went through.
32. {others_rule}
33. Never tell the user that Thirties can't do something, or that something it shows or sends can't be changed or turned off, unless a tool told you so this turn. Check first: call the tool that would do it (set_reply_extras for what Python adds to replies) and say what its result says.
34. A list item under a heading or under another line is named with where it sits, as the lists show it after "named:" (for example "Garden › Raised beds › order soil": its heading or parent line, then the item, at most two levels up). Name it that way whenever you mention it to the user: its own line alone means little. The tools still take its id or its own words.

Leaving today, and on-site days: something the user skips for today or puts on another day leaves today's list by itself (log_outcome skipped, update_event with to_day, or if_there_is_time with tomorrow, week or drop for anything on today's list); never say it can't be taken off. Taking something off today isn't dropping it for good: its own list keeps it; drop it for good only when the user says to drop it, isn't doing it ever or should forget it. When they say it happened (done, paid, sent) and to take it off, mark it done with log_outcome; never take it off first. When a tool says something is due today or overdue, tell them so. When the user's words make today an on-site day (their job is at the workplace, so nothing else fits during it), call work_on_site: Thirties moves what it had placed inside work itself; pass on where each went, as the tool says, and never ask whether to move them.
Skip means not today, never tomorrow: for a skip, log_outcome skipped is enough (Python takes it off today; a to-do comes back on a later day this week that Python picks, and a meal, routine step or appointment simply ends). Move something to tomorrow only when the user's words name tomorrow; when they name no day, use if_there_is_time with week, and pass on the day the tool gives.
Taking things off today: when the user takes things off today (remove_event, clear_time, or a new item put in their place), their lines leave today's list too. Read what the user meant for each, questions and complaints included (an angry question about items still being there is an ask for them to go), and pass today_list: off (the default: off today and gone), later or tomorrow (they want it back another day), or drop (dropped for good, only on their drop words). To keep something today, move it to another time instead of taking it off. The tool's answer says what happened to each line and whether it's due today; pass that on.
"""

    def _day_list_section(self) -> str:
        """The user's list for today (their Today note) and routines, with what's done."""
        if self.tools.scheduler is None:
            return ""
        from thirties_core.backlog import DONE_HIDES_DAYS
        from thirties_core.today import notes_with_role, with_done
        db = self.tools.scheduler.state_db
        roles = ListCatalog(db).roles()
        today = self.tools.today()
        done = {"today": db.completed_since(today - timedelta(days=DONE_HIDES_DAYS)),  # routines: today's only
                "routine": db.completed_since(today)}
        lines = []
        for role, heading in (("today", "Today's list"), ("routine", "Routine")):
            for note in notes_with_role(roles, self.active_notes, role):
                items = with_done(note, done[role], db)
                if items:
                    lines.append(f"{heading} ({note['title']}):")
                    lines += [f"- {'[done] ' if ticked else ''}{text}" for text, ticked in items]
        if not lines:
            return ""
        return ("TODAY'S LIST (what the user means to do today: their Today note, plus routines; ticks are what's "
                "done):\n" + "\n".join(lines))

    def build_backlog_summary(self) -> str:
        """The full Joplin backlog, organized by notebook and note, for a large-context model."""
        if not self.tasks and not self.active_notes:
            return "No active backlog tasks or notes found."

        # Map tasks by parent_note_id
        tasks_by_note: Dict[str, List[TaskItem]] = {}
        standalone_tasks: List[TaskItem] = []
        media = [t for t in self.tasks if t.id.startswith("media:")]
        for t in self.tasks:
            if t.id.startswith("media:"):
                continue
            if t.parent_note_id:
                tasks_by_note.setdefault(t.parent_note_id, []).append(t)
            else:
                standalone_tasks.append(t)

        # 1. Flag chronically deferred tasks first (tough love)
        chronic_lines = []
        for t in self.tasks:
            if t.deferred_count >= 3:
                parent_str = f" (in note '{t.parent_note_title}')" if t.parent_note_title else ""
                chronic_lines.append(
                    f"- [ID: {t.id}] '{t.title}'{parent_str} [Notebook: {t.source_notebook}] [CHRONIC DEFERRAL: {t.deferred_count}x]"
                )

        sections = []
        day_list = self._day_list_section()
        if day_list:
            sections.append(day_list)
        if self.tools.scheduler is not None:  # the week's, month's and year's plans (horizons.py)
            from thirties_core.horizons import HorizonPlanner
            plans = HorizonPlanner(self.tools, self.tasks).prompt_section()
            if plans:
                sections.append(plans)
        if chronic_lines:
            sections.append(
                "🔥 CHRONICALLY DEFERRED ITEMS (Urgent / Needs Tough Love or Single 30-Min Chunk):\n"
                + "\n".join(chronic_lines)
            )

        # 2. Present notes organized by notebook hierarchy
        notes_by_notebook: Dict[str, List[Dict[str, Any]]] = {}
        for note in self.active_notes:
            notes_by_notebook.setdefault(note["source_notebook"], []).append(note)

        # Include standalone tasks if any
        for t in standalone_tasks:
            if not any(n["id"] == t.id for n in self.active_notes):
                notes_by_notebook.setdefault(t.source_notebook, []).append({
                    "id": t.id,
                    "source_notebook": t.source_notebook,
                    "title": t.title,
                    "body": t.description,
                    "is_todo": True,
                    "todo_completed": 0,
                })

        roles: Dict[str, str] = {}
        placement = None
        if self.tools.scheduler is not None:
            all_roles = ListCatalog(self.tools.scheduler.state_db).roles()
            roles = {nid: r["role"] for nid, r in all_roles.items()}
            from thirties_core.placement_lists import Placement
            placement = Placement(self.active_notes, all_roles, self.tasks,
                                  getattr(self.tools.scheduler.config.joplin, "someday_sections", []))
        backlog_sections = []
        for nb_path in sorted(notes_by_notebook.keys()):
            nb_notes = notes_by_notebook[nb_path]
            nb_block = [f"=== NOTEBOOK: {nb_path} ==="]

            for note in nb_notes:
                n_id = note["id"]
                title = note["title"]
                body = note.get("body", "")
                is_todo = note.get("is_todo", False)
                todo_comp = note.get("todo_completed", 0)

                note_tasks = tasks_by_note.get(n_id, [])

                # What the note is for: the role the user confirmed or the model guessed, else a guess from the title
                lower_title = title.lower()
                if roles.get(n_id) in ROLE_BADGES:
                    note_type_badge = ROLE_BADGES[roles[n_id]]
                elif "routine" in lower_title:
                    note_type_badge = " [Daily Routine Checklist]"
                elif "shopping" in lower_title or "wishlist" in lower_title or "purchas" in lower_title:
                    note_type_badge = " [Reference / Purchasing Wishlist - NOT an immediate daily errand]"
                elif "errand" in lower_title or "admin" in lower_title:
                    note_type_badge = " [Active Admin & Logistics Bucket]"
                elif is_todo:
                    note_type_badge = " [To-Do Note]"
                else:
                    note_type_badge = " [Note]"
                if placement is not None:  # a list sorted by time: offered a split by area, once
                    from thirties_core.placement_lists import triage_offer_badge
                    note_type_badge += triage_offer_badge(placement, self.tools.scheduler.state_db, note)

                nb_block.append(f"### Note: \"{title}\"{note_type_badge}")

                if note_tasks:
                    from thirties_core.item_path import label, parents
                    by_task = {t.id: t for t in note_tasks}
                    nb_block.append("  Open checklist items:")
                    depth: Dict[str, int] = {}
                    current_section = None
                    for t in note_tasks:
                        if t.section != current_section:
                            current_section = t.section
                            if t.section:
                                tag = " (SOMEDAY/MAYBE: not committed; don't suggest unless asked)" if t.someday else ""
                                nb_block.append(f"  Section '{t.section}'{tag}:")
                        depth[t.id] = depth.get(t.parent_task_id, -1) + 1 if t.parent_task_id else 0
                        indent = "  " * (depth[t.id] + 1)
                        def_str = f" [Deferred: {t.deferred_count}x]" if t.deferred_count > 0 else ""
                        maybe = " [someday]" if t.someday and not t.section else ""
                        pressing = pressing_words(t) if t.urgency in ("now", "soon") or t.due else (
                            "this month" if t.horizon == "month" else "")  # the horizon urgency can't say
                        if getattr(t, "guessed", False) and not t.due:
                            pressing = ""  # only Thirties' own guess: unmarked, never pressing
                        pressing = f" [{pressing}]" if pressing and t.list_role not in ("today", "routine") else ""
                        # a nested item: how it's named to the user (item_path.py)
                        named = label(t, by_task) if parents(t, by_task) else ""
                        named = f" (named: {named})" if named else ""
                        # --- how the line reads (list_reading.py): kind, state, container, blocker, notes ---
                        from thirties_core.list_reading import prompt_tag
                        reading = prompt_tag(t, note_tasks)
                        # --- end how the line reads ---
                        nb_block.append(f"{indent}- [ID: {t.id}] '{t.title}'{named}{def_str}{maybe}{pressing}{reading}")
                elif is_todo and todo_comp == 0:
                    nb_block.append(f"  - [ID: {n_id}] Single Actionable Item: '{title}'")
                    if body.strip() and is_private_note(note):
                        nb_block.append("    (Private note: details not shown.)")
                    elif body.strip():
                        excerpt = "\n    ".join(body.strip().splitlines()[:5])
                        nb_block.append(f"    Details:\n    {excerpt}")
                elif body.strip() and is_private_note(note):
                    nb_block.append("    (Private note: contents not shown.)")
                elif body.strip():
                    clean_lines = [l for l in body.splitlines() if l.strip()]
                    if clean_lines:
                        excerpt = "\n    ".join(clean_lines[:8])
                        nb_block.append(f"    Contents:\n    {excerpt}")

                nb_block.append("")

            backlog_sections.append("\n".join(nb_block))

        if backlog_sections:
            sections.append("ACTIVE JOPLIN NOTEBOOK HIERARCHY & TASK BACKLOG:\n" + "\n\n".join(backlog_sections))
        # --- how the lists read (list_reading.py) ---
        if backlog_sections:
            sections.append(
            "HOW THE LISTS READ: an item's bracket says what Python read from its line. Only plain entries are "
            "offered or planned. A container's items are planned, never the container itself (name it with its "
            "progress, as given). Something dropped, closed, on hold, kept for reference or left open under a "
            "finished item is never offered; an undecided item or an option is offered as a decision; 'later, as a "
            "conversation' is something to talk about, not to schedule; an item waiting on something isn't for now "
            "(when only its last step waits, the rest can be done now). What the note's text shows about progress "
            "is evidence only: never tick anything because of it.")
        if self.tools.scheduler is not None:
            from thirties_core.list_reading import once_lines
            once = once_lines(self.tools.scheduler.state_db, self.tasks, self.active_notes,
                              someday_sections=getattr(self.tools.scheduler.config.joplin, "someday_sections", []))
            if once:
                sections.append("RAISE ONCE (in passing, at a natural moment; each is shown this one time):\n"
                                + "\n".join(once))
        # --- end how the lists read ---
        if media:
            by_list: Dict[str, List[TaskItem]] = {}
            for t in media:
                by_list.setdefault(t.source_notebook.split(" > ")[-1], []).append(t)
            import random
            pick = random.Random(date.today().toordinal())  # a different handful each day
            sections.append("WISHLISTS FROM THE USER'S PUBLIC ACCOUNTS (Goodreads, Letterboxd, Serializd; read-only, "
                            "synced automatically; separate from Joplin). A sample of each; search_backlog finds the "
                            "rest:\n" + "\n".join(
                                f"=== {name} ({len(items)} in all) ===\n" + "\n".join(
                                    f"- [ID: {t.id}] {t.title}" for t in (items if len(items) <= 15 else pick.sample(items, 15)))
                                for name, items in by_list.items()))
        elif self.tasks:
            # Fallback if active_notes was empty
            task_lines = [
                f"- [ID: {t.id}] '{t.title}' [Notebook: {t.source_notebook}]"
                for t in self.tasks[:50]
            ]
            sections.append("ACTIVE TASKS:\n" + "\n".join(task_lines))

        return "\n\n".join(sections)

    _OUTCOME_CLAIM_RE = re.compile(r"\b(marked|logged|recorded|checked (?:it |that |them )?off|ticked)\b[^.!?\n]*"
                                   r"\b(done|finished|complete|skipped)\b|\b(logged|marked) (?:it|that|them|both)\b",
                                   re.IGNORECASE)

    # "it isn't actually marked done", "hasn't been logged as finished": saying it's NOT recorded is no claim
    _NEGATED_RE = re.compile(r"(?:n't|\bnot|\bnever|\bnothing|\bno)\b(?:\s+\w+){0,3}\s*$", re.IGNORECASE)

    def _claims_outcome(self, text: str) -> bool:
        text = text or ""
        if self._ADMITS_RE.search(text):
            return False
        return any(not self._NEGATED_RE.search(text[max(0, m.start() - 40):m.start()])
                   and not re.search(r"\bnot\b|n't", m.group(0), re.IGNORECASE)
                   for m in self._OUTCOME_CLAIM_RE.finditer(text))

    def _claims_change(self, text: str) -> bool:
        """A sentence saying the plan changed; a question ("should I put it at 5?") claims nothing. "All fixed" and
        "everything is back in place" claim a change too (own_up.claims_all_fixed)."""
        from thirties_core.own_up import claims_all_fixed
        return any((ACTION_CLAIM_PATTERN.search(s) or claims_all_fixed(s)) and not s.rstrip().endswith("?")
                   for s in re.split(r"(?<=[.!?])\s+|\n+", text or ""))

    RECENT_TURNS = 8

    def _for_model(self) -> List[Dict[str, Any]]:
        """What the model sees: the whole conversation, but tool results only for the last few user turns
        (older ones are spent; the schedule in the prompt is current)."""
        user_idx = [i for i, m in enumerate(self.messages) if m.get("role") == "user"]
        cutoff = user_idx[-self.RECENT_TURNS] if len(user_idx) > self.RECENT_TURNS else 0
        return [self._with_unseen_note(m) for i, m in enumerate(self.messages) if (i >= cutoff or m.get("role") != "tool")
                and not m.get("display_only") and not m.get("spent")]

    # every send-back is about a draft the user never saw: the rewrite is the first thing they read
    UNSEEN_NOTE = ("The user hasn't seen your draft, only what you write now: write the reply fresh, never as a "
                   "correction of an earlier message (no \"correct\", \"earlier\", \"my last message\", \"the first "
                   "time\").")

    _REDO_RE = re.compile(r"\b(?:your reply says|correct your reply|rewrite (?:it|your reply)|write (?:it|your reply) "
                          r"(?:fresh|again)|corrects a message|didn't go through)\b", re.IGNORECASE)

    def _with_unseen_note(self, m: Dict[str, Any]) -> Dict[str, Any]:
        """A send-back asking to fix what a reply said carries the note (the rewrite is the first thing the user
        reads); other checks ("you didn't write a reply") are shown as they are."""
        text = str(m.get("content") or "")
        if not self._is_check(m) or self.UNSEEN_NOTE in text or not self._REDO_RE.search(text):
            return m
        return {**m, "content": str(m.get("content") or "").rstrip() + " " + self.UNSEEN_NOTE}

    def _bookkeeping_on(self) -> bool:
        """The turn bookkeeping (failures per item, every round checked, spent drafts): off by default until its
        study passes ([assistant] turn_bookkeeping)."""
        scheduler = getattr(self.tools, "scheduler", None)
        return bool(getattr(getattr(getattr(scheduler, "config", None), "assistant", None), "turn_bookkeeping", False))

    def respond(self, user_text: str) -> str:
        """Run one user turn: call the model, execute its tool calls, verify, reply."""
        if not self._bookkeeping_on():
            return self._respond_baseline(user_text)
        return self._respond_checked(user_text)

    def _respond_checked(self, user_text: str) -> str:
        """One turn with the turn bookkeeping on."""
        self.messages[0] = {"role": "system", "content": self.build_system_prompt()}
        changes_before = self.tools.successful_changes
        outcomes_before = self.tools.outcomes_logged
        self._times_before = self._times()
        self._status_before = self._statuses()
        logger.info("[FrontierBrain] User message: %r", user_text)
        self.messages.append({"role": "user", "content": user_text})
        turn_start = len(self.messages)

        self._reflected = False
        reply = self._rounds(MAX_ROUNDS, changes_before, outcomes_before)

        for attempt in range(2):  # a reply that's empty, or that narrates the conversation, gets one more try
            problem = "empty" if not reply.strip() else ("meta" if self._is_meta(reply) else "")
            if not problem or attempt == 1:
                break
            logger.warning("[FrontierBrain] Reply was %s; asking again: %r", problem, reply[:300])
            self._check(
                "You didn't write a reply. Reply to the user now, in your own words." if problem == "empty" else
                "Your reply described the conversation, your instructions or tool results. Write only the "
                "message the user should read.")
            reply = self._rounds(2, changes_before, outcomes_before)  # its calls are checked like any others
        reply = self._plain_words(reply, lambda: self._rounds(2, changes_before, outcomes_before))  # internal_text.py
        reply = self._fed_text_out(reply, changes_before)  # an echoed result goes whole (internal_text.py)
        if self._is_meta(reply):
            kept = [p for p in re.split(r"\n\s*\n", reply) if not self._is_meta(p)]
            reply = "\n\n".join(kept)
        reply = "\n".join(line for line in reply.splitlines()  # tool syntax never reaches the user, however it came
                          if not re.match(r"^\s*`*\s*CALL\s+[A-Za-z_]\w*", line)).strip()
        if self._claims_change(reply) and self.tools.successful_changes == changes_before:
            logger.warning("[FrontierBrain] Unverified change claim, asking for a fresh reply: %r", reply)
            reply = self._say_true(changes_before)
        if not reply.strip():
            reply = ("Done." if self.tools.successful_changes > changes_before else
                     "Sorry, I lost my train of thought there. Could you say that again?")
        reply = self._moves_made(turn_start, reply, lambda: self._rounds(2, changes_before, outcomes_before))
        reply = self._own_up(turn_start, reply, changes_before, outcomes_before)
        reply = self._no_unseen_correction(turn_start, reply, lambda: self._rounds(2, changes_before, outcomes_before))
        reply = self._fed_text_out(reply, changes_before)  # and nothing a rewrite brought back (internal_text.py)
        reply = self._not_holding(turn_start, reply, lambda: self._rounds(2, changes_before, outcomes_before))
        reply = self._kept_for_engineers(reply, lambda: self._rounds(2, changes_before, outcomes_before))
        reply = self._tidy(turn_start, reply, lambda: self._rounds(2, changes_before, outcomes_before), changes_before)
        self._show_only(turn_start, reply)
        for m in self.messages[turn_start:]:  # drafts and checks are spent: later turns see the final reply only
            if m.get("role") == "assistant" and m.get("hidden"):
                if m.get("tool_calls"):
                    m["content"] = ""  # the calls stay (their results answer them); the draft's words go
                else:
                    m["spent"] = True
            elif self._is_check(m):
                m["spent"] = True
            elif m.get("display_only"):  # the drafts are spent: the reply as shown is what the model said
                del m["display_only"]
        for m in self.messages[:turn_start]:  # a check placed before the message (the day's first turn) is spent too
            if self._is_check(m):
                m["spent"] = True
        logger.info("[FrontierBrain] Final reply: %r", reply)
        return reply

    # --- what the model was fed never reaches the user (internal_text.py) ---

    def _plain_words(self, reply: str, retry) -> str:
        """A reply that talks about the app's own workings (CALL lines, tools, commands) goes back once for plain
        words; what a second try still says about them is taken out after (_fed_text_out)."""
        from thirties_core.internal_text import talks_internals
        if not talks_internals(reply):
            return reply
        logger.warning("[FrontierBrain] Reply talked about the app's workings; asking again: %r", reply[:300])
        self._check("Your reply talks about calls, tools or commands, which the user never sees and can't act on. "
                    "Write it again for the user in plain words: what changed on their plan, what didn't and why "
                    "in their terms, or the one thing you need from them.")
        fresh = (retry() or "").strip()
        return fresh if fresh and not talks_internals(fresh) else reply

    def _fed_text_out(self, reply: str, changes_before: int) -> str:
        """The reply without an echoed tool result, a check, or a call written out; one that was nothing else says
        what happened in a plain line."""
        from thirties_core.internal_text import strip_internal
        clean = strip_internal(reply)
        if clean == reply:
            return reply
        logger.warning("[FrontierBrain] Reply held text for the model only; taken out: %r", reply[:300])
        if clean.strip():
            return clean
        if self.tools.successful_changes > changes_before:
            return "Done."
        from thirties_core.internal_text import talks_internals  # a refusal in the app's terms: say it plainly
        return (self.UNCHANGED_LINE if talks_internals(reply) else
                "Sorry, I lost my train of thought there. Could you say that again?")

    # --- end what the model was fed ---

    CHECK_PREFIX = "[System check, not from the user] "

    def _check(self, text: str) -> None:
        self.messages.append({"role": "user", "content": self.CHECK_PREFIX + text})

    def _is_check(self, m: Dict[str, Any]) -> bool:
        return m.get("role") == "user" and str(m.get("content") or "").startswith(self.CHECK_PREFIX)

    def _rounds(self, limit: int, changes_before: int, outcomes_before: int) -> str:
        """Model rounds until it has replied: each round's calls run, read-only lookups and refusals get a
        follow-up, and a reply that claims what didn't happen is sent back once. Every round of a turn, retries
        included, goes through here, so no call escapes these checks. A last round that made calls with only a line
        saying it's looking beside them gets one round more to answer. Returns the reply."""
        reply = ""
        from thirties_core.holding_lines import only_holds  # a lookup is always answered (holding_lines.py)
        for round_index in range(limit + 1):
            response = self.inference_engine.chat(messages=self._for_model(), tools=TOOL_SPECS)
            content = response.get("content", "") or ""
            calls = response.get("tool_calls", []) or []
            self.messages.append(response)
            if content.strip():
                reply = content
            results = []
            for call in calls:
                name, args = call.get("name"), call.get("arguments", {})
                with model_call():  # the model's own call (change_scope.py)
                    output = self.tools.execute(name, dict(args) if isinstance(args, dict) else args)
                logger.info("[FrontierBrain] %s(%s) -> %s", name, args, output)
                results.append(output)
                self.messages.append({"role": "tool", "tool_call_id": call.get("id", "call"), "name": name,
                                      "content": output, "arguments": args})
            changed = self.tools.successful_changes - changes_before
            if not calls:
                if (self._claims_outcome(content) and self.tools.outcomes_logged == outcomes_before
                        and not self._reflected):  # 10-30 sim: the model said a session was logged as done, with nothing logged
                    self._reflected = True
                    logger.warning("[FrontierBrain] Model claimed an outcome without log_outcome; asking it to correct.")
                    self._check("Your reply says something was marked done (or skipped), but no log_outcome went "
                                "through this turn, so nothing was recorded. Call log_outcome now, or correct your reply.")
                    continue
                if self._claims_change(content) and changed == 0 and not self._reflected:
                    self._reflected = True
                    logger.warning("[FrontierBrain] Model claimed a change without tool calls; asking it to correct.")
                    self._check("Your reply says the schedule changed, but you made no CALLs, so nothing changed. "
                                "Make the CALLs now, or correct your reply.")
                    continue
                break
            needs_follow_up = (any(c.get("name") in READ_ONLY_TOOLS for c in calls)
                               or any(r.startswith(NOT_DONE) for r in results) or not content.strip())
            # the last round looked something up and only said it would: one more, to answer from the result
            unanswered = only_holds(content)
            if not needs_follow_up or round_index == limit or (round_index == limit - 1 and not unanswered):
                break
            if any(r.startswith(NOT_DONE) for r in results):
                reply = ""  # the draft was written before the refusal; don't show it
        return reply

    def _show_only(self, turn_start: int, reply: str) -> None:
        """A turn can take several rounds (a draft, tool calls, a retry); the model keeps them all as context, but
        the user sees one message: the final reply. Without this the chat showed every draft, repeated or
        half-finished ones and the model's notes to itself included."""
        rounds = [m for m in self.messages[turn_start:] if m.get("role") == "assistant"]
        for m in rounds:
            m["hidden"] = True
        final = next((m for m in reversed(rounds) if str(m.get("content", "")).strip() == reply.strip()), None)
        if final is not None:
            final.pop("hidden", None)
        else:  # the reply was cleaned up or replaced: show it, but don't feed it back to the model as said twice
            self.messages.append({"role": "assistant", "content": reply, "display_only": True})

    _ADMITS_RE = re.compile(r"\b(couldn'?t|could not|can'?t|cannot|wasn'?t able|unable|didn'?t|failed|not able|"
                            r"won'?t|isn'?t on|not on (?:the|your))\b", re.IGNORECASE)
    _CLAIMS_RE = re.compile(r"\b(marked|logged|noted|all set|done|added|moved|scheduled|updated|cleaned|cleared|"
                            r"removed|checked (?:it )?off|ticked|taken care of|all fixed|fixed (?:it|that|them|"
                            r"everything)|back in place|everything is (?:back|right|where|set|fixed|in place))\b",
                            re.IGNORECASE)

    def _item_of(self, m: Dict[str, Any]) -> str:
        """The item a tool call was about (its title, when it was named by id), for telling failures apart."""
        args = m.get("arguments") if isinstance(m.get("arguments"), dict) else {}
        ref = next((args.get(k) for k in ("event", "title", "item", "task", "target", "routine") if args.get(k)), "")
        if isinstance(ref, list):
            ref = " ".join(map(str, ref))
        span = None
        try:
            span = self.tools.day_plan.get_span(str(ref)) if ref else None
        except Exception:
            span = None
        return (span.title if span is not None else str(ref)).strip().lower()

    def _failed_changes(self, turn_start: int) -> List[str]:
        """Changes that failed this turn, per item: a failure stays unless a later call on the same item worked
        (moving a break with a fresh add of it is getting around the refusal). Something else working later doesn't
        cancel it ("move A refused, add B" isn't "All set")."""
        failed_by_item: Dict[str, str] = {}
        joplin: List[str] = []
        for m in self.messages[turn_start:]:
            if m.get("role") != "tool" or m.get("name") in READ_ONLY_TOOLS:
                continue
            out, key = str(m.get("content") or ""), self._item_of(m) or f"<{m.get('name')}>"
            if out.startswith(NOT_DONE):
                failed_by_item[key] = out[len(NOT_DONE):].lstrip(": ").strip()
            else:
                failed_by_item.pop(key, None)
                note = re.search(r"\((couldn'?t [^:]*Joplin.*)\)\.?$", out)  # done here, but not in Joplin
                if note:
                    joplin.append(re.sub(r"\s+", " ", note.group(1)))
        return list(failed_by_item.values()) + joplin

    def _own_up(self, turn_start: int, reply: str, changes_before: int = 0, outcomes_before: int = 0) -> str:
        """A reply that says a change happened, when the change failed, gets one more try with the failures
        named ("It is marked done now" over a failed write, "All set" over three refusals: 10-22, 10-24). If it
        still claims it, the claim says what's still as it was, in place (plain_admit.py); nothing is added under it,
        and the model's tool wording never reaches the user."""
        failed = self._failed_changes(turn_start)
        if not failed or self._ADMITS_RE.search(reply) or not self._CLAIMS_RE.search(reply):
            return reply
        if self._own_up_line(turn_start, reply) is None:  # it already says so, or nothing failed for real
            return reply
        logger.warning("[FrontierBrain] Reply claims a change that failed: %r (%s)", reply[:200], failed)
        self._check("These didn't go through: " + " | ".join(failed) + ". Your reply says otherwise. "
                    + self._plain_check(turn_start, reply))
        fixed = self._rounds(2, changes_before, outcomes_before).strip()  # its calls are checked like any others
        if fixed and (self._ADMITS_RE.search(fixed) or not self._failed_changes(turn_start)
                      or self._own_up_line(turn_start, fixed) is None):
            return fixed
        return self._said_plainly(turn_start, reply)

    # --- the turn as it was before the bookkeeping (the switch's "off") ---

    def _respond_baseline(self, user_text: str) -> str:
        """The one-prompt turn as it was before the turn bookkeeping (kept verbatim while that waits for its study
        behind the turn_bookkeeping switch)."""
        self.messages[0] = {"role": "system", "content": self.build_system_prompt()}
        changes_before = self.tools.successful_changes
        outcomes_before = self.tools.outcomes_logged
        self._times_before = self._times()
        self._status_before = self._statuses()
        logger.info("[FrontierBrain] User message: %r", user_text)
        self.messages.append({"role": "user", "content": user_text})
        turn_start = len(self.messages)

        reply = ""
        last_results: List[str] = []
        reflected = False
        for round_index in range(MAX_ROUNDS):
            response = self.inference_engine.chat(messages=self._for_model(), tools=TOOL_SPECS)
            content = response.get("content", "") or ""
            calls = response.get("tool_calls", []) or []
            self.messages.append(response)
            if content.strip():
                reply = content

            results = []
            for call in calls:
                name, args = call.get("name"), call.get("arguments", {})
                with model_call():  # the model's own call (change_scope.py)
                    output = self.tools.execute(name, args)
                logger.info("[FrontierBrain] %s(%s) -> %s", name, args, output)
                results.append(output)
                self.messages.append({"role": "tool", "tool_call_id": call.get("id", "call"), "name": name, "content": output})
            if results:
                last_results = results

            changed = self.tools.successful_changes - changes_before
            if not calls:
                if (self._claims_outcome(content) and self.tools.outcomes_logged == outcomes_before
                        and not reflected):  # 10-30 sim: the model said a session was logged as done, with nothing logged
                    reflected = True
                    logger.warning("[FrontierBrain] Model claimed an outcome without log_outcome; asking it to correct.")
                    self.messages.append({"role": "user", "content": (
                        "[System check, not from the user] Your reply says something was marked done (or skipped), "
                        "but no log_outcome went through this turn, so nothing was recorded. Call log_outcome now, "
                        "or correct your reply."
                    )})
                    continue
                if self._claims_change(content) and changed == 0 and not reflected:
                    reflected = True
                    logger.warning("[FrontierBrain] Model claimed a change without tool calls; asking it to correct.")
                    self.messages.append({"role": "user", "content": (
                        "[System check, not from the user] Your reply says the schedule changed, but you made no "
                        "CALLs, so nothing changed. Make the CALLs now, or correct your reply."
                    )})
                    continue
                break
            needs_follow_up = (
                any(c.get("name") in READ_ONLY_TOOLS for c in calls)
                or any(r.startswith(NOT_DONE) for r in results)
                or not content.strip()
            )
            if not needs_follow_up or round_index == MAX_ROUNDS - 1:
                break
            if any(r.startswith(NOT_DONE) for r in results):
                reply = ""  # the draft was written before the refusal; don't show it

        for attempt in range(2):  # a reply that's empty, or that narrates the conversation, gets one more try
            problem = "empty" if not reply.strip() else ("meta" if self._is_meta(reply) else "")
            if not problem or attempt == 1:
                break
            logger.warning("[FrontierBrain] Reply was %s; asking again: %r", problem, reply[:300])
            self.messages.append({"role": "user", "content": (
                "[System check, not from the user] " + (
                    "You didn't write a reply. Reply to the user now, in your own words." if problem == "empty" else
                    "Your reply described the conversation, your instructions or tool results. Write only the "
                    "message the user should read.")
            )})
            response = self.inference_engine.chat(messages=self._for_model(), tools=TOOL_SPECS)
            self.messages.append(response)
            for call in response.get("tool_calls", []) or []:
                with model_call():  # the model's own call (change_scope.py)
                    output = self.tools.execute(call.get("name"), call.get("arguments", {}))
                self.messages.append({"role": "tool", "tool_call_id": call.get("id", "call"),
                                      "name": call.get("name"), "content": output})
            reply = response.get("content", "") or ""
        reply = self._plain_words(reply, self._one_more_round)  # no talk of calls or tools (internal_text.py)
        reply = self._fed_text_out(reply, changes_before)  # an echoed result goes whole (internal_text.py)
        if self._is_meta(reply):
            kept = [p for p in re.split(r"\n\s*\n", reply) if not self._is_meta(p)]
            reply = "\n\n".join(kept)
        reply = "\n".join(line for line in reply.splitlines()  # tool syntax never reaches the user, however it came
                          if not re.match(r"^\s*`*\s*CALL\s+[A-Za-z_]\w*", line)).strip()
        if self._claims_change(reply) and self.tools.successful_changes == changes_before:
            logger.warning("[FrontierBrain] Unverified change claim, asking for a fresh reply: %r", reply)
            reply = self._say_true(changes_before)
        if not reply.strip():
            reply = ("Done." if self.tools.successful_changes > changes_before else
                     "Sorry, I lost my train of thought there. Could you say that again?")
        reply = self._moves_made(turn_start, reply, self._one_more_round)
        reply = self._own_up_baseline(turn_start, reply)
        reply = self._no_unseen_correction(turn_start, reply, self._one_more_round)
        reply = self._fed_text_out(reply, changes_before)  # and nothing a rewrite brought back (internal_text.py)
        reply = self._not_holding(turn_start, reply, self._one_more_round)  # holding_lines.py
        reply = self._kept_for_engineers(reply, self._one_more_round)  # engineer_notes.py
        reply = self._tidy(turn_start, reply, self._one_more_round, changes_before)  # clean_prose.py
        self._show_only(turn_start, reply)
        logger.info("[FrontierBrain] Final reply: %r", reply)
        return reply


    def _failed_changes_baseline(self, turn_start: int) -> List[str]:
        """Changes that failed this turn with nothing after them that worked: a refusal the model got around
        another way (moving a break with a fresh add) isn't a failure the user needs to hear about."""
        failed: List[str] = []
        for m in self.messages[turn_start:]:
            if m.get("role") != "tool" or m.get("name") in READ_ONLY_TOOLS:
                continue
            out = str(m.get("content") or "")
            if out.startswith(NOT_DONE):
                failed.append(out[len(NOT_DONE):].lstrip(": ").strip())
            else:
                failed = []
                joplin = re.search(r"\((couldn'?t [^:]*Joplin.*)\)\.?$", out)  # done here, but not in Joplin
                if joplin:
                    failed.append(re.sub(r"\s+", " ", joplin.group(1)))
        return failed


    def _own_up_baseline(self, turn_start: int, reply: str) -> str:
        """A reply that says a change happened, when the change failed, gets one more try with the failures
        named ("It is marked done now" over a failed write, "All set" over three refusals: 10-22, 10-24). If it
        still claims it, the claim says what's still as it was, in place (plain_admit.py); nothing is added under it,
        and the model's tool wording never reaches the user."""
        failed = self._failed_changes_baseline(turn_start)
        if not failed or self._ADMITS_RE.search(reply) or not self._CLAIMS_RE.search(reply):
            return reply
        if self._own_up_line(turn_start, reply) is None:  # it already says so, or nothing failed for real
            return reply
        logger.warning("[FrontierBrain] Reply claims a change that failed: %r (%s)", reply[:200], failed)
        self.messages.append({"role": "user", "content": (
            "[System check, not from the user] These didn't go through: " + " | ".join(failed) + ". Your reply "
            "says otherwise. " + self._plain_check(turn_start, reply))})
        response = self.inference_engine.chat(messages=self._for_model(), tools=TOOL_SPECS)
        self.messages.append(response)
        for call in response.get("tool_calls", []) or []:
            with model_call():  # the model's own call (change_scope.py)
                output = self.tools.execute(call.get("name"), call.get("arguments", {}))
            self.messages.append({"role": "tool", "tool_call_id": call.get("id", "call"),
                                  "name": call.get("name"), "content": output})
        fixed = str(response.get("content") or "").strip()
        if fixed and (self._ADMITS_RE.search(fixed) or not self._failed_changes_baseline(turn_start)
                      or self._own_up_line(turn_start, fixed) is None):
            return fixed
        return self._said_plainly(turn_start, reply)

    # --- what didn't happen, said plainly, only when the reply doesn't say it (own_up.py) ---

    def _own_up_line(self, turn_start: int, reply: str) -> Optional[str]:
        from thirties_core.own_up import own_up_line
        try:
            return own_up_line(self.tools, self.messages, turn_start, READ_ONLY_TOOLS, NOT_DONE, reply)
        except Exception as e:  # the check never costs the user their reply
            logger.debug("Couldn't read what didn't happen: %s", e)
            return None

    # --- said in the reply itself, never as a note under it (plain_admit.py) ---

    def _plain_check(self, turn_start: int, reply: str) -> str:
        """The rewrite's instruction: the plain sentences to say, written fresh."""
        from thirties_core.own_up import still_lines
        from thirties_core.plain_admit import check_text
        try:
            lines = still_lines(self.tools, self.messages, turn_start, READ_ONLY_TOOLS, NOT_DONE, reply)
        except Exception as e:  # the check never costs the user their reply
            logger.debug("Couldn't word what didn't happen: %s", e)
            lines = []
        return check_text(lines) if lines else ("Rewrite it so it's true: say plainly what's still as it was. Write "
                                                "it fresh: the user never saw this draft.")

    def _said_plainly(self, turn_start: int, reply: str) -> str:
        """A rewrite that still claims it: the false sentence says what's still as it was, in place."""
        from thirties_core.own_up import BLANKET_RE, said_plainly
        claim = re.compile(f"(?:{self._CLAIMS_RE.pattern})|(?:{BLANKET_RE.pattern})", re.IGNORECASE)
        try:
            return said_plainly(self.tools, self.messages, turn_start, READ_ONLY_TOOLS, NOT_DONE, reply, claim)
        except Exception as e:  # the check never costs the user their reply
            logger.debug("Couldn't say what didn't happen: %s", e)
            return reply

    # --- end what didn't happen ---


    # --- a move the reply says happened, when nothing moved (11-03) ---

    _MOVED_RE = re.compile(r"\b(moved|shifted|pushed|bumped|slid|rescheduled|swapped|brought\s+(?:\w+\s+){0,3}?"
                           r"(?:up|forward|earlier))\b", re.IGNORECASE)
    _NOT_YET_RE = re.compile(r"\b(could|can|should|would|shall|might|may|to)\s+be\s+$|\b(if|want|wants|like|shall i|"
                             r"should i|could|can)\b[^.!?\n]*$", re.IGNORECASE)

    def _times(self) -> Dict[str, Any]:
        """Every span's times on the day being planned, by id: what a turn's moves are checked against."""
        plan = getattr(self.tools, "day_plan", None)
        return {s.id: (s.start_dt, s.end_dt) for s in (plan.spans if plan is not None else [])}

    def _named(self, sentence: str, whole: bool = False) -> List[Any]:
        """The plan's items a sentence names: every word of an item's title, or of a routine's name (its steps).
        ``whole``: only by the item's whole title, as written ("the goals are done" names no item)."""
        from thirties_core.item_facts import mentions, name_words
        said = name_words(sentence)
        plan = self.tools.day_plan
        titles = {s.id: self.tools._title(s) for s in plan.spans}
        named = [s for s in plan.spans if (re.search(rf"(?<![\w'’]){re.escape(titles[s.id])}(?![\w'’])", sentence,
                                                     re.IGNORECASE) if whole and titles[s.id]
                                           else not whole and mentions(sentence, titles[s.id], titles.values()))]
        assistant = getattr(self.tools, "assistant", None)
        if assistant is not None:
            try:
                for note in assistant._routines():
                    words = name_words(note.get("title", ""))
                    if words and words <= said:
                        steps = {t.lower() for t in assistant.routines.steps(note)}
                        steps |= assistant.routines.known_extras(note)
                        named += [s for s in plan.spans if s.title.strip().lower() in steps and s not in named]
            except Exception as e:  # the check never costs the user their reply
                logger.debug("Couldn't read the routines for the claim check: %s", e)
        return named

    # the tools that move things (or put them somewhere new): one that went through is a move made this turn
    MOVING_TOOLS = {"update_event", "swap_events", "push_back", "redraw", "undo_redraw", "put_routine", "add_event",
                    "add_to_today", "apply_day_proposal", "clear_time", "remove_event"}

    def _unmade_moves(self, turn_start: int, reply: str) -> List[str]:
        """What the reply says was moved this turn but wasn't: "Feed the fish is still at 9:00 AM" per named item, or
        "nothing" when it names none and no call that moves things went through. A sentence that asks, offers or
        admits is no claim."""
        before = getattr(self, "_times_before", None)
        if before is None:
            return []
        anything = any(m.get("role") == "tool" and m.get("name") in self.MOVING_TOOLS
                       and not str(m.get("content") or "").startswith(NOT_DONE) for m in self.messages[turn_start:])
        out: List[str] = []
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", reply or ""):
            m = self._MOVED_RE.search(sentence)
            if m is None or sentence.rstrip().endswith("?") or self._ADMITS_RE.search(sentence):
                continue
            lead = sentence[:m.start()]
            if self._NEGATED_RE.search(lead[-40:]) or self._NOT_YET_RE.search(lead):
                continue
            named = self._named(sentence)
            # --- an item skipped or ticked this turn was handled, never one that failed to move ---
            settled = getattr(self, "_status_before", None) or {}
            unsettled = [s for s in named if settled.get(s.id, s.status) == s.status]
            if named and not unsettled:
                continue
            named = unsettled
            # --- end handled ---
            if not named:
                if not anything:
                    out.append("nothing")
                continue
            still = [s for s in named if s.id in before and before[s.id] == (s.start_dt, s.end_dt)]
            if len(still) == len(named):  # none of what it names moved
                out += [f"{self.tools._title(s)} is still at {format_clock(s.start_dt)}" for s in still[:3]]
            # --- a claim about several items, checked part by part (claim_parts.py) ---
            elif still:
                for s in self._claimed_unchanged(sentence, self._MOVED_RE,
                                                 lambda s: s.id in before and before[s.id] == (s.start_dt, s.end_dt)
                                                 and settled.get(s.id, s.status) == s.status):
                    out.append(f"{self.tools._title(s)} is still at {format_clock(s.start_dt)}")
            # --- end part by part ---
        return list(dict.fromkeys(out))

    def _claimed_unchanged(self, sentence: str, claim_re, unchanged) -> List[Any]:
        """The items in the parts of a sentence its claim covers, where every item a part names is unchanged
        (claim_parts.py): "I moved the bench to 11 and the van to 6" with only the van moved gives the bench."""
        from thirties_core.claim_parts import claimed_parts
        out: List[Any] = []
        for part in claimed_parts(sentence, claim_re, lambda p: [self.tools._title(s) for s in self._named(p)]):
            named = [s for s in self._named(part) if not s.is_envelope]
            if named and all(unchanged(s) for s in named):
                out += [s for s in named if s not in out]
        return out[:3]

    # "Laundry is done", "the dishes are finished": a done said of a named item (beside "marked ... done")
    _SAID_DONE_RE = re.compile(r"\b(?:is|are|was|were|'s)\s+(?:now\s+|all\s+)?(?:marked\s+|logged\s+(?:as\s+)?)?"
                               r"(?:done|finished|complete)\b", re.IGNORECASE)

    def _statuses(self) -> Dict[str, Any]:
        plan = getattr(self.tools, "day_plan", None)
        return {s.id: s.status for s in (plan.spans if plan is not None else [])}

    def _unmade_dones(self, reply: str) -> List[str]:
        """Items the reply says are done (or marked) that weren't marked this turn: "Run the dishwasher isn't recorded as
        done" per item. Only where every item the sentence names is still unmarked (a sentence about two things, one
        of them marked, is left alone); a sentence naming nothing is the outcome check's."""
        from thirties_core.models import SpanStatus
        before = getattr(self, "_status_before", None)
        if before is None:
            return []
        out: List[str] = []
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", reply or ""):
            m = self._OUTCOME_CLAIM_RE.search(sentence) or self._SAID_DONE_RE.search(sentence)
            if m is None or sentence.rstrip().endswith("?") or self._ADMITS_RE.search(sentence):
                continue
            lead = sentence[:m.start()]
            if self._NEGATED_RE.search(lead[-40:]) or self._NOT_YET_RE.search(lead):
                continue
            # an item the reply says is done is named by its whole title, never by a word it shares with it
            named = [s for s in self._named(sentence, whole=True) if not s.is_envelope and s.source != "calendar"]
            if named and all(s.status == SpanStatus.PLANNED and before.get(s.id, SpanStatus.PLANNED) == SpanStatus.PLANNED
                             for s in named):
                out += [f"{self.tools._title(s)} isn't recorded as done" for s in named[:3]]
            # --- a claim about several items, checked part by part (claim_parts.py) ---
            elif len(named) > 1:
                claim = re.compile(f"(?:{self._OUTCOME_CLAIM_RE.pattern})|(?:{self._SAID_DONE_RE.pattern})",
                                   re.IGNORECASE)
                unmarked = [s for s in self._claimed_unchanged(
                    sentence, claim, lambda s: s.status == SpanStatus.PLANNED
                    and before.get(s.id, SpanStatus.PLANNED) == SpanStatus.PLANNED) if s.source != "calendar"
                    and s in named]
                out += [f"{self.tools._title(s)} isn't recorded as done" for s in unmarked]
            # --- end part by part ---
        return list(dict.fromkeys(out))

    # a clock time said in a reply: "3:30", "3:30 PM", "4pm", "noon"
    _CLOCK_RE = re.compile(r"\b(\d{1,2}):(\d{2})(?:\s*([ap])\.?m\b\.?)?|\b(\d{1,2})\s*([ap])\.?m\b\.?|\b(noon)\b", re.IGNORECASE)
    # where a time is about something other than an item on today's plan
    _ELSEWHERE_RE = re.compile(r"\b(tomorrow|yesterday|last night|next week|monday|tuesday|wednesday|thursday|friday|"
                               r"saturday|sunday|usually|every day|each day|ping|remind\w*|alarm|arriv\w*|due|"
                               r"deliver\w*|bedtime)\b", re.IGNORECASE)
    _RANGE_RE = re.compile(r"(\b\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\b\.?)?)\s*(?:–|-|to|until|till)\s*"
                           r"(\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\b\.?)?)", re.IGNORECASE)
    _REMOVED_RE = re.compile(r"\b(removed|deleted|took\b[^.!?\n]{0,40}\boff\b|(?:is|are|'s)\s+(?:now\s+)?off\s+(?:the|your|"
                             r"today'?s)\s+(?:schedule|plan|list|day))", re.IGNORECASE)

    @staticmethod
    def _clock_key(dt) -> tuple:
        return (dt.hour % 12, dt.minute)

    def _row_entry(self, part: str) -> Optional[str]:
        """What a list line with a time in front lists ("- 1:10 PM – Varnish (until 1:40 PM)": "Varnish (until
        1:40 PM)"); None for any other text."""
        m = self._CLOCK_RE.search(part or "")
        if m is None or part[:m.start()].strip(" \t-*•–—"):
            return None
        span = self._RANGE_RE.match(part, m.start())
        return part[(span or m).end():].lstrip(" \t–—:-")

    def _said_clocks(self, text: str) -> List[tuple]:
        out = []
        for m in self._CLOCK_RE.finditer(text or ""):
            if m.group(6):
                out.append((0, 0))
            elif m.group(1):
                out.append((int(m.group(1)) % 12, int(m.group(2))))
            else:
                out.append((int(m.group(4)) % 12, 0))
        return out

    @staticmethod
    def _opens_a_longer_title(part: str, title: str, every: List[str]) -> bool:
        """The item's whole title is in the sentence only as part of another title on the plan: each time, the
        word after it is that title's next word, or the words before it are all of that title's words before it
        ("Quench the tongs is at 1:10 PM" beside Quench and Quench the tongs names the tongs, never Quench)."""
        words, mine = re.findall(r"[\w'’]+", (part or "").lower()), re.findall(r"[\w'’]+", (title or "").lower())
        n = len(mine)
        longer = [w for w in (re.findall(r"[\w'’]+", t.lower()) for t in every) if len(w) > n]
        found = [i for i in range(len(words) - n + 1) if n and words[i:i + n] == mine]
        return bool(found) and all(
            any(other[k:k + n] == mine and ((k + n < len(other) and words[i + n:i + n + 1] == [other[k + n]])
                                            or (0 < k <= i and words[i - k:i] == other[:k]))
                for other in longer for k in range(len(other) - n + 1))
            for i in found)

    def _reported_times(self, turn_start: Optional[int]) -> Dict[str, set]:
        """The times this turn's calls gave for when an item finished or started (log_outcome's started and
        finished, doing_now's now) and Python kept, as the call's result shows them, by the item's title in lower
        case: what happened, never where it sits. A time Python dropped, or a call refused, gives none."""
        out: Dict[str, set] = {}
        turn = self.messages[turn_start:] if turn_start is not None else []
        results = {m.get("tool_call_id"): str(m.get("content") or "") for m in turn if m.get("role") == "tool"}
        for m in turn:
            for call in (m.get("tool_calls") or []) if m.get("role") == "assistant" else ():
                args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
                result = results.get(call.get("id"), "")
                if not result or result.startswith(NOT_DONE):
                    continue
                if call.get("name") == "log_outcome":
                    times = [c for k in ("started", "finished") for c in self._said_clocks(str(args.get(k) or ""))]
                elif call.get("name") == "doing_now":
                    times = [self._clock_key(self.tools._now())]
                else:
                    continue
                kept = set(self._said_clocks(result))
                out.setdefault(self._item_of(call), set()).update(c for c in times if c in kept)
        return out

    def _wrong_times(self, reply: str, turn_start: Optional[int] = None) -> List[str]:
        """Times the reply gives for a named item that aren't the item's on the plan after the turn ("canoe club is
        set for 2:30" with canoe club at 2:05): "Canoe club is at 2:05 PM" per item. Each time is judged by its role
        (role_times.py): a start and an end said together ("11:15 AM Pottery class (until 12:40 PM)", "11:15 AM –
        12:40 PM Pottery class") must be one item's start and its end, so a right end never covers a wrong start;
        a time said alone passes when it's any time of what it names (start or end, as given or by its block, or
        when it really happened). In a sentence, a title that is only part of another title on the plan isn't
        named ("Quench the tongs is at 1:10 PM" says nothing of Quench), and a time this turn's calls recorded as
        when the item finished or started ("marked Quench done at 9:10 PM") says what happened, never where it sits. One
        about another day, a reminder or a delivery is left alone."""
        from thirties_core.clock import thirty_of
        from thirties_core.done_reference import _listed_alone
        from thirties_core.item_facts import mentions
        from thirties_core.shown_times import shown_end
        plan = getattr(self.tools, "day_plan", None)
        if plan is None:
            return []
        spans = [s for s in plan.spans_in_view() if not s.shown_only]
        titles = {s.id: self.tools._title(s) for s in spans}
        # a time that's part of an item's title ("10:15 Feed the cat") is its name, never a claim about the plan
        timed = sorted((t for t in titles.values() if self._CLOCK_RE.search(t)), key=len, reverse=True)
        every = [self.tools._title(s) for s in plan.spans_in_view()]  # someone else's shown events too
        reported = self._reported_times(turn_start)
        out: List[str] = []
        for part in re.split(r"(?<=[.!?])\s+|\n+|[,;]|\band\b|\bthen\b", reply or ""):
            bare = part
            for t in timed:
                bare = re.sub(re.escape(t), " ", bare, flags=re.IGNORECASE)
            said = self._said_clocks(bare)
            if not said or part.rstrip().endswith("?") or self._ELSEWHERE_RE.search(part):
                continue
            # a list line with a time in front is about the item whose whole title is its entry, never one whose
            # title is only part of it ("1:10 PM Varnish the kite spars" says nothing of Varnish: done_reference.py)
            entry = None if any(re.match(rf"[\s*•-]*{re.escape(t)}", part, re.IGNORECASE) for t in timed) \
                else self._row_entry(part)  # a line opening with a title that holds a time isn't a timed line
            named = [s for s in spans if (_listed_alone(entry, titles[s.id]) if entry is not None
                                          else mentions(part, titles[s.id], titles.values())
                                          and not self._opens_a_longer_title(part, titles[s.id], every))]
            if not named:
                continue
            # --- a time from the item's own title ("Return the key by 4:30 PM") is its name, never its time ---
            own = {c for s in named for c in self._said_clocks(titles[s.id])}
            own |= {c for s in named for c in reported.get(titles[s.id].strip().lower(), ())}  # finished or started
            said = [c for c in said if c not in own]
            if not said:
                continue
            # --- end its own title ---
            known, starts, ends = set(), {}, {}
            for s in named:
                st = {self._clock_key(dt) for dt in (s.start_dt, thirty_of(s.start_dt), s.actual_start, s.planned_start)
                      if dt is not None}
                # an end as the schedule shows it: a calendar's or the user's to the minute, else its block's end
                # (shown_times.py), never the block's end of a time that is shown as it is
                en = {self._clock_key(dt) for dt in (s.end_dt, shown_end(s), s.actual_end, s.planned_end)
                      if dt is not None}
                starts[s.id], ends[s.id] = st, en
                known |= st | en
            # a range ("from 3:00 to 4:00") is a start and an end: its start is checked as one (an end that
            # matches never covers a wrong start)
            ranges = [(self._said_clocks(m.group(1)), self._said_clocks(m.group(2)))
                      for m in self._RANGE_RE.finditer(bare)]
            ranges = [(a[0], b[0]) for a, b in ranges if a and b]
            bad_range = bool(ranges) and not all(any(a in starts[s.id] and b in ends[s.id] for s in named)
                                                 for a, b in ranges)
            # --- a start beside an "(until ...)" end, each checked by its role (role_times.py) ---
            from thirties_core.role_times import start_and_end
            role_starts, role_ends = start_and_end(bare, self._CLOCK_RE, self._RANGE_RE, self._said_clocks)
            bad_roles = bool(role_starts and role_ends) and not any(
                set(role_starts) <= starts[s.id] and set(role_ends) <= ends[s.id] for s in named)
            # --- end by role ---
            if bad_roles or bad_range or not any(c in known for c in said):
                out += [f"{titles[s.id]} is at {format_clock(s.start_dt)}" for s in named[:2]]
        return list(dict.fromkeys(out))

    def _not_removed(self, reply: str) -> List[str]:
        """Items the reply says were taken off that are still planned as they were: "Shower is still on the plan at
        2:00 PM"."""
        from thirties_core.models import SpanStatus
        before, status = getattr(self, "_times_before", None), getattr(self, "_status_before", None)
        if before is None or status is None:
            return []
        out: List[str] = []
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", reply or ""):
            m = self._REMOVED_RE.search(sentence)
            if m is None or sentence.rstrip().endswith("?") or self._ADMITS_RE.search(sentence):
                continue
            lead = sentence[:m.start()]
            if self._NEGATED_RE.search(lead[-40:]) or self._NOT_YET_RE.search(lead):
                continue
            named = [s for s in self._named(sentence) if not s.is_envelope and s.source != "calendar"]
            if named and all(s.status == SpanStatus.PLANNED and status.get(s.id) == SpanStatus.PLANNED
                             and before.get(s.id) == (s.start_dt, s.end_dt) for s in named):
                out += [f"{self.tools._title(s)} is still on the plan at {format_clock(s.start_dt)}" for s in named[:3]]
        # --- said to be off today's list, while it's still on it with no time (off_today.py) ---
        from thirties_core.off_today import LIST_GONE_RE, listed_still
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", reply or ""):
            m = LIST_GONE_RE.search(sentence)
            if m is None or sentence.rstrip().endswith("?") or self._ADMITS_RE.search(sentence):
                continue
            lead = sentence[:m.start()]
            if self._NEGATED_RE.search(lead[-40:]) or self._NOT_YET_RE.search(lead):
                continue
            out += [f"{t} is still on today's list" for t in listed_still(self.tools, sentence)[:3]]
        # --- end off today's list ---
        return list(dict.fromkeys(out))

    def _moves_made(self, turn_start: int, reply: str, retry) -> str:
        """After the brain's turn, before anything is sent: what the reply says happened is checked against what
        the tools did, claim by claim. Moves (the items it names moved; naming none, a move went through), dones (the
        items it names were marked), times it gives for an item (the plan's), and removals (the items are off). A
        reply that disagrees is sent back once with the facts; if it still disagrees, each false sentence is replaced
        with the plan's facts, in place (plain_admit.py): nothing is added under it. The words alone never pass for a change (a turn that logged a done and claimed a move it never
        made; "the dishwasher's going" replied to as done; "set for 3:30" over a plan at 4:00)."""
        # --- how it talks, too (reply_checks.py): the time now is fixed outright; the rest goes into the rewrite ---
        from thirties_core import reply_checks
        try:
            now = self.tools._now()
        except Exception:
            now = None
        reply = reply_checks.fix_now_time(reply, now)

        from thirties_core.time_claims import unmade_time_claims  # "locked in" over a refusal (time_claims.py)

        from thirties_core.unasked import mark as unasked  # a move nobody asked for isn't announced (unasked.py)

        def problems(text: str):
            return self._reply_problems(turn_start, text, now)
        unmade, undone, wrong, kept, talk, promised = problems(reply)
        if not (unmade or undone or wrong or kept or talk or promised):
            return reply
        logger.warning("[FrontierBrain] Reply disagrees with the plan: %r (%s)", reply[:200],
                       unmade + undone + wrong + kept + talk + promised)
        said = []
        if unmade:
            said.append("something was moved, but " + ("nothing on the plan moved this turn" if unmade == ["nothing"]
                                                         else "; ".join(u for u in unmade if u != "nothing")))
        if undone:
            said.append("something is done, but " + "; ".join(undone)
                        + " (started isn't finished: what they're doing now is doing_now)")
        if wrong:
            said.append("times the plan doesn't have: " + "; ".join(wrong))
        if kept:
            said.append("something was taken off, but " + "; ".join(kept))
        if promised:  # what each promise needs (reply_checks.unkept_promises)
            said += reply_checks.promise_checks(promised, self, turn_start)
        from thirties_core.plain_admit import check_text, from_fact  # said in the reply itself (plain_admit.py)
        say = check_text(list(dict.fromkeys(from_fact(f) for f in unmade + undone + wrong + kept)))
        check = ("Your reply says " + ", and ".join(said) + ". Make the change now, or " + say[0].lower() + say[1:]
                 if said else "")
        if talk:
            check += (" " if check else "") + "Rewrite your reply: " + "; ".join(talk) + "."
        self._check(check)
        fixed = reply_checks.fix_now_time((retry() or "").strip(), now)
        if fixed and not any(problems(fixed)):
            return fixed
        if promised:  # a promise Python can't keep goes
            reply = reply_checks.drop_sentences(reply, promised)
        if talk:
            reply = reply_checks.say_picked(self, reply_checks.morning_fill(self, turn_start,
                                                                            reply_checks.last_resort(reply)))
        if not (unmade or undone or wrong or kept):
            return reply
        # --- the false sentence says what's true, in place; nothing is added under the reply (plain_admit.py) ---
        from thirties_core.plain_admit import say_in_place
        from thirties_core.time_claims import TIME_CLAIM_RE

        def facts_of(sentence: str) -> List[str]:
            return (unasked(self, turn_start, self._unmade_moves(turn_start, sentence)
                            + unmade_time_claims(self, turn_start, sentence))
                    + self._unmade_dones(sentence) + self._wrong_times(sentence, turn_start)
                    + self._not_removed(sentence))
        claim = re.compile("|".join(f"(?:{r.pattern})" for r in (self._MOVED_RE, TIME_CLAIM_RE, self._REMOVED_RE,
                                                                  self._OUTCOME_CLAIM_RE, self._CLAIMS_RE)),
                           re.IGNORECASE)
        plan = getattr(self.tools, "day_plan", None)
        out, _ = say_in_place(reply, facts_of, claim, [self.tools._title(s) for s in plan.spans] if plan else [])
        return out
        # --- end in place ---

    def _reply_problems(self, turn_start: int, text: str, now) -> tuple:
        """What a reply says that disagrees with the turn (_moves_made): (unmade moves, undone dones, wrong times,
        items not taken off, how it talks, unkept promises)."""
        from thirties_core import reply_checks
        from thirties_core.time_claims import unmade_time_claims
        from thirties_core.unasked import mark as unasked
        return (unasked(self, turn_start, self._unmade_moves(turn_start, text)
                        + unmade_time_claims(self, turn_start, text)),
                self._unmade_dones(text), self._wrong_times(text, turn_start),
                self._not_removed(text), reply_checks.problems(self, turn_start, text, now),
                reply_checks.unkept_promises(self, turn_start, text))

    def _no_unseen_correction(self, turn_start: int, reply: str, retry) -> str:
        """A reply that corrects a message the user never saw (a draft sent back this turn: "I need to correct my
        last message", "my first attempt didn't take") gets one fresh rewrite; if it still opens so, the opening
        is cut (reply_checks.unseen_corrections)."""
        from thirties_core import reply_checks
        if not reply_checks.unseen_corrections(self, turn_start, reply):
            return reply
        logger.warning("[FrontierBrain] Reply corrects a draft the user never saw: %r", reply[:200])
        self._check("Your reply corrects a message the user never saw. Write it fresh: what's true now, as if for "
                    "the first and only time.")
        fresh = (retry() or "").strip()
        if fresh and not reply_checks.unseen_corrections(self, turn_start, fresh):
            return fresh
        stripped = reply_checks.strip_corrections(fresh or reply)
        from thirties_core.clean_prose import introduced  # a cut that reads broken: the model's rewrite as written
        return fresh if fresh and introduced(stripped, [fresh]) else stripped

    # --- never only "checking…", and clean prose after Python's edits (holding_lines.py, clean_prose.py) ---

    def _not_holding(self, turn_start: int, reply: str, retry) -> str:
        """A reply that only says it's looking something up goes back once: look it up now and answer from it, in
        this turn. Its answer is checked like any reply (_moves_made). If it still only announces, the user hears
        plainly that it couldn't be looked up; a holding line beside a real answer goes, whole."""
        from thirties_core import holding_lines
        if not holding_lines.holds(reply):
            return reply
        if not holding_lines.only_holds(reply):
            return holding_lines.without(reply) or reply
        logger.warning("[FrontierBrain] Reply only announces a lookup; asking for the answer: %r", reply[:200])
        self._check(holding_lines.CHECK)
        fresh = (retry() or "").strip()
        if fresh and not holding_lines.only_holds(fresh):
            fresh = self._moves_made(turn_start, fresh, retry)
            if not holding_lines.only_holds(fresh):
                return holding_lines.without(fresh) or fresh
        return holding_lines.COULDNT

    # --- a note said to be kept for the engineers only when one was (engineer_notes.py) ---

    def _kept_for_engineers(self, reply: str, retry) -> str:
        """A reply that says a note for the engineers is kept, when none was, goes back once: the model keeps it
        (note_for_engineers) or drops the claim. Whatever claim is still untrue after that goes, whole."""
        from thirties_core import engineer_notes
        if not engineer_notes.false_kept_claim(self.tools, reply):
            return reply
        logger.warning("[FrontierBrain] Reply says a note for the engineers is kept; none was: %r", reply[:200])
        self._check(engineer_notes.CHECK)
        fresh = (retry() or "").strip() or reply  # a call alone, with no new words: the draft may now be true
        return engineer_notes.without_false_kept(self.tools, fresh)

    # --- end note said to be kept ---

    def _written(self, turn_start: int) -> List[str]:
        """Everything the model wrote this turn, drafts included."""
        return [str(m.get("content") or "") for m in self.messages[turn_start:]
                if m.get("role") == "assistant" and str(m.get("content") or "").strip()]

    def _tidy(self, turn_start: int, reply: str, retry, changes_before: int) -> str:
        """The last check before a reply goes: if Python's edits left it reading broken (a sentence starting in
        lower case, a stranded colon, a list without its lead-in) where the model's own words weren't, the model
        rewrites it whole. A rewrite that still disagrees with the turn, or only announces a lookup, isn't used:
        the broken sentences go instead, whole."""
        from thirties_core import clean_prose
        from thirties_core.holding_lines import only_holds
        written = self._written(turn_start)
        broken = clean_prose.introduced(reply, written)
        if not broken:
            return reply
        logger.warning("[FrontierBrain] An edit left the reply broken (%s): %r", broken, reply[:300])
        self._check(clean_prose.rewrite_check(broken) + " It reads now: " + reply)
        fresh = (retry() or "").strip()
        try:
            now = self.tools._now()
        except Exception:
            now = None
        if fresh and not only_holds(fresh) and not clean_prose.introduced(fresh, written) \
                and not any(self._reply_problems(turn_start, fresh, now)):
            return fresh
        out = clean_prose.cut_broken(reply, broken)
        if out.strip() and not clean_prose.introduced(out, written):
            return out
        return "Done." if self.tools.successful_changes > changes_before else self.UNCHANGED_LINE

    # --- end never only "checking…", clean prose ---

    UNCHANGED_LINE = "I wasn't able to change your schedule just now. Could you tell me again what you'd like moved or added?"

    def _sky(self, now) -> str:
        """The moon and tomorrow's sun, computed (sky.py): a fact to say, never a guess."""
        try:
            from thirties_core.sky import sky_line
            gen = self.tools.scheduler.config.general if getattr(self.tools, "scheduler", None) else None
            return sky_line(now, getattr(gen, "latitude", None), getattr(gen, "longitude", None))
        except Exception:
            return ""

    def _say_true(self, changes_before: int) -> str:
        """A reply claiming a change that never happened gets one fresh try in the model's own words (fresh text
        over fixed lines); the fixed line only if that fails too."""
        self._check("Your reply says the plan changed, but nothing changed this turn. Make the change now, or write "
                    "your reply again so it's true: what you'd do next, or the one fact you still lack.")
        fresh = (self._rounds(2, changes_before, self.tools.outcomes_logged) if self._bookkeeping_on()
                 else self._one_more_round()).strip()
        if fresh and (self.tools.successful_changes > changes_before or not self._claims_change(fresh)):
            return fresh
        return self.UNCHANGED_LINE

    def _one_more_round(self) -> str:
        """The model's retry (the baseline turn's), its calls run: another round when one only made calls, looked
        something up, or was refused, so the reply that answers the results is the one kept. Words written next to
        a lookup are a draft ("Checking why…"): the answer comes after the result, in the same turn."""
        content = ""
        for _ in range(3):
            response = self.inference_engine.chat(messages=self._for_model(), tools=TOOL_SPECS)
            self.messages.append(response)
            calls = response.get("tool_calls", []) or []
            results = []
            for call in calls:
                with model_call():  # the model's own call (change_scope.py)
                    output = self.tools.execute(call.get("name"), call.get("arguments", {}))
                results.append(str(output))
                self.messages.append({"role": "tool", "tool_call_id": call.get("id", "call"),
                                      "name": call.get("name"), "content": output})
            said = str(response.get("content") or "")
            if said.strip() or not calls:
                content = said
            if not calls:
                break
            refused = any(r.startswith(NOT_DONE) for r in results)
            if not (said.strip() and not refused and not any(c.get("name") in READ_ONLY_TOOLS for c in calls)):
                if refused:
                    content = ""  # written before the refusal: not what's true now
                continue
            break
        return content

    _META_RE = re.compile(
        r"(^|\n)\s*(wait[,!.]|notice that|let'?s check|let me check|looking at the (prompt|transcript|history)|"
        r"the previous (turn|assistant)|the assistant'?s? (previous|reply|turn)|in the (prompt|transcript|history)\b)|"
        r"\b(tool results?|tool calls?|(?-i:CALL [a-z]+_[a-z_]+)|system instructions|rule \d+\b|push_?later|"
        r"replace_?overlaps|"
        r"task_id|\bid=[0-9a-f]{6,})|\(Result of [a-z_]+|for the assistant only", re.IGNORECASE)

    def _is_meta(self, text: str) -> bool:
        """The model talking about the transcript, its rules or its tools instead of to the user."""
        return bool(self._META_RE.search(text or ""))
