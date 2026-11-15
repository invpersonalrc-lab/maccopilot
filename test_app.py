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


if __name__ == "__main__":
    unittest.main()
