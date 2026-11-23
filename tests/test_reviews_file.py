import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from prwarden import cli, config, doctor
from prwarden.config import ConfigError
from prwarden.engine import CHECKS_LEVEL, CODE_LEVEL, DESIGN_LEVEL, DOCS_LEVEL
from prwarden.paths import PACKAGE_ROOT, Locations
from prwarden.review_plan import review_plan
from prwarden.reviews_file import (
    EngineChoice,
    apply_overrides,
    load_reviews_file,
    parse_reviews,
    render_reviews,
    reviews_payload,
    settings_from_reviews,
)
from tests.test_config import write_private_env
from tests.test_review_plan import settings as env_settings

EXAMPLE = PACKAGE_ROOT / "reviews.example.json"
SOURCE = Path("/config/reviews.json")


def example() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def identity(items) -> list:
    return [
        (item.review_engine, item.review_model, item.review_effort) for item in items
    ]


def described(plan) -> list:
    """Everything a plan's types review with, as plain values."""

    return [
        (
            review_type.name,
            identity((review_type.settings, *review_type.fallbacks)),
            identity(review_type.verifiers),
            review_type.settings.review_timeout_seconds,
            review_type.settings.review_verify_timeout_seconds,
            review_type.settings.review_verify_timeout_per_candidate_seconds,
            review_type.settings.max_findings
            if review_type.name == CODE_LEVEL
            else review_type.settings.max_design_findings
            if review_type.name == DESIGN_LEVEL
            else review_type.settings.max_docs_findings
            if review_type.name == DOCS_LEVEL
            else None,
        )
        for review_type in plan.types
    ]


class ParseTests(unittest.TestCase):
    def test_the_example_is_a_file_the_loader_accepts(self) -> None:
        """Tracked, so it must stay one a run would read."""

        reviews = load_reviews_file(EXAMPLE)

        self.assertEqual(
            [review_type.name for review_type in reviews.types],
            [DESIGN_LEVEL, DOCS_LEVEL, CODE_LEVEL, CHECKS_LEVEL],
        )
        code = reviews.get(CODE_LEVEL)
        self.assertEqual(
            code.review.engines,
            (
                EngineChoice("kiro", "claude-sonnet-5.5", "high"),
                EngineChoice("claude", "claude-opus-5-5", "xhigh"),
            ),
        )
        self.assertEqual(code.verify.timeout_per_candidate_seconds, 180)
        self.assertEqual(code.max_findings, 10)
        self.assertIsNone(reviews.get(CHECKS_LEVEL).verify)
        self.assertEqual(reviews.env_for("kiro"), ())

    def test_no_file_is_no_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(load_reviews_file(Path(directory) / "reviews.json"))

    def test_only_code_is_required_and_the_rest_has_defaults(self) -> None:
        reviews = parse_reviews(
            {"types": {"code": {"review": {"engines": [{"engine": "codex"}]}}}},
            SOURCE,
        )

        (code,) = reviews.types
        self.assertEqual(code.review.engines, (EngineChoice("codex"),))
        self.assertEqual(code.review.timeout_seconds, 300)
        self.assertIsNone(code.verify)
        self.assertEqual(code.max_findings, 10)
        self.assertEqual((reviews.concurrency, reviews.max_review_bundles), (2, 4))
        self.assertIn("package-lock.json", reviews.ignored_missing_patch_patterns)

    def test_a_disabled_type_is_left_out_and_the_order_may_change(self) -> None:
        payload = example()
        payload["types"]["docs"]["enabled"] = False
        payload["order"] = ["design", "code"]
        reviews = parse_reviews(payload, SOURCE)
        self.assertEqual(
            [review_type.name for review_type in reviews.types],
            [DESIGN_LEVEL, CODE_LEVEL, CHECKS_LEVEL],
        )

        payload = example()
        payload["order"] = ["docs", "design", "code"]
        reviews = parse_reviews(payload, SOURCE)
        self.assertEqual(
            [review_type.name for review_type in reviews.types][:3],
            [DOCS_LEVEL, DESIGN_LEVEL, CODE_LEVEL],
        )

    def test_a_file_that_says_something_wrong_is_refused_by_name(self) -> None:
        def changed(change) -> dict:
            payload = example()
            change(payload)
            return payload

        code = lambda payload: payload["types"]["code"]  # noqa: E731
        cases = {
            "unknown key 'reveiw'": changed(
                lambda p: code(p).update({"reveiw": code(p).pop("review")})
            ),
            "types.code is required": changed(lambda p: p["types"].pop("code")),
            "types.code.enabled cannot be false": changed(
                lambda p: code(p).update({"enabled": False})
            ),
            "order must end with code": changed(
                lambda p: p.update({"order": ["code", "design", "docs"]})
            ),
            "order must list each enabled type": changed(
                lambda p: p.update({"order": ["design", "code"]})
            ),
            "types.code.review.engines must list one to 4 engines": changed(
                lambda p: code(p)["review"].update(
                    {
                        "engines": [
                            {"engine": "kiro", "model": f"m{n}"} for n in range(5)
                        ]
                    }
                )
            ),
            "types.code.review.engines[1] repeats an engine and model": changed(
                lambda p: code(p)["review"]["engines"].append(
                    {"engine": "kiro", "model": "claude-sonnet-5.5"}
                )
                or code(p)["review"]["engines"].pop(1)
            ),
            "types.code.review.engines[0].effort must be one of": changed(
                lambda p: code(p)["review"]["engines"][0].update({"effort": "fast"})
            ),
            "types.code.review.engines[0].engine must be one of": changed(
                lambda p: code(p)["review"]["engines"][0].update({"engine": "cursor"})
            ),
            "engines.command.command is required": changed(
                lambda p: code(p)["review"]["engines"].__setitem__(
                    0, {"engine": "command"}
                )
            ),
            "types.code.review.engines[1] names the command engine": changed(
                lambda p: code(p)["review"]["engines"].insert(1, {"engine": "command"})
            ),
            "types.code.verify.engines[0] names the command engine": changed(
                lambda p: code(p)["verify"]["engines"].insert(0, {"engine": "command"})
            ),
            "types.code.review.engines[0].effort cannot be set for the command": (
                changed(
                    lambda p: code(p)["review"]["engines"][0].update(
                        {"engine": "command", "effort": "high"}
                    )
                )
            ),
            "engines.kiro.command must be a list of arguments": changed(
                lambda p: p["engines"]["kiro"].update({"command": "kiro-cli"})
            ),
            "types.checks.review.engines must list one engine": changed(
                lambda p: p["types"]["checks"]["review"]["engines"].append(
                    {"engine": "claude"}
                )
            ),
            "types.checks has an unknown key 'verify'": changed(
                lambda p: p["types"]["checks"].update(
                    {"verify": code(p)["verify"]}
                )
            ),
            "types.code.max_findings must be between 1 and 10": changed(
                lambda p: code(p).update({"max_findings": 11})
            ),
            "types.code.review.timeout_seconds must be between 30 and 1800": changed(
                lambda p: code(p)["review"].update({"timeout_seconds": 3600})
            ),
            "engines.kiro.env names '1BAD'": changed(
                lambda p: p["engines"]["kiro"].update({"env": ["1BAD"]})
            ),
            "types is required": {"concurrency": 2},
        }
        for message, payload in cases.items():
            with self.subTest(message=message):
                with self.assertRaises(ConfigError) as raised:
                    parse_reviews(payload, SOURCE)
                self.assertIn(message, str(raised.exception))
                self.assertTrue(str(raised.exception).startswith(f"{SOURCE}: "))

    def test_invalid_json_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reviews.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "is not valid JSON"):
                load_reviews_file(path)


class PlanTests(unittest.TestCase):
    def with_file(self, payload: dict, directory: str):
        reviews = parse_reviews(payload, SOURCE)
        return settings_from_reviews(env_settings(directory), reviews)

    def test_each_type_runs_on_the_engines_its_stages_name(self) -> None:
        payload = example()
        payload["types"]["design"]["review"]["engines"] = [
            {"engine": "codex", "model": "gpt-6.1-terra", "effort": "high"}
        ]
        payload["types"]["design"].pop("verify")
        payload["types"]["code"]["review"]["engines"].append(
            {"engine": "antigravity", "model": "gemini-3.1-pro-high"}
        )
        payload["engines"] = {"codex": {"env": ["CODEX_HOME"]}}
        with tempfile.TemporaryDirectory() as directory:
            settings = self.with_file(payload, directory)
            plan = review_plan(settings)

        design, docs, code, checks = plan.types
        self.assertEqual(
            identity((design.settings, *design.fallbacks)),
            [("codex", "gpt-6.1-terra", "high")],
        )
        self.assertEqual(design.verifiers, ())
        self.assertEqual(design.settings.review_engine_env_allowlist, ("CODEX_HOME",))
        self.assertEqual(code.settings.review_engine_env_allowlist, ())
        self.assertEqual(
            identity(code.fallbacks),
            [
                ("claude", "claude-opus-5-5", "xhigh"),
                ("antigravity", "gemini-3.1-pro-high", None),
            ],
        )
        self.assertEqual(
            identity(code.verifiers),
            [("kiro", "gpt-5.6-luna", "max"), ("codex", "gpt-6.1-terra", "xhigh")],
        )
        # The role fields name the type's own engines, for every report of them.
        self.assertEqual(code.settings.fallback_engine, "claude")
        self.assertEqual(code.settings.verify_model, "gpt-5.6-luna")
        self.assertEqual(code.settings.verify_fallback_engine, "codex")
        self.assertIsNone(code.settings.review_code_effort)
        self.assertEqual(code.settings.review_timeout_seconds, 1800)
        self.assertEqual(
            identity((checks.settings,)), [("kiro", "claude-haiku-5.5", "medium")]
        )
        self.assertIsNone(checks.settings.verify_engine)
        self.assertEqual(checks.fallbacks + checks.verifiers, ())
        # The run's own settings are the code review's and the file's.
        self.assertEqual(settings.review_engine, "kiro")
        self.assertEqual(settings.max_diff_bytes, 200000)
        self.assertTrue(settings.checks_level)
        self.assertEqual(docs.settings.max_docs_findings, 3)

    def test_the_run_names_every_types_engines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = review_plan(self.with_file(example(), directory))
            lines = cli._describe_plan(plan)

        self.assertEqual(
            lines[2],
            "Code review: Claude Sonnet 5.5 @ Kiro at high effort, falling back to "
            "Claude Opus 5.5 @ Claude at xhigh effort; verified by GPT-5.6 Luna @ "
            "Kiro at max effort, falling back to GPT-6.1 Terra @ Codex at xhigh "
            "effort.",
        )
        self.assertTrue(lines[0].startswith("Solution-design review: "))
        self.assertEqual(
            lines[3], "Checks reading: Claude Haiku 5.5 @ Kiro at medium effort."
        )

    def test_the_run_warns_when_one_provider_carries_two_roles(self) -> None:
        def notes(verify):
            payload = example()
            payload["types"]["code"]["verify"]["engines"] = verify
            with tempfile.TemporaryDirectory() as directory:
                plan = review_plan(self.with_file(payload, directory))
                return [
                    line
                    for line in cli._describe_plan(plan)
                    if line.startswith("Note: ")
                ]

        self.assertEqual(notes(example()["types"]["code"]["verify"]["engines"]), [])
        self.assertEqual(
            notes([{"engine": "claude", "model": "claude-sonnet-5-5"}]),
            [
                "Note: claude serves both fallback and verification, so one "
                "provider limit stops both."
            ],
        )
        self.assertEqual(
            notes([{"engine": "kiro", "model": "gpt-5.6-luna"}]),
            [
                "Note: kiro serves both review and verification, so while it is "
                "down the fallback's findings cannot be verified or delivered."
            ],
        )

    def test_the_file_env_writes_runs_as_env_did(self) -> None:
        """The file `reviews_payload` writes from `.env` settings plans the same
        reviews, so moving to the file changes nothing a run does."""

        with tempfile.TemporaryDirectory() as directory:
            for overrides in (
                {},
                {"review_code_effort": None, "design_level": False},
                {"verify_engine": None, "verify_fallback_engine": None},
                {"fallback_engine": None, "checks_level": True},
                {"review_engine_env_allowlist": ("ANTHROPIC_API_KEY",)},
            ):
                with self.subTest(overrides=overrides):
                    base = env_settings(directory, **overrides)
                    payload = reviews_payload(base)
                    moved = settings_from_reviews(
                        base, parse_reviews(json.loads(json.dumps(payload)), SOURCE)
                    )
                    self.assertEqual(
                        described(review_plan(moved)), described(review_plan(base))
                    )
                    for review_type in review_plan(moved).types:
                        self.assertEqual(
                            review_type.settings.review_engine_env_allowlist,
                            base.review_engine_env_allowlist,
                        )

    def test_the_written_file_reads_back_as_the_payload(self) -> None:
        text = render_reviews(example())

        self.assertEqual(json.loads(text), example())
        self.assertIn('"order": ["design", "docs", "code"]', text)
        self.assertTrue(all(len(line) <= 88 for line in text.splitlines()))

    def test_the_reviewers_own_command_moves_into_the_file(self) -> None:
        """A built-in engine run from its own path, or a custom wrapper, once could
        not move, or moved onto the engine's default executable unnoticed."""

        with tempfile.TemporaryDirectory() as directory:
            for overrides in (
                {"review_engine_command": ("/opt/tools/kiro-cli",)},
                {
                    "review_engine": "command",
                    "review_engine_command": ("/opt/tools/review", "--json"),
                    "review_model": None,
                    "review_effort": None,
                    "review_code_effort": None,
                    "checks_effort": None,
                    "fallback_engine": None,
                },
            ):
                with self.subTest(overrides=overrides):
                    base = env_settings(directory, **overrides)
                    payload = json.loads(json.dumps(reviews_payload(base)))
                    engine = base.review_engine
                    self.assertEqual(
                        payload["engines"][engine]["command"],
                        list(base.review_engine_command),
                    )
                    moved = settings_from_reviews(
                        base, parse_reviews(payload, SOURCE)
                    )
                    code = review_plan(moved).get(CODE_LEVEL)
                    self.assertEqual(
                        code.settings.review_engine_command,
                        base.review_engine_command,
                    )
                    self.assertEqual(code.settings.review_engine, engine)

    def test_an_engine_the_file_runs_from_its_own_path_runs_from_there(self) -> None:
        payload = example()
        payload["engines"]["kiro"]["command"] = ["/opt/tools/kiro-cli", "--quiet"]
        with tempfile.TemporaryDirectory() as directory:
            plan = review_plan(self.with_file(payload, directory))

        code = plan.get(CODE_LEVEL)
        self.assertEqual(
            code.settings.review_engine_command, ("/opt/tools/kiro-cli", "--quiet")
        )
        self.assertEqual(
            code.verifiers[0].review_engine_command,
            ("/opt/tools/kiro-cli", "--quiet"),
        )
        self.assertNotEqual(code.fallbacks[0].review_engine_command[0], "/opt/tools")

    def test_types_configured_alike_still_share_engines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.with_file(example(), directory)
            with patch(
                "prwarden.cli.create_review_engine",
                side_effect=lambda made: Mock(name=made.review_engine),
            ):
                stages = cli._create_review_stages(review_plan(settings))

        self.assertIs(stages[DESIGN_LEVEL].engine, stages[DOCS_LEVEL].engine)
        self.assertIs(stages[DESIGN_LEVEL].fallback, stages[CODE_LEVEL].fallback)
        self.assertIs(
            stages[DESIGN_LEVEL].verification, stages[CODE_LEVEL].verification
        )
        self.assertIsNone(stages[CHECKS_LEVEL].fallback)


class OverrideTests(unittest.TestCase):
    def reviews(self):
        return parse_reviews(example(), SOURCE)

    def firsts(self, reviews, stage="review"):
        return {
            review_type.name: getattr(review_type, stage).engines
            for review_type in reviews.types
            if getattr(review_type, stage, None) is not None
        }

    def test_another_engine_takes_no_model_or_effort_written_for_the_first(
        self,
    ) -> None:
        changed = apply_overrides(self.reviews(), engine="codex", model="gpt-6.1")

        for name, engines in self.firsts(changed).items():
            with self.subTest(type=name):
                self.assertEqual(engines[0], EngineChoice("codex", "gpt-6.1"))
        self.assertEqual(
            self.firsts(changed)[CODE_LEVEL][1],
            EngineChoice("claude", "claude-opus-5-5", "xhigh"),
        )

    def test_a_model_alone_keeps_the_engine_its_effort_and_the_checks_model(
        self,
    ) -> None:
        changed = apply_overrides(self.reviews(), model="claude-opus-5.5")

        firsts = self.firsts(changed)
        self.assertEqual(
            firsts[CODE_LEVEL][0], EngineChoice("kiro", "claude-opus-5.5", "high")
        )
        self.assertEqual(
            firsts[CHECKS_LEVEL][0], EngineChoice("kiro", "claude-haiku-5.5", "medium")
        )

    def test_no_fallback_leaves_each_review_its_first_engine(self) -> None:
        changed = apply_overrides(self.reviews(), no_fallback=True)

        for engines in self.firsts(changed).values():
            self.assertEqual(len(engines), 1)
        self.assertEqual(len(self.firsts(changed, "verify")[CODE_LEVEL]), 2)

    def test_a_fallback_engine_replaces_the_second(self) -> None:
        changed = apply_overrides(
            self.reviews(), fallback_engine="antigravity", fallback_model="gemini"
        )

        self.assertEqual(
            self.firsts(changed)[CODE_LEVEL][1], EngineChoice("antigravity", "gemini")
        )

    def test_a_verifier_is_given_to_a_type_that_had_none(self) -> None:
        payload = example()
        payload["types"]["design"].pop("verify")
        changed = apply_overrides(
            parse_reviews(payload, SOURCE), verify_engine="claude", verify_model="x"
        )

        self.assertEqual(
            changed.get(DESIGN_LEVEL).verify.engines, (EngineChoice("claude", "x"),)
        )
        self.assertEqual(
            changed.get(CODE_LEVEL).verify.engines[0], EngineChoice("claude", "x")
        )

    def test_a_model_for_a_role_the_file_does_not_name_is_refused(self) -> None:
        payload = example()
        payload["types"]["design"]["review"]["engines"] = payload["types"]["design"][
            "review"
        ]["engines"][:1]
        with self.assertRaisesRegex(ConfigError, "--fallback-model needs"):
            apply_overrides(parse_reviews(payload, SOURCE), fallback_model="m")
        payload["types"]["design"].pop("verify")
        with self.assertRaisesRegex(ConfigError, "--verify-model needs"):
            apply_overrides(parse_reviews(payload, SOURCE), verify_model="m")

    def test_a_custom_engine_on_the_command_line_is_refused(self) -> None:
        reviews = parse_reviews(example(), SOURCE)
        for flag in ("engine", "verify_engine", "fallback_engine"):
            with self.subTest(flag=flag):
                with self.assertRaisesRegex(ConfigError, "built-in engines"):
                    apply_overrides(reviews, **{flag: "command"})


class LoaderTests(unittest.TestCase):
    """With a reviews file, `.env`'s engine keys are not read at all."""

    def load(self, env_lines, payload, **overrides):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            reviews = Path(directory) / "custom-reviews.json"
            reviews.write_text(json.dumps(payload), encoding="utf-8")
            write_private_env(
                env_file,
                "\n".join(
                    (
                        "GITHUB_REVIEWER_USERNAME=reviewer",
                        f"REVIEWS_FILE={reviews}",
                        *env_lines,
                    )
                )
                + "\n",
            )
            repositories = Path(directory) / "repositories.json"
            repositories.write_text(
                '[{"slug": "owner/repo", "path": "' + directory + '"}]',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                return config.load_settings(
                    env_file, repositories, Path(directory) / "state.json", **overrides
                )

    def test_the_file_names_the_engines_and_env_is_not_read(self) -> None:
        settings = self.load(
            ("REVIEW_ENGINE=command", "REVIEW_EFFORT=nonsense", "MAX_DIFF_BYTES=5000"),
            example(),
        )

        self.assertIsNotNone(settings.reviews)
        self.assertEqual(settings.reviews.source.name, "custom-reviews.json")
        self.assertEqual(settings.review_engine, "kiro")
        self.assertEqual(settings.review_effort, "high")
        self.assertEqual(settings.max_diff_bytes, 200000)
        self.assertEqual(review_plan(settings).names[-1], CHECKS_LEVEL)

    def test_the_command_line_changes_the_files_engines(self) -> None:
        settings = self.load(
            (), example(), review_engine_override="codex", fallback_disabled=True
        )

        code = review_plan(settings).get(CODE_LEVEL)
        self.assertEqual(code.settings.review_engine, "codex")
        self.assertEqual(code.fallbacks, ())

    def test_stale_values_of_replaced_keys_neither_apply_nor_stop_the_run(
        self,
    ) -> None:
        settings = self.load(
            (
                "MAX_DIFF_BYTES=999999",
                "REVIEW_TIMEOUT_SECONDS=5",
                "REVIEW_CONCURRENCY=99",
                "REVIEW_DESIGN_LEVEL=maybe",
                "MAX_FINDINGS=0",
                "REVIEW_ENGINE_ENV_ALLOWLIST=not json",
                "IGNORED_MISSING_PATCH_PATTERNS=not json",
            ),
            example(),
        )

        self.assertEqual(settings.max_diff_bytes, 200000)
        self.assertEqual(settings.review_concurrency, example()["concurrency"])
        self.assertIsNotNone(review_plan(settings).get(DESIGN_LEVEL))

    def test_a_reviews_file_setting_that_names_no_file_stops_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            write_private_env(
                env_file,
                "GITHUB_REVIEWER_USERNAME=reviewer\nREVIEW_ENGINE=claude\n"
                f"REVIEWS_FILE={Path(directory) / 'missing.json'}\n",
            )
            repositories = Path(directory) / "repositories.json"
            repositories.write_text(
                '[{"slug": "owner/repo", "path": "' + directory + '"}]',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ConfigError, "missing.json"):
                    config.load_settings(
                        env_file, repositories, Path(directory) / "state.json"
                    )

    def test_without_the_file_env_names_the_engines_as_before(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            write_private_env(
                env_file,
                "GITHUB_REVIEWER_USERNAME=reviewer\nREVIEW_ENGINE=claude\n"
                "MAX_DIFF_BYTES=5000\n",
            )
            repositories = Path(directory) / "repositories.json"
            repositories.write_text(
                '[{"slug": "owner/repo", "path": "' + directory + '"}]',
                encoding="utf-8",
            )
            locations = Locations(
                "test", Path(directory), Path(directory), Path(directory)
            )
            with patch.dict(os.environ, {}, clear=True), patch.object(
                config, "default_locations", return_value=locations
            ):
                settings = config.load_settings(
                    env_file, repositories, Path(directory) / "state.json"
                )

        self.assertIsNone(settings.reviews)
        self.assertEqual(settings.review_engine, "claude")
        self.assertEqual(settings.max_diff_bytes, 5000)


class DoctorTests(unittest.TestCase):
    def test_the_doctor_names_the_file_and_the_env_keys_it_replaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = settings_from_reviews(
                env_settings(directory), parse_reviews(example(), SOURCE)
            )
            with patch.dict(os.environ, {"REVIEW_ENGINE": "kiro"}, clear=True):
                checks = doctor.check_reviews_file(settings)
            plain = doctor.check_reviews_file(env_settings(directory))

        self.assertIn("names design, docs, code, checks", checks[0].message)
        self.assertEqual(checks[1].status, doctor.NOTE)
        self.assertIn("REVIEW_ENGINE", checks[1].message)
        self.assertIn("no reviews file", plain[0].message)

    def test_the_doctor_checks_every_engine_the_file_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = settings_from_reviews(
                env_settings(directory), parse_reviews(example(), SOURCE)
            )
            with patch("prwarden.doctor.shutil.which", return_value="/bin/cli"), patch(
                "prwarden.reviews_file.parse_command", side_effect=lambda value: tuple(
                    json.loads(value)
                )
            ):
                checks = doctor.check_engines(settings)

        self.assertEqual(
            [check.message.split(":")[0] for check in checks],
            [
                "reviews file engine claude",
                "reviews file engine codex",
                "reviews file engine kiro",
            ],
        )


if __name__ == "__main__":
    unittest.main()
