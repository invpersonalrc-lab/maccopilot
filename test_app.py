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


if __name__ == "__main__":
    unittest.main()
