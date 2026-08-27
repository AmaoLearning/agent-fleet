from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

from dsh_harbor import AgentFleetDsh  # noqa: E402


class Context:
    n_input_tokens: int | None = None
    n_output_tokens: int | None = None
    n_cache_tokens: int | None = None


class AgentFleetDshTests(unittest.TestCase):
    def make_agent(self, root: Path, **kwargs: object) -> AgentFleetDsh:
        return AgentFleetDsh(
            logs_dir=root,
            version="0.1.1-rc.2",
            model_name="deepseek/private/deepseek-v4-flash-0731",
            extra_env={
                "DSH_API_KEY": "fake-key",
                "DSH_BASE_URL": "https://gateway.example.test/v1",
            },
            **kwargs,
        )

    def test_patch_preserves_native_route_and_evaluation_controls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            agent = self.make_agent(Path(temporary_name))
            patch = yaml.safe_load(agent.build_eval_patch())

        by_id = {entry.get("id"): entry for entry in patch if "id" in entry}
        self.assertEqual(
            by_id["agent-default-model"]["config"],
            {
                "provider": "deepseek-official",
                "model": "private/deepseek-v4-flash-0731",
            },
        )
        route = by_id["llm-deepseek"]["config"]
        self.assertEqual(route["baseURL"], "https://gateway.example.test/v1")
        self.assertEqual(route["reasoningEffort"], "max")
        self.assertEqual(route["thinking"], "enabled")
        self.assertEqual(route["retryPolicy"]["maxRetries"], 0)
        self.assertNotIn("fake-key", agent.build_eval_patch())

        persistence = by_id["session-persistence-jsonl"]["config"]
        self.assertEqual(persistence["compression"], "none")
        self.assertFalse(persistence["packChunks"])

        inserted = next(entry["insert"] for entry in patch if "insert" in entry)
        self.assertEqual(inserted[0]["name"], "./sampling-plugin.mjs")
        self.assertEqual(inserted[0]["config"]["temperature"], 1.0)
        self.assertEqual(
            agent._SAMPLING_PLUGIN_PATH,
            "/installed-agent/dsh-home/profiles/headless/sampling-plugin.mjs",
        )

    def test_rejects_unsupported_top_p_instead_of_ignoring_it(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary_name,
            self.assertRaisesRegex(ValueError, "does not expose top_p"),
        ):
            self.make_agent(Path(temporary_name), top_p="0.95")

    def test_rejects_non_native_model_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            agent = AgentFleetDsh(
                logs_dir=Path(temporary_name),
                model_name="openai/model",
                extra_env={"DSH_BASE_URL": "https://gateway.example.test/v1"},
            )
            with self.assertRaisesRegex(ValueError, "provider prefix"):
                agent.build_eval_patch()

    def test_generic_route_uses_pi_ai_openai_completions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            agent = AgentFleetDsh(
                logs_dir=Path(temporary_name),
                model_name="harbor/private/deepseek-v4-flash-0731",
                extra_env={
                    "DSH_API_KEY": "fake-key",
                    "DSH_BASE_URL": "https://gateway.example.test/v1",
                },
                provider_route="harbor",
                thinking_format="deepseek",
            )
            patch = yaml.safe_load(agent.build_eval_patch())
            settings = yaml.safe_load(agent.build_settings() or "")

        by_id = {entry.get("id"): entry for entry in patch if "id" in entry}
        self.assertEqual(
            by_id["agent-default-model"]["config"],
            {
                "provider": "harbor",
                "model": "private/deepseek-v4-flash-0731",
            },
        )
        self.assertNotIn("llm-deepseek", by_id)
        route = settings["llm-pi-ai"]["providers"]["harbor"]
        self.assertEqual(route["api"], "openai-completions")
        self.assertEqual(route["baseURL"], "https://gateway.example.test/v1")
        self.assertEqual(route["timeoutMs"], 300000)
        self.assertEqual(route["streamIdleTimeoutMs"], 300000)
        self.assertEqual(route["retryPolicy"], {"mode": "normal", "maxRetries": 0})
        self.assertEqual(
            route["models"],
            [
                {
                    "id": "private/deepseek-v4-flash-0731",
                    "name": "private/deepseek-v4-flash-0731",
                    "contextWindow": 1000000,
                    "maxTokens": 256000,
                }
            ],
        )
        self.assertEqual(route["compat"], {"thinkingFormat": "deepseek"})

    def test_populates_token_context_from_session_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            session_dir = root / "dsh-sessions" / "workspace" / "session"
            session_dir.mkdir(parents=True)
            events = [
                {
                    "type": "assistant/message",
                    "data": {
                        "usage": {
                            "inputTokens": 100,
                            "outputTokens": 30,
                            "cacheReadTokens": 20,
                            "cacheWriteTokens": 5,
                        }
                    },
                },
                {"type": "turn/end", "data": {}},
            ]
            (session_dir / "session.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events),
                encoding="utf-8",
            )
            agent = self.make_agent(root)
            context = Context()
            agent.populate_context_post_run(context)  # type: ignore[arg-type]

        self.assertEqual(context.n_input_tokens, 125)
        self.assertEqual(context.n_output_tokens, 30)
        self.assertEqual(context.n_cache_tokens, 20)


if __name__ == "__main__":
    unittest.main()
