"""Offline model isolation at role-worker boundaries; no live profiles or sessions."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from misaka.config import home


class MoADefaultModelTests(unittest.TestCase):
    def test_default_slots_decode_canonical_references_without_a_registry(self):
        from misaka.core.moa import provider as moa

        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"MISAKA_HOME": directory}), \
                patch("misaka.ai.models.get_providers", return_value=["builtin-provider", "global-provider"]), \
                patch("misaka.core.model_registry.ModelRegistry", side_effect=AssertionError("recursive registry")):
            home.path("models").parent.mkdir(parents=True, exist_ok=True)
            home.path("models").write_text(json.dumps({"providers": {"custom-provider": {}}}), encoding="utf-8")
            for reference, expected in (
                ("builtin-provider/role-model", {"provider": "builtin-provider", "model": "role-model"}),
                ("custom-provider/family/role-model", {"provider": "custom-provider", "model": "family/role-model"}),
                ("legacy-role-model", {"provider": "global-provider", "model": "legacy-role-model"}),
                ("family/raw-model", {"provider": "global-provider", "model": "family/raw-model"}),
                ("inherit", {"provider": "global-provider", "model": "global-model"}),
            ):
                cfg = {"provider": "global-provider", "default_model": "global-model", "lo_model": reference}
                with self.subTest(reference=reference), patch.object(moa, "current_config", return_value=cfg):
                    refs, aggregator = moa._default_slots()
                    self.assertEqual(refs, [expected, {"provider": "global-provider", "model": "global-model"}])
                    self.assertEqual(aggregator, expected)

    def test_global_pair_keeps_raw_slash_id_even_when_prefix_is_a_provider(self):
        from misaka.core.moa import provider as moa

        cfg = {"provider": "global-provider", "default_model": "builtin-provider/raw-model",
               "lo_model": "builtin-provider/raw-model"}
        with patch.object(moa, "current_config", return_value=cfg), \
                patch("misaka.ai.models.get_providers", return_value=["builtin-provider"]):
            refs, aggregator = moa._default_slots()
        expected = {"provider": "global-provider", "model": "builtin-provider/raw-model"}
        self.assertEqual(refs, [expected, expected])
        self.assertEqual(aggregator, expected)

    def test_explicit_moa_slots_remain_untouched(self):
        from misaka.core.moa import provider as moa

        cfg = {"provider": "global-provider", "default_model": "global-model", "lo_model": "global-model"}
        raw = {"reference_models": [{"provider": "advisor", "model": "raw/model"}],
               "aggregator": {"provider": "aggregator", "model": "other/raw-model"}}
        original = json.dumps(raw)
        with patch.object(moa, "current_config", return_value=cfg):
            result = moa._normalize_preset(raw)
        self.assertEqual(result["reference_models"], [{**raw["reference_models"][0], "enabled": True}])
        self.assertEqual(result["aggregator"], raw["aggregator"])
        self.assertEqual(json.dumps(raw), original)


if __name__ == "__main__":
    unittest.main()
