import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import prwarden.config as config
from prwarden.comment_template import (
    DEFAULT_GITHUB_COMMENT_TAGS_FILE,
    DEFAULT_GITHUB_COMMENT_TEMPLATE_FILE,
)


def write_private_env(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


class EngineOverrideFlagTests(unittest.TestCase):
    """The command line must express a whole pipeline, because reaching one through
    the environment took three variables and knowing which cancels which."""

    def _write_env(self, directory, **values):
        lines = [
            "GITHUB_REVIEWER_USERNAME=reviewer",
            "REVIEW_ENGINE=antigravity",
            "REVIEW_MODEL=gemini-3.1-pro-high",
            'REVIEW_ENGINE_COMMAND=["/opt/bin/agy"]',
            "REVIEW_VERIFY_ENGINE=kiro",
            "REVIEW_VERIFY_MODEL=gpt-5.6-terra",
            "REVIEW_FALLBACK_ENGINE=claude",
            "REVIEW_FALLBACK_MODEL=opus",
        ]
        for key, value in values.items():
            lines.append(f"{key}={value}")
        path = Path(directory) / ".env"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.chmod(0o600)
        repositories = Path(directory) / "repositories.json"
        repositories.write_text(
            '[{"slug": "owner/repo", "path": "' + directory + '"}]', encoding="utf-8"
        )
        return path, repositories

    def test_engine_and_model_together_pin_the_primary(self):
        """--engine alone clears the model, since a model ID belongs to one provider;
        naming one alongside is how the pair is chosen."""

        with tempfile.TemporaryDirectory() as directory:
            env_file, repositories = self._write_env(directory)
            with patch.dict(os.environ, {}, clear=True):
                settings = config.load_settings(
                    env_file,
                    repositories,
                    Path(directory) / "state.json",
                    "kiro",
                    review_model_override="claude-opus-5",
                )

        self.assertEqual(settings.review_engine, "kiro")
        self.assertEqual(settings.review_model, "claude-opus-5")
        # The configured engine's command must not follow the engine that replaced it.
        self.assertTrue(settings.review_engine_command[0].endswith("kiro-cli"))
        self.assertNotIn("agy", settings.review_engine_command[0])

    def test_secondary_engines_take_their_own_models(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file, repositories = self._write_env(directory)
            with patch.dict(os.environ, {}, clear=True):
                settings = config.load_settings(
                    env_file,
                    repositories,
                    Path(directory) / "state.json",
                    verify_engine_override="claude",
                    verify_model_override="opus",
                    fallback_engine_override="kiro",
                    fallback_model_override="claude-opus-5",
                )

        self.assertEqual(settings.verify_engine, "claude")
        self.assertEqual(settings.verify_model, "opus")
        self.assertEqual(settings.fallback_engine, "kiro")
        self.assertEqual(settings.fallback_model, "claude-opus-5")
        self.assertTrue(settings.fallback_engine_command[0].endswith("kiro-cli"))

    def test_a_secondary_engine_does_not_inherit_another_providers_model(self):
        """REVIEW_FALLBACK_MODEL=opus belongs to Claude; Kiro can only reject it."""

        with tempfile.TemporaryDirectory() as directory:
            env_file, repositories = self._write_env(directory)
            with patch.dict(os.environ, {}, clear=True):
                settings = config.load_settings(
                    env_file,
                    repositories,
                    Path(directory) / "state.json",
                    fallback_engine_override="kiro",
                )

        self.assertEqual(settings.fallback_engine, "kiro")
        self.assertIsNone(settings.fallback_model)

    def test_a_model_alone_applies_to_the_configured_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file, repositories = self._write_env(directory)
            with patch.dict(os.environ, {}, clear=True):
                settings = config.load_settings(
                    env_file,
                    repositories,
                    Path(directory) / "state.json",
                    review_model_override="gemini-3.1-pro-low",
                    verify_model_override="gpt-5.6-sol",
                )

        self.assertEqual(settings.review_engine, "antigravity")
        self.assertEqual(settings.review_model, "gemini-3.1-pro-low")
        self.assertEqual(settings.verify_engine, "kiro")
        self.assertEqual(settings.verify_model, "gpt-5.6-sol")

    def test_no_fallback_leaves_the_primary_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file, repositories = self._write_env(directory)
            with patch.dict(os.environ, {}, clear=True):
                settings = config.load_settings(
                    env_file,
                    repositories,
                    Path(directory) / "state.json",
                    fallback_disabled=True,
                )

        self.assertIsNone(settings.fallback_engine)
        self.assertIsNone(settings.fallback_model)
        self.assertEqual(settings.fallback_engine_command, ())
        # Verification is a different role and must be untouched by it.
        self.assertEqual(settings.verify_engine, "kiro")


class EffortSettingTests(unittest.TestCase):
    """Effort reaches two CLIs, and Kiro accepts any string with exit 0, so this
    loader is the only thing that can catch a misspelt level."""

    BASE = (
        "GITHUB_REVIEWER_USERNAME=reviewer",
        "REVIEW_ENGINE=kiro",
        "REVIEW_MODEL=claude-opus-5.5",
        "REVIEW_VERIFY_ENGINE=kiro",
        "REVIEW_VERIFY_MODEL=gpt-5.6-terra",
        "REVIEW_FALLBACK_ENGINE=antigravity",
        "REVIEW_FALLBACK_MODEL=claude-opus-4-6-thinking",
    )

    def load(self, *extra, **overrides):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            write_private_env(env_file, "\n".join(self.BASE + extra) + "\n")
            repositories = Path(directory) / "repositories.json"
            repositories.write_text(
                '[{"slug": "owner/repo", "path": "' + directory + '"}]',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                return config.load_settings(
                    env_file,
                    repositories,
                    Path(directory) / "state.json",
                    **overrides,
                )

    def test_every_role_takes_its_own_effort(self):
        settings = self.load(
            "REVIEW_EFFORT=xhigh",
            "REVIEW_CODE_EFFORT=MAX",
            "REVIEW_VERIFY_EFFORT=xhigh",
            "REVIEW_FALLBACK_EFFORT=high",
        )

        self.assertEqual(settings.review_effort, "xhigh")
        self.assertEqual(settings.review_code_effort, "max")
        self.assertEqual(settings.verify_effort, "xhigh")
        self.assertEqual(settings.fallback_effort, "high")

    def test_no_effort_leaves_every_model_at_its_default(self):
        settings = self.load()

        for name in (
            "review_effort",
            "review_code_effort",
            "verify_effort",
            "fallback_effort",
        ):
            self.assertIsNone(getattr(settings, name), name)

    def test_a_misspelt_level_is_refused_by_name(self):
        """x-high is how the level was first written in conversation."""

        with self.assertRaisesRegex(
            config.ConfigError,
            r"REVIEW_EFFORT must be one of low, medium, high, xhigh, max, "
            r"not 'x-high'",
        ):
            self.load("REVIEW_EFFORT=x-high")

    def test_each_role_is_checked(self):
        for line, name in (
            ("REVIEW_CODE_EFFORT=maximum", "REVIEW_CODE_EFFORT"),
            ("REVIEW_VERIFY_EFFORT=extra", "REVIEW_VERIFY_EFFORT"),
            ("REVIEW_FALLBACK_EFFORT=highest", "REVIEW_FALLBACK_EFFORT"),
        ):
            with self.subTest(name=name):
                with self.assertRaisesRegex(config.ConfigError, f"^{name} must be"):
                    self.load(line)

    def test_an_adapter_that_cannot_pass_effort_refuses_it(self):
        """Dropping it without a word would leave the run at a level nobody chose."""

        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            write_private_env(
                env_file,
                "GITHUB_REVIEWER_USERNAME=reviewer\nREVIEW_ENGINE=kiro\n"
                "REVIEW_FALLBACK_ENGINE=codex\nREVIEW_FALLBACK_EFFORT=max\n",
            )
            repositories = Path(directory) / "repositories.json"
            repositories.write_text(
                '[{"slug": "owner/repo", "path": "' + directory + '"}]',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(
                    config.ConfigError,
                    "REVIEW_FALLBACK_EFFORT is not supported by the codex adapter",
                ):
                    config.load_settings(
                        env_file, repositories, Path(directory) / "state.json"
                    )

    def test_the_verifier_takes_a_fallback_of_its_own(self):
        """Kiro served review and verification, so while it was down nothing the
        review fallback found could be verified or delivered."""

        settings = self.load(
            "REVIEW_VERIFY_FALLBACK_ENGINE=claude",
            "REVIEW_VERIFY_FALLBACK_MODEL=claude-sonnet-5-5",
            "REVIEW_VERIFY_FALLBACK_EFFORT=XHIGH",
        )

        self.assertEqual(settings.verify_fallback_engine, "claude")
        self.assertTrue(settings.verify_fallback_engine_command[0].endswith("claude"))
        self.assertEqual(settings.verify_fallback_model, "claude-sonnet-5-5")
        self.assertEqual(settings.verify_fallback_effort, "xhigh")

    def test_a_verification_fallback_needs_a_verifier(self):
        """Without a separate verifier the reviewer verifies, and its own fallback
        already covers it."""

        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            write_private_env(
                env_file,
                "GITHUB_REVIEWER_USERNAME=reviewer\nREVIEW_ENGINE=kiro\n"
                "REVIEW_VERIFY_FALLBACK_ENGINE=claude\n",
            )
            repositories = Path(directory) / "repositories.json"
            repositories.write_text(
                '[{"slug": "owner/repo", "path": "' + directory + '"}]',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(
                    config.ConfigError,
                    "REVIEW_VERIFY_FALLBACK_ENGINE requires REVIEW_VERIFY_ENGINE",
                ):
                    config.load_settings(
                        env_file, repositories, Path(directory) / "state.json"
                    )

    def test_a_verification_fallback_equal_to_the_verifier_is_none(self):
        """Repeating a failed call on the same engine and model recovers nothing."""

        from prwarden.engine import verify_fallback_engine_settings

        same = self.load(
            "REVIEW_VERIFY_FALLBACK_ENGINE=kiro",
            "REVIEW_VERIFY_FALLBACK_MODEL=gpt-5.6-terra",
        )
        other = self.load(
            "REVIEW_VERIFY_FALLBACK_ENGINE=claude",
            "REVIEW_VERIFY_FALLBACK_MODEL=claude-sonnet-5-5",
            "REVIEW_VERIFY_FALLBACK_EFFORT=xhigh",
            "REVIEW_CODE_EFFORT=max",
        )

        self.assertIsNone(verify_fallback_engine_settings(same))
        fallback = verify_fallback_engine_settings(other)
        self.assertEqual(
            (fallback.review_engine, fallback.review_model, fallback.review_effort),
            ("claude", "claude-sonnet-5-5", "xhigh"),
        )
        self.assertIsNone(fallback.review_code_effort)

    def test_a_secondary_effort_needs_its_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            write_private_env(
                env_file,
                "GITHUB_REVIEWER_USERNAME=reviewer\nREVIEW_ENGINE=kiro\n"
                "REVIEW_VERIFY_EFFORT=xhigh\n",
            )
            repositories = Path(directory) / "repositories.json"
            repositories.write_text(
                '[{"slug": "owner/repo", "path": "' + directory + '"}]',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(
                    config.ConfigError,
                    "REVIEW_VERIFY_EFFORT requires REVIEW_VERIFY_ENGINE",
                ):
                    config.load_settings(
                        env_file, repositories, Path(directory) / "state.json"
                    )

    def test_switching_the_primary_engine_drops_its_effort(self):
        """Antigravity refuses an effort its default model does not take, so the
        configured level must not follow --engine to another provider."""

        settings = self.load(
            "REVIEW_EFFORT=xhigh",
            "REVIEW_CODE_EFFORT=max",
            review_engine_override="antigravity",
        )

        self.assertEqual(settings.review_engine, "antigravity")
        self.assertIsNone(settings.review_effort)
        self.assertIsNone(settings.review_code_effort)

    def test_naming_the_same_engine_keeps_its_effort(self):
        """The single-PR command names --engine kiro alongside a kiro configuration."""

        settings = self.load(
            "REVIEW_EFFORT=xhigh",
            "REVIEW_CODE_EFFORT=max",
            review_engine_override="kiro",
            review_model_override="claude-opus-5",
        )

        self.assertEqual(settings.review_effort, "xhigh")
        self.assertEqual(settings.review_code_effort, "max")

    def test_a_secondary_switched_to_another_engine_drops_its_effort(self):
        settings = self.load(
            "REVIEW_FALLBACK_EFFORT=max",
            fallback_engine_override="kiro",
            fallback_model_override="claude-opus-4.8",
        )

        self.assertEqual(settings.fallback_engine, "kiro")
        self.assertIsNone(settings.fallback_effort)

    def test_no_fallback_drops_its_effort(self):
        settings = self.load("REVIEW_FALLBACK_EFFORT=max", fallback_disabled=True)

        self.assertIsNone(settings.fallback_engine)
        self.assertIsNone(settings.fallback_effort)


class ConfigurationTests(unittest.TestCase):
    def test_load_settings_from_local_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repositories_file = root / "repositories.json"
            repositories_file.write_text(
                json.dumps(
                    [{"slug": "example/example-app", "path": "./example-app"}]
                ),
                encoding="utf-8",
            )
            env_file = root / ".env"
            write_private_env(
                env_file,
                "AUTO_REVIEW_DRY_RUN=yes\n"
                'EXCLUDED_PR_LABELS=["In Progress", "Do Not Review"]\n'
                'IGNORED_MISSING_PATCH_PATTERNS=["*.tsbuildinfo", "generated/**"]\n'
                f"REPOSITORIES_FILE={repositories_file}\n"
                f"STATE_FILE={root / 'state.json'}\n",
            )

            with patch.dict(os.environ, {}, clear=True), patch(
                "prwarden.config.resolve_github_username",
                return_value="reviewer",
            ):
                settings = config.load_settings(env_file)

            self.assertEqual(settings.reviewer_username, "reviewer")
            self.assertTrue(settings.dry_run)
            self.assertEqual(
                settings.excluded_pr_labels,
                frozenset({"in progress", "do not review"}),
            )
            self.assertEqual(
                settings.ignored_missing_patch_patterns,
                ("*.tsbuildinfo", "generated/**"),
            )
            self.assertEqual(settings.repositories[0].name, "example-app")
            self.assertEqual(settings.repositories[0].slug, "example/example-app")
            self.assertEqual(
                settings.repositories[0].path,
                (root / "example-app").resolve(),
            )

    def test_run_log_retention_defaults_to_a_week_and_is_configurable(self):
        """How long transcripts are kept is an operator decision."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repositories_file = root / "repositories.json"
            repositories_file.write_text(
                json.dumps([{"slug": "example/example-app", "path": "."}]),
                encoding="utf-8",
            )
            base = (
                f"REPOSITORIES_FILE={repositories_file}\n"
                f"STATE_FILE={root / 'state.json'}\n"
            )
            env_file = root / ".env"

            def settings_for(extra: str):
                write_private_env(env_file, base + extra)
                with patch.dict(os.environ, {}, clear=True), patch(
                    "prwarden.config.resolve_github_username", return_value="reviewer"
                ):
                    return config.load_settings(env_file)

            self.assertEqual(settings_for("").run_log_retention_days, 7)
            self.assertEqual(
                settings_for("RUN_LOG_RETENTION_DAYS=30\n").run_log_retention_days,
                30,
            )

            for invalid in ("0", "-1", "8.5", "week", str(
                config.MAX_RUN_LOG_RETENTION_DAYS + 1
            )):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(config.ConfigError):
                        settings_for(f"RUN_LOG_RETENTION_DAYS={invalid}\n")

    def test_design_level_is_on_by_default_and_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repositories_file = root / "repositories.json"
            repositories_file.write_text(
                json.dumps([{"slug": "example/example-app", "path": "."}]),
                encoding="utf-8",
            )
            base = (
                f"REPOSITORIES_FILE={repositories_file}\n"
                f"STATE_FILE={root / 'state.json'}\n"
            )
            env_file = root / ".env"

            def settings_for(extra: str):
                write_private_env(env_file, base + extra)
                with patch.dict(os.environ, {}, clear=True), patch(
                    "prwarden.config.resolve_github_username", return_value="reviewer"
                ):
                    return config.load_settings(env_file)

            defaults = settings_for("")
            self.assertTrue(defaults.design_level)
            self.assertEqual(defaults.max_design_findings, 4)
            self.assertFalse(settings_for("REVIEW_DESIGN_LEVEL=0\n").design_level)
            self.assertEqual(
                settings_for("MAX_DESIGN_FINDINGS=2\n").max_design_findings, 2
            )

            for variable, invalid in (
                ("REVIEW_DESIGN_LEVEL", "sometimes"),
                ("MAX_DESIGN_FINDINGS", "0"),
                ("MAX_DESIGN_FINDINGS", "11"),
            ):
                with self.subTest(variable=variable, invalid=invalid):
                    with self.assertRaises(config.ConfigError):
                        settings_for(f"{variable}={invalid}\n")

    def test_verification_budget_settings_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repositories_file = root / "repositories.json"
            repositories_file.write_text(
                json.dumps([{"slug": "example/example-app", "path": "."}]),
                encoding="utf-8",
            )
            base = (
                f"REPOSITORIES_FILE={repositories_file}\n"
                f"STATE_FILE={root / 'state.json'}\n"
            )
            env_file = root / ".env"

            def settings_for(extra: str):
                write_private_env(env_file, base + extra)
                with patch.dict(os.environ, {}, clear=True), patch(
                    "prwarden.config.resolve_github_username", return_value="reviewer"
                ):
                    return config.load_settings(env_file)

            defaults = settings_for("")
            self.assertEqual(defaults.review_verify_timeout_seconds, 900)
            self.assertEqual(
                defaults.review_verify_timeout_per_candidate_seconds, 180
            )
            configured = settings_for(
                "REVIEW_VERIFY_TIMEOUT_SECONDS=1200\n"
                "REVIEW_VERIFY_TIMEOUT_PER_CANDIDATE_SECONDS=0\n"
            )
            self.assertEqual(configured.review_verify_timeout_seconds, 1200)
            self.assertEqual(
                configured.review_verify_timeout_per_candidate_seconds, 0
            )

            for variable, invalid in (
                ("REVIEW_VERIFY_TIMEOUT_SECONDS", "29"),
                ("REVIEW_VERIFY_TIMEOUT_SECONDS", "3601"),
                ("REVIEW_VERIFY_TIMEOUT_SECONDS", "quarter-hour"),
                ("REVIEW_VERIFY_TIMEOUT_PER_CANDIDATE_SECONDS", "-1"),
                ("REVIEW_VERIFY_TIMEOUT_PER_CANDIDATE_SECONDS", "601"),
            ):
                with self.subTest(variable=variable, invalid=invalid):
                    with self.assertRaises(config.ConfigError):
                        settings_for(f"{variable}={invalid}\n")

    def test_process_environment_takes_precedence_over_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            write_private_env(env_file, "EXAMPLE_SETTING=file-value\n")
            with patch.dict(
                os.environ,
                {"EXAMPLE_SETTING": "process-value"},
                clear=True,
            ):
                config.load_env_file(env_file)
                self.assertEqual(os.environ["EXAMPLE_SETTING"], "process-value")

    def test_loads_custom_github_comment_template_and_tag_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repositories_file = root / "repositories.json"
            repositories_file.write_text(
                '[{"slug":"example/app","path":"."}]',
                encoding="utf-8",
            )
            template_file = root / "comment.md"
            template_file.write_text(
                "[priority]: [problem]\n[identity]\n",
                encoding="utf-8",
            )
            tags_file = root / "tags.json"
            tags_file.write_text(
                json.dumps(
                    {
                        "priority": "severity",
                        "problem": "problem",
                        "identity": "marker",
                    }
                ),
                encoding="utf-8",
            )
            env_file = root / ".env"
            write_private_env(
                env_file,
                f"REPOSITORIES_FILE={repositories_file}\n"
                f"GITHUB_COMMENT_TEMPLATE_FILE={template_file}\n"
                f"GITHUB_COMMENT_TAGS_FILE={tags_file}\n",
            )

            with patch.dict(os.environ, {}, clear=True), patch(
                "prwarden.config.resolve_github_username",
                return_value="reviewer",
            ):
                settings = config.load_settings(env_file)

            self.assertEqual(
                settings.github_comment_template.source,
                template_file.resolve(),
            )
            self.assertEqual(
                settings.github_comment_template.tags["priority"],
                "severity",
            )

    def test_rejects_invalid_github_comment_template_during_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repositories_file = root / "repositories.json"
            repositories_file.write_text(
                '[{"slug":"example/app","path":"."}]',
                encoding="utf-8",
            )
            template_file = root / "comment.md"
            template_file.write_text("[missing]\n", encoding="utf-8")
            tags_file = root / "tags.json"
            tags_file.write_text('{"marker":"marker"}', encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "GITHUB_COMMENT_TEMPLATE_FILE": str(template_file),
                    "GITHUB_COMMENT_TAGS_FILE": str(tags_file),
                },
                clear=True,
            ):
                with self.assertRaisesRegex(config.ConfigError, "unknown tags"):
                    config.load_settings(root / "missing.env", repositories_file)

    @unittest.skipIf(os.name == "nt", "POSIX permissions are not available")
    def test_rejects_dotenv_readable_by_group_or_other_users(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("SLACK_WEBHOOK_URL=secret\n", encoding="utf-8")
            env_file.chmod(0o644)

            with self.assertRaisesRegex(config.ConfigError, "must have mode 0600"):
                config.load_env_file(env_file)

    def test_duplicate_repository_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "repositories.json"
            path.write_text(
                json.dumps(
                    [
                        {"slug": "example/repository"},
                        {"slug": "example/repository"},
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(config.ConfigError, "Duplicate"):
                config.load_repositories(path)

    def test_review_command_requires_json_string_array(self):
        self.assertEqual(config.parse_command("[\"reviewer\", \"--json\"]"), ("reviewer", "--json"))
        with self.assertRaises(config.ConfigError):
            config.parse_command("reviewer --json")
        self.assertEqual(
            config.parse_env_allowlist("[\"PROVIDER_API_KEY\"]"),
            ("PROVIDER_API_KEY",),
        )
        with self.assertRaises(config.ConfigError):
            config.parse_env_allowlist("[\"INVALID-NAME\"]")
        self.assertEqual(
            config.parse_excluded_pr_labels('[" In Progress ", "BLOCKED"]'),
            frozenset({"in progress", "blocked"}),
        )
        with self.assertRaises(config.ConfigError):
            config.parse_excluded_pr_labels('[" "]')
        self.assertEqual(
            config.parse_ignored_missing_patch_patterns(
                '[" *.tsbuildinfo ", "generated/**"]'
            ),
            ("*.tsbuildinfo", "generated/**"),
        )
        with self.assertRaises(config.ConfigError):
            config.parse_ignored_missing_patch_patterns('[""]')

    def test_engine_defaults_and_runtime_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repositories_file = root / "repositories.json"
            repositories_file.write_text(
                '[{"slug":"example/app","path":"."}]', encoding="utf-8"
            )
            env_file = root / ".env"
            write_private_env(
                env_file,
                'REVIEW_ENGINE=antigravity\nREVIEW_ENGINE_COMMAND=["agy-wrapper"]\n',
            )
            with patch.dict(os.environ, {}, clear=True), patch(
                "prwarden.config.resolve_github_username", return_value="reviewer"
            ):
                settings = config.load_settings(
                    env_file,
                    repositories_file,
                    review_engine_override="codex",
                )
            self.assertEqual(settings.review_engine, "codex")
            self.assertEqual(Path(settings.review_engine_command[0]).name, "codex")

    def test_selecting_another_engine_drops_the_configured_model(self):
        """A model ID belongs to the provider that publishes it.

        Carrying REVIEW_MODEL onto a different engine sent Antigravity's
        gemini-3.1-pro-high to the Claude CLI as --model, which only that provider
        could reject, so --engine could not work for anybody who had set a model.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repositories_file = root / "repositories.json"
            repositories_file.write_text(
                '[{"slug":"example/app","path":"."}]', encoding="utf-8"
            )
            env_file = root / ".env"
            write_private_env(
                env_file,
                "REVIEW_ENGINE=antigravity\nREVIEW_MODEL=gemini-3.1-pro-high\n",
            )
            with patch.dict(os.environ, {}, clear=True), patch(
                "prwarden.config.resolve_github_username", return_value="reviewer"
            ):
                switched = config.load_settings(
                    env_file,
                    repositories_file,
                    review_engine_override="claude",
                )
                same = config.load_settings(
                    env_file,
                    repositories_file,
                    review_engine_override="antigravity",
                )
                unchanged = config.load_settings(env_file, repositories_file)

            self.assertIsNone(switched.review_model)
            # Naming the engine already configured changes nothing.
            self.assertEqual(same.review_model, "gemini-3.1-pro-high")
            self.assertEqual(unchanged.review_model, "gemini-3.1-pro-high")

    def test_verification_engine_and_model_are_optional_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repositories_file = root / "repositories.json"
            repositories_file.write_text(
                '[{"slug":"example/app","path":"."}]', encoding="utf-8"
            )
            env_file = root / "missing.env"

            with patch.dict(os.environ, {}, clear=True), patch(
                "prwarden.config.resolve_github_username", return_value="reviewer"
            ):
                settings = config.load_settings(env_file, repositories_file)
            self.assertIsNone(settings.verify_engine)
            self.assertIsNone(settings.verify_model)
            self.assertEqual(settings.verify_engine_command, ())

            with patch.dict(
                os.environ,
                {
                    "REVIEW_VERIFY_ENGINE": "Kiro",
                    "REVIEW_VERIFY_MODEL": "claude-opus-5",
                },
                clear=True,
            ), patch(
                "prwarden.config.resolve_github_username", return_value="reviewer"
            ):
                settings = config.load_settings(env_file, repositories_file)
            self.assertEqual(settings.verify_engine, "kiro")
            self.assertEqual(settings.verify_model, "claude-opus-5")
            self.assertEqual(
                Path(settings.verify_engine_command[0]).name, "kiro-cli"
            )

            with patch.dict(
                os.environ, {"REVIEW_VERIFY_ENGINE": "command"}, clear=True
            ), patch(
                "prwarden.config.resolve_github_username", return_value="reviewer"
            ):
                with self.assertRaisesRegex(
                    config.ConfigError, "Unsupported REVIEW_VERIFY_ENGINE"
                ):
                    config.load_settings(env_file, repositories_file)

            with patch.dict(
                os.environ, {"REVIEW_VERIFY_MODEL": "claude-opus-5"}, clear=True
            ), patch(
                "prwarden.config.resolve_github_username", return_value="reviewer"
            ):
                with self.assertRaisesRegex(
                    config.ConfigError, "REVIEW_VERIFY_MODEL requires"
                ):
                    config.load_settings(env_file, repositories_file)

    def test_fallback_engine_and_model_are_optional_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repositories_file = root / "repositories.json"
            repositories_file.write_text(
                '[{"slug":"example/app","path":"."}]', encoding="utf-8"
            )
            env_file = root / "missing.env"

            with patch.dict(os.environ, {}, clear=True), patch(
                "prwarden.config.resolve_github_username", return_value="reviewer"
            ):
                settings = config.load_settings(env_file, repositories_file)
            self.assertIsNone(settings.fallback_engine)
            self.assertIsNone(settings.fallback_model)
            self.assertEqual(settings.fallback_engine_command, ())

            with patch.dict(
                os.environ,
                {
                    "REVIEW_FALLBACK_ENGINE": "Claude",
                    "REVIEW_FALLBACK_MODEL": "claude-opus-5",
                },
                clear=True,
            ), patch(
                "prwarden.config.resolve_github_username", return_value="reviewer"
            ):
                settings = config.load_settings(env_file, repositories_file)
            self.assertEqual(settings.fallback_engine, "claude")
            self.assertEqual(settings.fallback_model, "claude-opus-5")
            self.assertEqual(
                Path(settings.fallback_engine_command[0]).name, "claude"
            )

            with patch.dict(
                os.environ, {"REVIEW_FALLBACK_ENGINE": "command"}, clear=True
            ), patch(
                "prwarden.config.resolve_github_username", return_value="reviewer"
            ):
                with self.assertRaisesRegex(
                    config.ConfigError, "Unsupported REVIEW_FALLBACK_ENGINE"
                ):
                    config.load_settings(env_file, repositories_file)

            with patch.dict(
                os.environ, {"REVIEW_FALLBACK_MODEL": "claude-opus-5"}, clear=True
            ), patch(
                "prwarden.config.resolve_github_username", return_value="reviewer"
            ):
                with self.assertRaisesRegex(
                    config.ConfigError, "REVIEW_FALLBACK_MODEL requires"
                ):
                    config.load_settings(env_file, repositories_file)

    def test_unknown_engine_is_rejected_before_command_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repositories_file = root / "repositories.json"
            repositories_file.write_text('[{"slug":"example/app"}]', encoding="utf-8")
            with patch.dict(os.environ, {"REVIEW_ENGINE": "unknown"}, clear=True):
                with self.assertRaisesRegex(config.ConfigError, "Unsupported"):
                    config.load_settings(root / "missing.env", repositories_file)

    def test_command_engine_requires_explicit_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repositories_file = root / "repositories.json"
            repositories_file.write_text(
                '[{"slug":"example/app"}]', encoding="utf-8"
            )
            with patch.dict(os.environ, {"REVIEW_ENGINE": "command"}, clear=True), patch(
                "prwarden.config.resolve_github_username", return_value="reviewer"
            ):
                with self.assertRaisesRegex(config.ConfigError, "is required"):
                    config.load_settings(root / "missing.env", repositories_file)

    def test_invalid_limits_are_rejected(self):
        with self.assertRaises(config.ConfigError):
            config.parse_integer("11", "MAX_FINDINGS", 1, 10)
        with self.assertRaises(config.ConfigError):
            config.parse_bool("sometimes", "AUTO_REVIEW_DRY_RUN")


class EngineSwitchTests(unittest.TestCase):
    """A command written beside one engine does not belong to another engine.

    The Claude adapter once built Claude arguments and handed them to Antigravity's
    binary, because `REVIEW_ENGINE=claude` in the environment left the file's
    `REVIEW_ENGINE_COMMAND` in place. The binary answered with its own help text and
    the pull request went unreviewed.
    """

    def settings_for(self, environment: dict, engine_line: str = "antigravity"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repositories_file = root / "repositories.json"
            repositories_file.write_text(
                json.dumps([{"slug": "example/app", "path": "./app"}]),
                encoding="utf-8",
            )
            env_file = root / ".env"
            write_private_env(
                env_file,
                f"REVIEW_ENGINE={engine_line}\n"
                'REVIEW_ENGINE_COMMAND=["/opt/agy"]\n'
                "REVIEW_MODEL=gemini-3.1-pro-high\n"
                f"REPOSITORIES_FILE={repositories_file}\n"
                f"STATE_FILE={root / 'state.json'}\n",
            )
            with patch.dict(os.environ, environment, clear=True), patch(
                "prwarden.config.resolve_github_username", return_value="reviewer"
            ), patch(
                "prwarden.config.parse_command", side_effect=lambda value: tuple(
                    json.loads(value)
                )
            ):
                return config.load_settings(env_file)

    def test_the_files_command_and_model_stay_with_the_files_engine(self):
        settings = self.settings_for({})

        self.assertEqual(settings.review_engine, "antigravity")
        self.assertEqual(settings.review_engine_command, ("/opt/agy",))
        self.assertEqual(settings.review_model, "gemini-3.1-pro-high")

    def test_switching_engine_by_environment_drops_the_files_command(self):
        settings = self.settings_for({"REVIEW_ENGINE": "claude"})

        self.assertEqual(settings.review_engine, "claude")
        self.assertNotIn("/opt/agy", settings.review_engine_command)
        self.assertIsNone(settings.review_model)

    def test_a_command_the_environment_supplies_is_kept(self):
        """It was chosen with the new engine in view, so it is not a leftover."""

        settings = self.settings_for(
            {
                "REVIEW_ENGINE": "claude",
                "REVIEW_ENGINE_COMMAND": '["/usr/bin/env", "claude"]',
                "REVIEW_MODEL": "sonnet",
            }
        )

        self.assertEqual(settings.review_engine_command, ("/usr/bin/env", "claude"))
        self.assertEqual(settings.review_model, "sonnet")

    def test_a_file_naming_no_engine_keeps_its_command(self):
        """Without a declared engine there is nothing to have switched away from."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repositories_file = root / "repositories.json"
            repositories_file.write_text(
                json.dumps([{"slug": "example/app", "path": "./app"}]),
                encoding="utf-8",
            )
            env_file = root / ".env"
            write_private_env(
                env_file,
                'REVIEW_ENGINE_COMMAND=["/opt/custom"]\n'
                f"REPOSITORIES_FILE={repositories_file}\n"
                f"STATE_FILE={root / 'state.json'}\n",
            )
            with patch.dict(os.environ, {}, clear=True), patch(
                "prwarden.config.resolve_github_username", return_value="reviewer"
            ), patch(
                "prwarden.config.parse_command",
                side_effect=lambda value: tuple(json.loads(value)),
            ):
                settings = config.load_settings(env_file)

        self.assertEqual(settings.review_engine_command, ("/opt/custom",))


class GithubUsernameResolutionTests(unittest.TestCase):
    def test_rest_lookup_is_preferred_and_not_followed_by_fallbacks(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "prwarden.config.run_cmd", return_value="reviewer\n"
        ) as run_command:
            self.assertEqual(config.resolve_github_username(), "reviewer")
        self.assertEqual(run_command.call_count, 1)
        self.assertEqual(
            run_command.call_args.args[0],
            ["gh", "api", "user", "--jq", ".login"],
        )

    def test_graphql_viewer_is_used_when_rest_user_endpoint_fails(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "prwarden.config.run_cmd",
            side_effect=[
                config.CommandError(
                    "gh exited with 1: gh: No server is currently available to "
                    "service your request. (HTTP 503)"
                ),
                "reviewer\n",
            ],
        ) as run_command:
            self.assertEqual(config.resolve_github_username(), "reviewer")
        self.assertEqual(run_command.call_count, 2)
        self.assertEqual(
            run_command.call_args.args[0],
            [
                "gh",
                "api",
                "graphql",
                "-f",
                "query={viewer{login}}",
                "--jq",
                ".data.viewer.login",
            ],
        )

    def test_only_the_last_identity_source_spends_transient_retries(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "prwarden.config.run_cmd",
            side_effect=[config.CommandError("rest boom (HTTP 503)"), "reviewer"],
        ) as run_command:
            self.assertEqual(config.resolve_github_username(), "reviewer")
        first_call, last_call = run_command.call_args_list
        self.assertFalse(first_call.kwargs["retry_transient"])
        self.assertTrue(last_call.kwargs["retry_transient"])

    def test_configured_username_bypasses_github_lookups(self):
        with patch.dict(
            os.environ,
            {"GITHUB_REVIEWER_USERNAME": " configured-user \n"},
            clear=True,
        ), patch("prwarden.config.run_cmd") as run_command:
            self.assertEqual(config.resolve_github_username(), "configured-user")
        run_command.assert_not_called()

    def test_invalid_configured_username_is_rejected_without_falling_back(self):
        with patch.dict(
            os.environ,
            {"GITHUB_REVIEWER_USERNAME": "not a login"},
            clear=True,
        ), patch("prwarden.config.run_cmd") as run_command:
            with self.assertRaisesRegex(
                config.ConfigError, "GITHUB_REVIEWER_USERNAME"
            ):
                config.resolve_github_username()
        run_command.assert_not_called()

    def test_blank_configured_username_falls_back_to_github(self):
        with patch.dict(
            os.environ,
            {"GITHUB_REVIEWER_USERNAME": "   "},
            clear=True,
        ), patch("prwarden.config.run_cmd", return_value="reviewer") as run_command:
            self.assertEqual(config.resolve_github_username(), "reviewer")
        self.assertEqual(run_command.call_count, 1)

    def test_empty_github_response_falls_back_to_graphql(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "prwarden.config.run_cmd", side_effect=["", "reviewer"]
        ) as run_command:
            self.assertEqual(config.resolve_github_username(), "reviewer")
        self.assertEqual(run_command.call_count, 2)

    def test_all_sources_failing_reports_every_attempt_and_the_override(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "prwarden.config.run_cmd",
            side_effect=[
                config.CommandError("rest boom (HTTP 503)"),
                config.CommandError("graphql boom (HTTP 503)"),
            ],
        ):
            with self.assertRaises(config.ConfigError) as raised:
                config.resolve_github_username()

        detail = str(raised.exception)
        self.assertIn("Cannot resolve the current GitHub CLI user", detail)
        self.assertIn("GITHUB_REVIEWER_USERNAME", detail)
        self.assertIn("rest boom", detail)
        self.assertIn("graphql boom", detail)

    def test_configured_username_is_used_by_load_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repositories_file = root / "repositories.json"
            repositories_file.write_text(
                '[{"slug":"example/app","path":"."}]', encoding="utf-8"
            )
            env_file = root / ".env"
            write_private_env(env_file, "GITHUB_REVIEWER_USERNAME=configured-user\n")
            with patch.dict(os.environ, {}, clear=True), patch(
                "prwarden.config.run_cmd"
            ) as run_command:
                settings = config.load_settings(env_file, repositories_file)
            self.assertEqual(settings.reviewer_username, "configured-user")
            run_command.assert_not_called()


class ConfigurationLocationTests(unittest.TestCase):
    """Where an installed copy finds its configuration, since it has no checkout."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        (self.home / "repositories.json").write_text(
            '[{"slug": "example/app", "path": "."}]', encoding="utf-8"
        )
        username = patch(
            "prwarden.config.resolve_github_username", return_value="reviewer"
        )
        username.start()
        self.addCleanup(username.stop)

    def _load(self, env: str = "", **environ: str) -> config.Settings:
        write_private_env(self.home / ".env", env)
        with patch.dict(
            os.environ, {"PRWARDEN_HOME": str(self.home), **environ}, clear=True
        ):
            return config.load_settings()

    def test_everything_defaults_to_the_configured_home(self) -> None:
        settings = self._load("AUTO_REVIEW_DRY_RUN=0\n")

        self.assertFalse(settings.dry_run)
        self.assertEqual(settings.repositories[0].path, self.home)
        self.assertEqual(
            settings.state_file, self.home / ".state" / "review-state.json"
        )

    def test_relative_settings_resolve_from_the_configuration_directory(
        self,
    ) -> None:
        """Not from wherever the run started, which a scheduler does not choose."""

        (self.home / "private").mkdir()
        (self.home / "repositories.json").rename(
            self.home / "private" / "repositories.json"
        )

        settings = self._load(
            "REPOSITORIES_FILE=private/repositories.json\n"
            "STATE_FILE=records/state.json\n"
        )

        self.assertEqual(settings.repositories[0].path, self.home / "private")
        self.assertEqual(settings.state_file, self.home / "records" / "state.json")

    def test_the_old_default_template_paths_still_mean_the_defaults(self) -> None:
        """`.env.example` named them under templates/ until they moved."""

        settings = self._load(
            "GITHUB_COMMENT_TEMPLATE_FILE=templates/github-comment.md\n"
            "GITHUB_COMMENT_TAGS_FILE=./templates/github-comment-tags.json\n"
        )

        self.assertEqual(
            settings.github_comment_template.source,
            DEFAULT_GITHUB_COMMENT_TEMPLATE_FILE,
        )
        self.assertEqual(
            settings.github_comment_template.tags_source,
            DEFAULT_GITHUB_COMMENT_TAGS_FILE,
        )

    def test_a_template_kept_in_the_configuration_directory_is_used(self) -> None:
        (self.home / "templates").mkdir()
        override = self.home / "templates" / "github-comment.md"
        override.write_text(
            DEFAULT_GITHUB_COMMENT_TEMPLATE_FILE.read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        settings = self._load(
            "GITHUB_COMMENT_TEMPLATE_FILE=templates/github-comment.md\n"
        )

        self.assertEqual(settings.github_comment_template.source, override)

    def test_a_missing_configured_template_is_still_an_error(self) -> None:
        with self.assertRaises(config.ConfigError):
            self._load("GITHUB_COMMENT_TEMPLATE_FILE=my-comment.md\n")

    def test_a_relative_home_is_a_configuration_error(self) -> None:
        with patch.dict(os.environ, {"PRWARDEN_HOME": "relative"}, clear=True):
            with self.assertRaisesRegex(config.ConfigError, "PRWARDEN_HOME"):
                config.load_settings()


if __name__ == "__main__":
    unittest.main()
