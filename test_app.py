import unittest
from unittest.mock import patch

import app as copilot


class MacCopilotSafetyTests(unittest.TestCase):
    def setUp(self):
        copilot.pending.clear()
        copilot._audit_log.clear()
        copilot._action_times.clear()
        self.client = copilot.app.test_client()
        self.size = patch.object(copilot.pyautogui, "size", return_value=(1440, 900))
        self.size.start()
        self.addCleanup(self.size.stop)
        self.click = patch.object(copilot.pyautogui, "click").start()
        self.write = patch.object(copilot.pyautogui, "write").start()
        self.hotkey = patch.object(copilot.pyautogui, "hotkey").start()
        self.press = patch.object(copilot.pyautogui, "press").start()
        self.addCleanup(patch.stopall)
        copilot._ask_times.clear()
        copilot._agent.update(running=False, state="idle", log=[], question="", action="", steps=0)
        copilot._agent_stop.clear()
        copilot._agent_answer.clear()
        patch.object(copilot, "AGENT_STEP_DELAY_SECONDS", 0).start()
        self.screenshot = patch.object(copilot, "_screenshot_for_model", return_value=("aW1n", 1440, 900)).start()

    def ask(self, suggestion, question="How do I open a new tab?"):
        with patch.object(copilot, "_call_claude", return_value=suggestion) as claude:
            response = self.client.post("/ask", json={"question": question})
        copilot._ask_times.clear()  # keep the per-test pacing limit out of the way
        return response, claude

    def propose_click(self):
        return self.client.post("/propose", json={"kind": "click", "x": 10, "y": 20, "value": ""})

    def test_proposal_never_executes_without_explicit_approval(self):
        response = self.propose_click()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["permission_level"], "GREEN")
        self.click.assert_not_called()
        self.write.assert_not_called()

    def test_explicit_approval_executes_exactly_one_mocked_action_once(self):
        token = self.propose_click().json["token"]
        response = self.client.post("/approve", json={"token": token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.click.call_args_list, [((10, 20),)])
        self.assertEqual(self.client.post("/approve", json={"token": token}).status_code, 400)
        self.click.assert_called_once_with(10, 20)

    def test_unapproved_and_unknown_action_denied(self):
        self.assertEqual(self.client.post("/approve", json={"token": "forged"}).status_code, 400)
        response = self.client.post("/propose", json={"kind": "delete", "value": "file"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("denied", response.json["error"])
        self.click.assert_not_called()
        self.write.assert_not_called()

    def test_sensitive_or_multiline_typing_denied(self):
        for value in ("my password is abc", "api_key=abc", "delete all", "hello\nworld"):
            with self.subTest(value=value):
                self.assertEqual(self.client.post("/propose", json={"kind": "type", "value": value}).status_code, 400)
        self.write.assert_not_called()

    def test_only_one_pending_proposal_and_reject_cancels(self):
        first = self.propose_click()
        self.assertEqual(self.propose_click().status_code, 429)
        self.assertEqual(self.client.post("/reject").status_code, 200)
        self.assertEqual(self.client.post("/approve", json={"token": first.json["token"]}).status_code, 400)
        self.click.assert_not_called()

    def test_action_limit_enforced_before_execution(self):
        copilot._action_times.extend([__import__("time").monotonic()] * copilot.MAX_ACTIONS_PER_SESSION)
        token = self.propose_click().json["token"]
        response = self.client.post("/approve", json={"token": token})
        self.assertEqual(response.status_code, 429)
        self.click.assert_not_called()

    def test_localhost_binding_and_failsafe_preserved(self):
        with open(copilot.__file__, encoding="utf-8") as source_file:
            source = source_file.read()
        self.assertIn("app.run(host='127.0.0.1'", source)
        self.assertTrue(copilot.pyautogui.FAILSAFE)

    def test_page_uses_explicit_dom_references_and_button_starts_disabled(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("document.getElementById('approve')", page)
        self.assertIn("id=\"approve\" disabled", page)
        script = page.split("<script>", 1)[1].split("</script>", 1)[0]
        self.assertIn("els.approve.disabled=true; token=null;", script)
        self.assertIn("if(!r.ok || j.error)", script)
        self.assertIn("token=j.token;", script)
        self.assertIn("els.approve.disabled=false;", script)
        self.assertLess(script.index("if(!r.ok || j.error)"), script.index("els.approve.disabled=false;"))
        self.assertIn("if(!token)return;", script)
        self.assertIn("body:JSON.stringify({token:approvedToken})", script)
        self.assertNotRegex(script, r"(?<![.\w])kind\.value")

    def test_type_and_hotkey_click_target_first_when_given(self):
        token = self.client.post("/propose", json={"kind": "type", "x": 50, "y": 60, "value": "VLC"}).json
        self.assertEqual(token["description"], "Click (50, 60), then type: 'VLC'")
        self.write.assert_not_called()
        self.client.post("/approve", json={"token": token["token"]})
        self.click.assert_called_once_with(50, 60)
        self.write.assert_called_once()
        self.click.reset_mock()
        token = self.client.post("/propose", json={"kind": "hotkey", "x": 0, "y": 0, "value": "enter"}).json["token"]
        self.client.post("/approve", json={"token": token})
        self.click.assert_not_called()
        self.press.assert_called_once_with("enter")

    def test_focus_click_outside_display_denied(self):
        response = self.client.post("/propose", json={"kind": "type", "x": 9999, "y": 60, "value": "VLC"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(copilot.pending, {})

    # ---- Ask (Claude-suggested actions) ----

    def test_ask_suggestion_never_executes_without_approval(self):
        response, claude = self.ask({"explanation": "Press Command-T.", "kind": "hotkey", "x": 0, "y": 0, "value": "command+t"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["description"], "Press: command+t")
        self.assertIn("token", response.json)
        claude.assert_called_once()
        self.hotkey.assert_not_called()
        self.click.assert_not_called()
        self.write.assert_not_called()

    def test_ask_suggestion_executes_once_only_after_approval(self):
        token = self.ask({"explanation": "Click the button.", "kind": "click", "x": 100, "y": 200, "value": ""})[0].json["token"]
        self.click.assert_not_called()
        self.assertEqual(self.client.post("/approve", json={"token": token}).status_code, 200)
        self.click.assert_called_once_with(100, 200)
        self.assertEqual(self.client.post("/approve", json={"token": token}).status_code, 400)

    def test_ask_unsafe_suggestions_are_blocked_by_same_rules(self):
        unsafe = [
            {"explanation": "", "kind": "hotkey", "x": 0, "y": 0, "value": "command+q"},
            {"explanation": "", "kind": "type", "x": 0, "y": 0, "value": "sudo rm -rf /"},
            {"explanation": "", "kind": "type", "x": 0, "y": 0, "value": "my password is hunter2"},
            {"explanation": "", "kind": "click", "x": 5000, "y": 5000, "value": ""},
            {"explanation": "", "kind": "delete", "x": 0, "y": 0, "value": "file"},
        ]
        for suggestion in unsafe:
            with self.subTest(suggestion=suggestion):
                response = self.ask(suggestion)[0]
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("token", response.json)
                self.assertIn("blocked", response.json["note"])
        self.assertEqual(copilot.pending, {})
        self.click.assert_not_called()
        self.write.assert_not_called()
        self.hotkey.assert_not_called()

    def test_ask_none_gives_explanation_without_pending_action(self):
        response = self.ask({"explanation": "Type your password yourself.", "kind": "none", "x": 0, "y": 0, "value": ""})[0]
        self.assertEqual(response.json["explanation"], "Type your password yourself.")
        self.assertNotIn("token", response.json)
        self.assertEqual(copilot.pending, {})

    def test_ask_refused_while_proposal_pending_and_input_validated(self):
        self.propose_click()
        with patch.object(copilot, "_call_claude") as claude:
            self.assertEqual(self.client.post("/ask", json={"question": "hi"}).status_code, 429)
            self.assertEqual(self.client.post("/ask", json={"question": ""}).status_code, 400)
            self.assertEqual(self.client.post("/ask", json={"question": "x" * 501}).status_code, 400)
        claude.assert_not_called()

    def test_ask_is_rate_limited(self):
        with patch.object(copilot, "_call_claude", return_value={"explanation": "", "kind": "none", "x": 0, "y": 0, "value": ""}) as claude:
            self.assertEqual(self.client.post("/ask", json={"question": "a"}).status_code, 200)
            self.assertEqual(self.client.post("/ask", json={"question": "b"}).status_code, 429)
            copilot._ask_times.clear()
            copilot._ask_times.extend([__import__("time").monotonic() - 60] * copilot.MAX_ASKS_PER_HOUR)
            self.assertEqual(self.client.post("/ask", json={"question": "c"}).status_code, 429)
        self.assertEqual(claude.call_count, 1)

    def test_ask_errors_are_reported_without_executing(self):
        with patch.object(copilot, "_call_claude", side_effect=RuntimeError("No API key")):
            response = self.client.post("/ask", json={"question": "help"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("No API key", response.json["error"])
        self.assertEqual(copilot.pending, {})

    def test_missing_api_key_gives_clear_error(self):
        with patch.object(copilot, "_read_env_file", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "ANTHROPIC_API_KEY"):
                copilot._call_claude("q", "aW1n", 1440, 900)

    def test_question_text_is_not_written_to_audit_log(self):
        self.ask({"explanation": "", "kind": "none", "x": 0, "y": 0, "value": ""}, question="my secret plan")
        self.assertNotIn("my secret plan", repr(list(copilot._audit_log)))

    def test_claude_request_shape(self):
        fake_response = type("R", (), {"stop_reason": "end_turn", "content": [
            type("B", (), {"type": "text", "text": '{"explanation":"ok","kind":"none","x":0,"y":0,"value":""}'})()]})()
        with patch.object(copilot, "_read_env_file", return_value={"ANTHROPIC_API_KEY": "test-key"}), \
                patch("anthropic.Anthropic") as client_cls:
            client_cls.return_value.beta.messages.create.return_value = fake_response
            result = copilot._call_claude("open a tab", "aW1n", 1440, 900)
        self.assertEqual(result["kind"], "none")
        self.assertEqual(client_cls.call_args.kwargs["base_url"], "https://api.anthropic.com")
        kwargs = client_cls.return_value.beta.messages.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "claude-opus-5-5")
        self.assertEqual(kwargs["output_config"]["format"]["schema"], copilot.ASK_SCHEMA)
        self.assertIn("1440x900", kwargs["system"])
        image, text = kwargs["messages"][0]["content"]
        self.assertEqual(image["source"]["media_type"], "image/png")
        self.assertEqual(text["text"], "open a tab")


    # ---- Agent mode ----

    def step(self, status, kind="none", x=0, y=0, value="", question="", explanation="e"):
        return {"status": status, "explanation": explanation, "question": question, "kind": kind, "x": x, "y": y, "value": value}

    def run_agent(self, decisions, goal="Install VLC"):
        with patch.object(copilot, "_call_claude_agent", side_effect=decisions) as claude:
            copilot._agent["running"] = True
            copilot._agent_loop(goal)
        return claude

    def test_agent_runs_safe_steps_without_approval_until_done(self):
        claude = self.run_agent([
            self.step("act", "click", 100, 200),
            self.step("act", "type", 300, 40, "videolan.org"),
            self.step("act", "hotkey", 0, 0, "enter"),
            self.step("done", explanation="VLC installed"),
        ])
        self.assertEqual(claude.call_count, 4)
        self.assertEqual(self.click.call_args_list, [((100, 200),), ((300, 40),)])
        self.write.assert_called_once()
        self.press.assert_called_once_with("enter")
        self.assertFalse(copilot._agent["running"])
        self.assertIn("Done: VLC installed", copilot._agent["log"][-1])
        history = claude.call_args_list[-1].args[1]
        self.assertTrue(any("videolan.org" in h for h in history))

    def test_agent_never_bypasses_safety_rules(self):
        self.run_agent([
            self.step("act", "type", 0, 0, "sudo rm -rf /"),
            self.step("act", "type", 0, 0, "my password is hunter2"),
            self.step("act", "hotkey", 0, 0, "command+q"),
            self.step("act", "click", 9999, 9999),
            self.step("stuck", explanation="no safe way"),
        ])
        self.click.assert_not_called()
        self.write.assert_not_called()
        self.hotkey.assert_not_called()
        self.assertTrue(any("Blocked" in line for line in copilot._agent["log"]))

    def test_agent_pauses_for_person_and_stops_if_declined(self):
        import threading
        decisions = [self.step("ask_user", "click", 500, 500, question="This costs $4.99. Buy it?")]
        with patch.object(copilot, "_call_claude_agent", side_effect=decisions):
            copilot._agent["running"] = True
            worker = threading.Thread(target=copilot._agent_loop, args=("buy app",))
            worker.start()
            for _ in range(100):
                if copilot._agent["state"] == "waiting":
                    break
                __import__("time").sleep(0.02)
            self.assertEqual(copilot._agent["question"], "This costs $4.99. Buy it?")
            self.click.assert_not_called()
            self.assertEqual(self.client.post("/agent/answer", json={"approve": False}).status_code, 200)
            worker.join(5)
        self.click.assert_not_called()
        self.assertFalse(copilot._agent["running"])

    def test_agent_runs_paused_step_only_after_yes(self):
        import threading
        decisions = [self.step("ask_user", "click", 500, 500, question="Allow?"), self.step("done")]
        with patch.object(copilot, "_call_claude_agent", side_effect=decisions):
            copilot._agent["running"] = True
            worker = threading.Thread(target=copilot._agent_loop, args=("g",))
            worker.start()
            for _ in range(100):
                if copilot._agent["state"] == "waiting":
                    break
                __import__("time").sleep(0.02)
            self.click.assert_not_called()
            self.client.post("/agent/answer", json={"approve": True})
            worker.join(5)
        self.click.assert_called_once_with(500, 500)

    def test_agent_stop_and_step_limit(self):
        copilot._agent_stop.set()
        claude = self.run_agent([self.step("act", "click", 1, 1)])
        claude.assert_not_called()
        self.assertIn("Stopped by you", copilot._agent["log"][-1])
        copilot._agent_stop.clear()
        claude = self.run_agent([self.step("act", "click", 10, 10)] * copilot.AGENT_MAX_STEPS)
        self.assertEqual(claude.call_count, copilot.AGENT_MAX_STEPS)
        self.assertIn("step limit", copilot._agent["log"][-1])

    def test_agent_emergency_stop_ends_run(self):
        self.click.side_effect = copilot.pyautogui.FailSafeException("corner")
        self.run_agent([self.step("act", "click", 10, 10), self.step("act", "click", 20, 20)])
        self.assertIn("Emergency stop", copilot._agent["log"][-1])
        self.assertEqual(self.click.call_count, 1)

    def test_agent_respects_hourly_action_limit(self):
        copilot._action_times.extend([__import__("time").monotonic()] * copilot.MAX_ACTIONS_PER_SESSION)
        self.run_agent([self.step("act", "click", 10, 10)])
        self.click.assert_not_called()
        self.assertIn("limit", copilot._agent["log"][-1])

    def test_agent_endpoints_and_manual_actions_blocked_while_running(self):
        self.assertEqual(self.client.post("/agent/start", json={"goal": ""}).status_code, 400)
        copilot._agent["running"] = True
        self.assertEqual(self.client.post("/agent/start", json={"goal": "x"}).status_code, 409)
        self.assertEqual(self.propose_click().status_code, 409)
        self.assertEqual(self.client.post("/agent/answer", json={"approve": True}).status_code, 400)
        self.assertEqual(self.client.get("/agent/status").json["running"], True)
        self.client.post("/agent/stop")
        self.assertTrue(copilot._agent_stop.is_set())

    def test_reject_button_also_stops_agent(self):
        self.client.post("/reject")
        self.assertTrue(copilot._agent_stop.is_set())


if __name__ == "__main__":
    unittest.main()
