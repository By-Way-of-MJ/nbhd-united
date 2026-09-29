"""Local-test Ollama seam for chat_completion: inert unless the local_test settings define it."""

from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.common import openrouter

OK = {"choices": [{"message": {"content": "hello"}}]}


def _resp(data):
    r = mock.Mock()
    r.json.return_value = data
    r.raise_for_status.return_value = None
    return r


class LocalLLMSeamTests(SimpleTestCase):
    @override_settings(OPENROUTER_API_KEY="sk-test")
    def test_unset_keeps_openrouter(self):
        self.assertIsNone(openrouter.local_llm())
        with (
            mock.patch.object(openrouter.requests, "post", return_value=_resp(OK)) as post,
            mock.patch.object(openrouter, "_record_success"),
        ):
            data, used = openrouter.chat_completion("openrouter/a/b", [{"role": "user", "content": "x"}])
        self.assertEqual(post.call_args.args[0], openrouter.OPENROUTER_CHAT_URL)
        self.assertEqual(post.call_args.kwargs["json"]["model"], "a/b")
        self.assertEqual(used, "openrouter/a/b")

    @override_settings(
        OPENROUTER_API_KEY="", LOCAL_TEST_LLM_URL="http://127.0.0.1:11434/v1", LOCAL_TEST_LLM_MODEL="qwen-local"
    )
    def test_local_routes_to_loopback_without_a_key(self):
        with (
            mock.patch.object(openrouter.requests, "post", return_value=_resp(OK)) as post,
            mock.patch.object(openrouter, "_record_success") as healthy,
        ):
            data, used = openrouter.chat_completion(
                ["openrouter/a/b", "openrouter/c/d"],
                [{"role": "user", "content": "x"}],
                response_format={"type": "json_object"},
            )
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:11434/v1/chat/completions")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["model"], "qwen-local")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertNotIn("Authorization", post.call_args.kwargs["headers"])
        self.assertEqual(used, "openrouter/a/b")
        healthy.assert_not_called()

    @override_settings(
        LOCAL_TEST_LLM_URL="http://127.0.0.1:11434/v1", LOCAL_TEST_LLM_MODEL="m", LOCAL_TEST_LLM_TIMEOUT=600
    )
    def test_local_timeout_floor(self):
        with mock.patch.object(openrouter.requests, "post", return_value=_resp(OK)) as post:
            openrouter.chat_completion("openrouter/a/b", [{"role": "user", "content": "x"}], timeout=45)
        self.assertEqual(post.call_args.kwargs["timeout"], 600)

    def test_rejects_anything_but_plain_loopback(self):
        for url in (
            "https://127.0.0.1:11434/v1",
            "http://10.0.0.2:11434/v1",
            "http://127.0.0.1/v1",
            "http://user:pw@127.0.0.1:11434/v1",
            "http://127.0.0.1.evil.test:11434/v1",
        ):
            with (
                self.subTest(url=url),
                override_settings(LOCAL_TEST_LLM_URL=url, LOCAL_TEST_LLM_MODEL="m"),
                self.assertRaises(RuntimeError),
            ):
                openrouter.local_llm()
        with (
            override_settings(LOCAL_TEST_LLM_URL="http://127.0.0.1:11434/v1", LOCAL_TEST_LLM_MODEL=""),
            self.assertRaises(RuntimeError),
        ):
            openrouter.local_llm()

    @override_settings(OPENROUTER_API_KEY="")
    def test_no_key_and_no_local_still_raises(self):
        with self.assertRaises(RuntimeError):
            openrouter.chat_completion("openrouter/a/b", [{"role": "user", "content": "x"}])
