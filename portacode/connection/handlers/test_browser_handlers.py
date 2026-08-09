import unittest
from unittest.mock import AsyncMock, patch

from . import browser_handlers
from .browser_handlers import BrowserRunHandler


class BrowserRunHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.handler = BrowserRunHandler(None, {})

    async def test_rejects_unsafe_profile_identifier_before_starting_browser(self):
        with self.assertRaisesRegex(ValueError, "session_id"):
            await self.handler.execute({"session_id": "../../other-user", "steps": []})

    async def test_rejects_more_than_thirty_steps(self):
        with self.assertRaisesRegex(ValueError, "at most 30"):
            await self.handler.execute({"session_id": "safe", "steps": [{"op": "wait"}] * 31})

    async def test_rejects_more_than_four_explicit_screenshots(self):
        with self.assertRaisesRegex(ValueError, "four explicit screenshots"):
            await self.handler.execute({"session_id": "safe", "steps": [{"op": "screenshot"}] * 5})

    async def test_reuses_worker_for_same_runtime_user_and_session(self):
        worker = AsyncMock()
        worker.run.return_value = {"ok": True, "steps": [], "screenshots": []}
        message = {"session_id": "safe", "steps": [], "final_screenshot": False}
        with (
            patch.object(browser_handlers, "get_default_runtime_user", return_value="tester"),
            patch.object(browser_handlers, "get_runtime_user_home", return_value="/tmp"),
            patch.object(browser_handlers, "mkdir_with_owner"),
            patch.object(browser_handlers, "wrap_argv_for_user", return_value=["node"]),
            patch.object(browser_handlers, "_node_path", AsyncMock(return_value="/modules")),
            patch.object(browser_handlers, "_worker_for", AsyncMock(return_value=worker)) as get_worker,
        ):
            first = await self.handler.execute(message)
            second = await self.handler.execute(message)
        self.assertTrue(first["success"])
        self.assertTrue(second["success"])
        self.assertEqual(get_worker.await_count, 2)
        keys = [(call.kwargs["user"], call.kwargs["session_id"]) for call in get_worker.await_args_list]
        self.assertEqual(keys, [("tester", "safe"), ("tester", "safe")])
        self.assertEqual(worker.run.await_count, 2)

    async def test_worker_registry_separates_users_and_sessions(self):
        browser_handlers._WORKERS.clear()
        first = await browser_handlers._worker_for(
            user="one", session_id="shared", argv=["node"], env={}
        )
        same = await browser_handlers._worker_for(
            user="one", session_id="shared", argv=["node"], env={}
        )
        other_user = await browser_handlers._worker_for(
            user="two", session_id="shared", argv=["node"], env={}
        )
        other_session = await browser_handlers._worker_for(
            user="one", session_id="other", argv=["node"], env={}
        )
        self.assertIs(first, same)
        self.assertIsNot(first, other_user)
        self.assertIsNot(first, other_session)
        browser_handlers._WORKERS.clear()
