from __future__ import annotations

import unittest

from research_platform.__main__ import build_parser


class USPITAssembleCLIContractTests(unittest.TestCase):
    def test_assemble_reviewed_requires_explicit_inputs_and_window(self) -> None:
        args = build_parser().parse_args(
            [
                "us-pit",
                "assemble-reviewed",
                "--normalization-dir",
                "normalized",
                "--review-dir",
                "review",
                "--output-dir",
                "workspaces",
                "--start",
                "2021-08-31",
                "--end",
                "2026-07-31",
                "--source-batch",
                "a" * 64,
                "--source-batch",
                "b" * 64,
            ]
        )

        self.assertEqual(args.us_pit_command, "assemble-reviewed")
        self.assertEqual(args.start, "2021-08-31")
        self.assertEqual(args.end, "2026-07-31")
        self.assertEqual(args.source_batch, ["a" * 64, "b" * 64])


if __name__ == "__main__":
    unittest.main()
