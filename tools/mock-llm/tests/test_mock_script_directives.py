# SPDX-License-Identifier: Apache-2.0
"""Unit tests of tools/mock-llm/mock_script.py: one per script directive and directive source."""

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mock_script as ms

TOOLS = ["terminal", "kanban_show", "send_message", "phone_notify", "web_search"]


def user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def system(text: str) -> dict[str, Any]:
    return {"role": "system", "content": text}


def tool_result(text: str) -> dict[str, Any]:
    return {"role": "tool", "content": text}


def called(name: str, args: dict[str, Any]) -> dict[str, Any]:
    call = {"id": "c", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    return {"role": "assistant", "content": None, "tool_calls": [call]}


def answer(messages: list[dict[str, Any]], tools: list[str] = TOOLS) -> ms.Plan:
    return ms.plan_reply(ms.analyse(messages), tools)


def delay(messages: list[dict[str, Any]], tools: list[str] = TOOLS) -> int | None:
    return ms.delay_seconds(ms.analyse(messages), tools)


def test_call_directive_in_last_user_message_emits_that_tool_call() -> None:
    plan = answer([user('go @@CALL terminal {"command": "ls"}@@')])
    assert plan == ms.Plan(("terminal", {"command": "ls"}), "")


def test_call_directives_run_in_order_one_per_model_call() -> None:
    first = user('@@CALL terminal {"command": "a"}@@ @@CALL send_message {"text": "b"}@@')
    turn = [first, called("terminal", {"command": "a"}), tool_result("ok")]
    assert answer(turn).tool_call == ("send_message", {"text": "b"})
    turn += [called("send_message", {"text": "b"}), tool_result("sent")]
    assert answer(turn) == ms.Plan(None, "MOCK[?] done; last tool result: sent")


def test_call_directive_for_a_tool_not_offered_answers_with_text() -> None:
    plan = answer([user("@@CALL delegate_task {}@@")], ["terminal", "clarify"])
    assert plan == ms.Plan(None, "MOCK[?] tool delegate_task not offered (have 2 tools)")


def test_call_directive_in_system_prompt_applies_on_the_first_user_turn_only() -> None:
    sys_msg = system('@@CALL terminal {"command": "x"}@@')
    assert answer([sys_msg, user("hi")]).tool_call == ("terminal", {"command": "x"})
    later = [sys_msg, user("hi"), {"role": "assistant", "content": "hello"}, user("again")]
    assert answer(later) == ms.Plan(None, "MOCK[?] done; you said: again")


def test_call_directive_inside_a_json_string_argument_is_not_run_by_the_outer_turn() -> None:
    body = '@@CALL kanban_create {"body": "@@CALL terminal {\\"command\\": \\"inner\\"}@@"}@@'
    directives = ms.parse_directives(body)
    assert directives == [("kanban_create", {"body": '@@CALL terminal {"command": "inner"}@@'})]


def test_call_directive_with_bad_or_non_object_arguments_gets_empty_arguments() -> None:
    text = "@@CALL terminal {not json@@ @@CALL clarify [1, 2]@@ @@CALL @@"
    assert ms.parse_directives(text) == [("terminal", {}), ("clarify", {})]


def test_call_directive_carried_in_a_tool_result_runs_once() -> None:
    task = json.dumps({"task": {"body": 'do it @@CALL terminal {"command": "w"}@@'}})
    turn = [user("work kanban task t_1"), called("kanban_show", {"task_id": "t_1"}), tool_result(task)]
    assert answer(turn).tool_call == ("terminal", {"command": "w"})
    turn += [called("terminal", {"command": "w"}), tool_result("done")]
    assert answer(turn).tool_call is None


def test_call_directive_in_a_non_json_tool_result_is_found_in_the_raw_text() -> None:
    turn = [user("child"), called("web_search", {}), tool_result('plain @@CALL terminal {"command": "r"}@@')]
    assert answer(turn).tool_call == ("terminal", {"command": "r"})


def test_kanban_worker_prompt_bootstraps_kanban_show_once() -> None:
    turn = [user("work kanban task t_9abc")]
    assert answer(turn).tool_call == ("kanban_show", {"task_id": "t_9abc"})
    turn += [called("kanban_show", {"task_id": "t_9abc"}), tool_result('{"task": "nothing scripted"}')]
    assert answer(turn).tool_call is None


def test_profile_marker_in_the_system_prompt_names_the_answering_profile() -> None:
    plan = answer([system("You are x. PROFILE-MARKER=work_2"), user("hello\nthere")])
    assert plan.reply == "MOCK[work_2] done; you said: hello there"


def test_delay_directive_sleeps_n_seconds_capped_at_120() -> None:
    assert delay([user("slow <<DELAY:7>>")]) == 7
    assert delay([user("slow <<DELAY:999>>")]) == 120
    assert delay([user("no delay")]) is None


def test_delay_directive_is_ignored_without_tools_with_a_call_or_after_a_tool_result() -> None:
    assert delay([user("slow <<DELAY:7>>")], []) is None
    assert delay([user("<<DELAY:7>> @@CALL terminal {}@@")]) is None
    assert delay([system("@@CALL terminal {}@@"), user("<<DELAY:7>>")]) is None
    assert delay([user("<<DELAY:7>>"), called("web_search", {}), tool_result("r")]) is None


def test_long_directive_appends_the_long_answer() -> None:
    reply = answer([user("tell me <<LONG>>")]).reply
    assert reply.startswith("MOCK[?] done; you said: tell me <<LONG>> | LONG ANSWER: Line 1 of")
    assert reply.endswith("Line 8 of a deliberately long mock answer that does not fit on the glasses.")
    assert len(reply) > 600


def test_request_without_tools_gets_the_auxiliary_title_reply() -> None:
    assert answer([user('@@CALL terminal {"command": "ls"}@@')], []) == ms.Plan(None, "Mock title")


def test_list_content_parts_are_joined_as_text() -> None:
    parts = [{"type": "text", "text": "@@CALL terminal"}, "skip", {"type": "text", "text": '{"command": "p"}@@'}]
    assert answer([{"role": "user", "content": parts}]).tool_call == ("terminal", {"command": "p"})


def test_kanban_worker_bootstrap_reads_only_the_first_user_message() -> None:
    later = [user("hello"), {"role": "assistant", "content": "hi"}, user("work kanban task t_42")]
    assert answer(later) == ms.Plan(None, "MOCK[?] done; you said: work kanban task t_42")
    first = [user("work kanban task t_42"), {"role": "assistant", "content": "x"}, user("go on")]
    assert answer(first).tool_call == ("kanban_show", {"task_id": "t_42"})


def test_call_directives_count_only_tool_calling_assistant_turns_after_the_last_user_message() -> None:
    script = user('@@CALL terminal {"command": "a"}@@ @@CALL send_message {"text": "b"}@@')
    earlier = [user("old"), called("terminal", {"command": "a"}), tool_result("x")]
    chatter = {"role": "assistant", "content": "thinking"}
    assert answer([*earlier, script, chatter]).tool_call == ("terminal", {"command": "a"})


def test_carried_directive_already_run_with_reordered_arguments_is_not_repeated() -> None:
    task = json.dumps({"body": '@@CALL terminal {"b": 2, "a": 1}@@'})
    turn = [user("child"), called("web_search", {}), tool_result(task), called("terminal", {"a": 1, "b": 2})]
    assert answer([*turn, tool_result("ok")]).tool_call is None


def test_delay_long_and_tools_directives_outside_the_last_user_message_are_ignored() -> None:
    a = ms.analyse([system("<<DELAY:9>> <<LONG>> <<TOOLS>>"), user("old <<DELAY:9>>"), user("now")])
    assert ms.delay_seconds(a, TOOLS) is None
    assert "LONG ANSWER" not in ms.plan_reply(a, TOOLS).reply


def test_a_directive_for_a_tool_not_offered_does_not_advance_to_the_next_one() -> None:
    turn = [user("@@CALL delegate_task {}@@ @@CALL terminal {}@@")]
    assert answer(turn, ["terminal"]) == ms.Plan(None, "MOCK[?] tool delegate_task not offered (have 1 tools)")
