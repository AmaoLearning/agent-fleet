from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "dsh_sampling_plugin.mjs"


class DshSamplingPluginTests(unittest.TestCase):
    def test_sets_temperature_and_top_p(self) -> None:
        script = r"""
const plugin = await import(process.argv[1])
const listeners = new Map()
let extension
const ctx = {
  on(name, listener) { listeners.set(name, listener) },
  deepseekLlmApiExtensions: {
    register(name, provider) { extension = { name, provider } },
  },
}
plugin.apply(ctx)
const config = await listeners.get('agent/request')({}, async () => ({
  provider: 'deepseek-official', model: 'deepseek-v4', maxTokens: 65536,
}))
const prepared = extension.provider.prepare()
process.stdout.write(JSON.stringify({ config, field: extension.name, value: prepared.value }))
"""
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", script, PLUGIN.as_uri()],
            check=True,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "DSH_TEMPERATURE": "1.0",
                "DSH_TOP_P": "0.95",
            },
        )
        result = json.loads(completed.stdout)
        self.assertEqual(
            result,
            {
                "config": {
                    "provider": "deepseek-official",
                    "model": "deepseek-v4",
                    "maxTokens": 65536,
                    "temperature": 1,
                },
                "field": "top_p",
                "value": 0.95,
            },
        )


if __name__ == "__main__":
    unittest.main()
