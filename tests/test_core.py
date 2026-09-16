from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_bot.bot import truncate
from agent_bot.claude_runner import (
    UserSession,
    build_command,
    get_user_session,
    list_user_sessions,
    parse_claude_output,
    run_claude,
    switch_user_session,
)
from agent_bot.config import _parse_user_ids


class CoreTests(unittest.TestCase):
    def test_parse_user_ids(self) -> None:
        self.assertEqual(_parse_user_ids("123, 456,123"), frozenset({123, 456}))

    def test_empty_user_ids_enable_pairing_mode(self) -> None:
        self.assertEqual(_parse_user_ids(""), frozenset())

    def test_truncate(self) -> None:
        result = truncate("x" * 5000)
        self.assertLessEqual(len(result), 4000)
        self.assertIn("rút gọn", result)

    def test_parse_claude_json(self) -> None:
        self.assertEqual(parse_claude_output('{"result":"xin chào"}'), "xin chào")

    def test_new_command_uses_safe_arguments(self) -> None:
        prompt = 'hello; rm -rf /; echo "still data"'
        session = UserSession("general", Path("/tmp/session-test"), "test-id", True)
        command = build_command("claude", prompt, session)
        self.assertEqual(command[2], prompt)
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertIn("--restricted", command)
        self.assertIn("acceptEdits", command)
        self.assertIn("sonnet", command)
        self.assertIn("medium", command)
        self.assertIn("--session-id", command)

    def test_existing_command_resumes_session(self) -> None:
        session = UserSession("general", Path("/tmp/session-test"), "test-id", False)
        command = build_command("claude", "continue", session)
        self.assertIn("--resume", command)
        self.assertNotIn("--session-id", command)

    def test_user_gets_persistent_session_workspace(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = get_user_session(root, 123)
            second = get_user_session(root, 123)
            other = get_user_session(root, 456)
            self.assertEqual(first.workspace, second.workspace)
            self.assertEqual(first.session_id, second.session_id)
            self.assertNotEqual(first.workspace, other.workspace)

    def test_named_sessions_can_be_listed_and_switched(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            get_user_session(root, 123, force_new=True, session_name="daily")
            get_user_session(root, 123, force_new=True, session_name="coding")
            current, names = list_user_sessions(root, 123)
            self.assertEqual(current, "coding")
            self.assertEqual(names, ["coding", "daily"])
            selected = switch_user_session(root, 123, "daily")
            self.assertEqual(selected.name, "daily")

    @patch("agent_bot.claude_runner.subprocess.run")
    def test_run_claude_uses_no_shell(self, mock_run) -> None:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = '{"result":"ok"}'
        mock_run.return_value.stderr = ""
        session = UserSession("general", Path("/tmp/session-test"), "test-id", True)
        with patch("agent_bot.claude_runner.get_user_session", return_value=session):
            result, returned_session = run_claude(
                "claude", Path("/tmp"), 123, "test"
            )
        self.assertEqual(result, "ok")
        self.assertEqual(returned_session, session)
        self.assertIs(mock_run.call_args.kwargs["shell"], False)
        self.assertEqual(mock_run.call_args.kwargs["cwd"], session.workspace)


if __name__ == "__main__":
    unittest.main()
