import unittest

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
