import json
import unittest
from unittest.mock import patch

from xray import XRayConfig


class XRayConfigCompatibilityTest(unittest.TestCase):
    def test_accepts_legacy_config_with_missing_sections(self):
        config = XRayConfig("{}", "203.0.113.10")

        self.assertEqual(config["inbounds"][0]["tag"], "API_INBOUND")
        self.assertEqual(config["routing"]["rules"][0]["outboundTag"], "API")
        self.assertEqual(config["api"]["tag"], "API")
        self.assertEqual(config["stats"], {})

    def test_accepts_legacy_config_with_null_lists(self):
        config = XRayConfig(
            json.dumps(
                {
                    "inbounds": None,
                    "routing": {"domainStrategy": "IPIfNonMatch", "rules": None},
                }
            ),
            "203.0.113.10",
        )

        self.assertEqual(len(config["inbounds"]), 1)
        self.assertEqual(config["routing"]["domainStrategy"], "IPIfNonMatch")
        self.assertEqual(len(config["routing"]["rules"]), 1)

    def test_replaces_only_node_owned_api_entries(self):
        legacy = {
            "api": {"tag": "OLD_API"},
            "inbounds": [
                {"tag": "API_INBOUND", "protocol": "dokodemo-door"},
                {"tag": "VLESS TCP REALITY", "protocol": "vless"},
            ],
            "routing": {
                "domainStrategy": "IPIfNonMatch",
                "rules": [
                    {
                        "inboundTag": ["API_INBOUND"],
                        "outboundTag": "OLD_API",
                        "type": "field",
                    },
                    {
                        "inboundTag": ["public"],
                        "outboundTag": "OLD_API",
                        "type": "field",
                    },
                ],
            },
            "observatory": {"subjectSelector": ["proxy"]},
        }

        config = XRayConfig(json.dumps(legacy), "203.0.113.10")

        self.assertEqual(config["inbounds"][0]["tag"], "API_INBOUND")
        self.assertEqual(config["inbounds"][1]["tag"], "VLESS TCP REALITY")
        self.assertEqual(config["routing"]["rules"][0]["outboundTag"], "API")
        self.assertEqual(config["routing"]["rules"][1]["inboundTag"], ["public"])
        self.assertEqual(config["observatory"], {"subjectSelector": ["proxy"]})

    def test_keeps_selected_legacy_inbounds_filter(self):
        legacy = {
            "inbounds": [
                {"tag": "keep-me", "protocol": "vless"},
                {"tag": "drop-me", "protocol": "vmess"},
            ]
        }

        with patch("xray.INBOUNDS", ["keep-me"]):
            config = XRayConfig(json.dumps(legacy), "203.0.113.10")

        self.assertEqual(
            [inbound["tag"] for inbound in config["inbounds"]],
            ["API_INBOUND", "keep-me"],
        )


if __name__ == "__main__":
    unittest.main()
