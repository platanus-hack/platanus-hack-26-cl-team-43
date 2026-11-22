"""Unit tests for the deterministic adversarial prompt mutation engine."""

from __future__ import annotations

import base64

import pytest

from app.services.mutator import DEFAULT_STRATEGIES, MutatedPrompt, MutationEngine

BENIGN_PROMPT = "Summarize the security policy for handling API keys."
TEST_CATEGORY = "prompt_injection"


class TestAvailableStrategiesAndConstants:
    """Verify public strategy constants and engine discovery."""

    def test_default_strategies_constant(self) -> None:
        assert DEFAULT_STRATEGIES == (
            "indirect_injection",
            "roleplay_framing",
            "base64_encoding",
            "delimiter_escape",
        )

    def test_available_strategies_returns_tuple_with_all_defaults(self) -> None:
        engine = MutationEngine()
        strategies = engine.available_strategies

        assert isinstance(strategies, tuple)
        assert strategies == DEFAULT_STRATEGIES
        assert len(strategies) == 4


class TestIndividualStrategies:
    """Verify each mutation strategy produces expected benign transformations."""

    def test_indirect_injection_embeds_prompt_in_document_or_email_framing(
        self,
    ) -> None:
        engine = MutationEngine(seed=42)
        result = engine.mutate(
            prompt=BENIGN_PROMPT,
            category=TEST_CATEGORY,
            strategy="indirect_injection",
            dataset_id=1,
        )

        assert isinstance(result, MutatedPrompt)
        assert result.strategy == "indirect_injection"
        assert result.original_prompt == BENIGN_PROMPT
        assert BENIGN_PROMPT in result.mutated_prompt
        assert result.mutated_prompt != BENIGN_PROMPT

        lowered = result.mutated_prompt.lower()
        assert any(
            marker in lowered
            for marker in (
                "document",
                "email",
                "message",
                "excerpt",
                "memo",
                "quote",
                "forwarded",
                "from:",
                "subject:",
                "attachment",
                "note",
                "---",
                ">",
            )
        )

    def test_roleplay_framing_embeds_prompt_in_security_audit_persona(self) -> None:
        engine = MutationEngine(seed=42)
        result = engine.mutate(
            prompt=BENIGN_PROMPT,
            category=TEST_CATEGORY,
            strategy="roleplay_framing",
            dataset_id=2,
        )

        assert isinstance(result, MutatedPrompt)
        assert result.strategy == "roleplay_framing"
        assert result.original_prompt == BENIGN_PROMPT
        assert BENIGN_PROMPT in result.mutated_prompt
        assert result.mutated_prompt != BENIGN_PROMPT

        lowered = result.mutated_prompt.lower()
        assert any(
            marker in lowered
            for marker in (
                "persona",
                "roleplay",
                "role-play",
                "fictional",
                "security",
                "audit",
                "auditor",
                "evaluator",
                "simulation",
                "scenario",
                "character",
                "red-team",
                "red team",
            )
        )

    def test_base64_encoding_embeds_encoded_prompt_and_decoding_context(self) -> None:
        engine = MutationEngine(seed=42)
        result = engine.mutate(
            prompt=BENIGN_PROMPT,
            category=TEST_CATEGORY,
            strategy="base64_encoding",
            dataset_id=3,
        )

        expected_b64 = base64.b64encode(BENIGN_PROMPT.encode("utf-8")).decode("ascii")

        assert isinstance(result, MutatedPrompt)
        assert result.strategy == "base64_encoding"
        assert result.original_prompt == BENIGN_PROMPT
        assert expected_b64 in result.mutated_prompt

        lowered = result.mutated_prompt.lower()
        assert any(token in lowered for token in ("decode", "base64", "encoded"))
        assert any(
            token in lowered
            for token in (
                "safety",
                "evaluat",
                "audit",
                "security",
                "review",
                "assess",
                "analy",
                "policy",
            )
        )

    def test_delimiter_escape_includes_context_system_boundary_and_prompt(self) -> None:
        engine = MutationEngine(seed=42)
        result = engine.mutate(
            prompt=BENIGN_PROMPT,
            category=TEST_CATEGORY,
            strategy="delimiter_escape",
            dataset_id=4,
        )

        assert isinstance(result, MutatedPrompt)
        assert result.strategy == "delimiter_escape"
        assert result.original_prompt == BENIGN_PROMPT
        assert "</context><system>" in result.mutated_prompt
        assert BENIGN_PROMPT in result.mutated_prompt


class TestErrorHandling:
    """Verify validation errors for unknown strategies and invalid configuration."""

    def test_mutate_with_unknown_strategy_raises_value_error(self) -> None:
        engine = MutationEngine(seed=42)
        with pytest.raises(ValueError):
            engine.mutate(
                prompt=BENIGN_PROMPT,
                category=TEST_CATEGORY,
                strategy="nonexistent_strategy",
            )

    def test_generate_suite_with_unknown_strategy_raises_value_error(self) -> None:
        engine = MutationEngine(seed=42)
        with pytest.raises(ValueError):
            engine.generate_suite(
                prompt=BENIGN_PROMPT,
                category=TEST_CATEGORY,
                strategies=["base64_encoding", "unknown_strategy"],
            )

    def test_compose_with_empty_strategies_raises_value_error(self) -> None:
        engine = MutationEngine(seed=42)
        with pytest.raises(ValueError):
            engine.compose(
                prompt=BENIGN_PROMPT,
                category=TEST_CATEGORY,
                strategies=[],
            )

    def test_compose_with_unknown_strategy_raises_value_error(self) -> None:
        engine = MutationEngine(seed=42)
        with pytest.raises(ValueError):
            engine.compose(
                prompt=BENIGN_PROMPT,
                category=TEST_CATEGORY,
                strategies=["roleplay_framing", "invalid_strategy"],
            )

    @pytest.mark.parametrize("invalid_max_length", [0, -5])
    def test_invalid_max_length_raises_value_error(
        self, invalid_max_length: int
    ) -> None:
        with pytest.raises(ValueError):
            MutationEngine(max_length=invalid_max_length)


class TestDeterministicRepeatability:
    """Verify deterministic behavior across instances and call orders."""

    def test_same_seed_produces_identical_outputs_across_instances(self) -> None:
        engine_a = MutationEngine(seed=123)
        engine_b = MutationEngine(seed=123)

        for strategy in DEFAULT_STRATEGIES:
            result_a = engine_a.mutate(
                prompt=BENIGN_PROMPT,
                category=TEST_CATEGORY,
                strategy=strategy,
                dataset_id=10,
            )
            result_b = engine_b.mutate(
                prompt=BENIGN_PROMPT,
                category=TEST_CATEGORY,
                strategy=strategy,
                dataset_id=10,
            )
            assert result_a == result_b

        suite_a = engine_a.generate_suite(
            prompt=BENIGN_PROMPT,
            category=TEST_CATEGORY,
            dataset_id=10,
        )
        suite_b = engine_b.generate_suite(
            prompt=BENIGN_PROMPT,
            category=TEST_CATEGORY,
            dataset_id=10,
        )
        assert suite_a == suite_b

    def test_mutate_is_order_independent_for_same_seed(self) -> None:
        engine_forward = MutationEngine(seed=123)
        engine_reverse = MutationEngine(seed=123)

        forward_results = {
            strategy: engine_forward.mutate(
                prompt=BENIGN_PROMPT,
                category=TEST_CATEGORY,
                strategy=strategy,
                dataset_id=5,
            )
            for strategy in DEFAULT_STRATEGIES
        }
        reverse_results = {
            strategy: engine_reverse.mutate(
                prompt=BENIGN_PROMPT,
                category=TEST_CATEGORY,
                strategy=strategy,
                dataset_id=5,
            )
            for strategy in reversed(DEFAULT_STRATEGIES)
        }

        for strategy in DEFAULT_STRATEGIES:
            assert forward_results[strategy] == reverse_results[strategy]


class TestGenerateSuiteAndCompose:
    """Verify suite generation and sequential strategy composition."""

    def test_generate_suite_default_strategies_order(self) -> None:
        engine = MutationEngine(seed=42)
        suite = engine.generate_suite(
            prompt=BENIGN_PROMPT,
            category=TEST_CATEGORY,
            strategies=None,
        )

        assert len(suite) == 4
        assert all(isinstance(item, MutatedPrompt) for item in suite)
        assert tuple(item.strategy for item in suite) == DEFAULT_STRATEGIES

    def test_generate_suite_subset_preserves_order(self) -> None:
        engine = MutationEngine(seed=42)
        subset = ["base64_encoding", "delimiter_escape"]
        suite = engine.generate_suite(
            prompt=BENIGN_PROMPT,
            category=TEST_CATEGORY,
            strategies=subset,
        )

        assert len(suite) == 2
        assert [item.strategy for item in suite] == subset

    def test_compose_chains_roleplay_and_delimiter_escape(self) -> None:
        engine = MutationEngine(seed=42)
        first_step = engine.mutate(
            prompt=BENIGN_PROMPT,
            category=TEST_CATEGORY,
            strategy="roleplay_framing",
            dataset_id=9,
        )
        composed = engine.compose(
            prompt=BENIGN_PROMPT,
            category=TEST_CATEGORY,
            strategies=["roleplay_framing", "delimiter_escape"],
            dataset_id=9,
        )

        assert isinstance(composed, MutatedPrompt)
        assert composed.strategy == "roleplay_framing+delimiter_escape"
        assert composed.original_prompt == BENIGN_PROMPT
        assert "</context><system>" in composed.mutated_prompt
        assert BENIGN_PROMPT in composed.mutated_prompt
        assert first_step.mutated_prompt in composed.mutated_prompt

    def test_compose_chains_indirect_injection_and_base64_encoding(self) -> None:
        engine = MutationEngine(seed=42)
        first_step = engine.mutate(
            prompt=BENIGN_PROMPT,
            category=TEST_CATEGORY,
            strategy="indirect_injection",
        )
        composed = engine.compose(
            prompt=BENIGN_PROMPT,
            category=TEST_CATEGORY,
            strategies=["indirect_injection", "base64_encoding"],
        )

        expected_b64 = base64.b64encode(
            first_step.mutated_prompt.encode("utf-8")
        ).decode("ascii")

        assert composed.strategy == "indirect_injection+base64_encoding"
        assert composed.original_prompt == BENIGN_PROMPT
        assert expected_b64 in composed.mutated_prompt


class TestMetadataPreservation:
    """Verify metadata fields remain intact across all public methods."""

    @pytest.mark.parametrize("dataset_id", [7, None])
    def test_metadata_preserved_in_mutate_suite_and_compose(
        self, dataset_id: int | None
    ) -> None:
        seed = 99
        category = "policy_compliance"
        engine = MutationEngine(seed=seed)

        single = engine.mutate(
            prompt=BENIGN_PROMPT,
            category=category,
            strategy="indirect_injection",
            dataset_id=dataset_id,
        )
        assert single.dataset_id == dataset_id
        assert single.category == category
        assert single.strategy == "indirect_injection"
        assert single.original_prompt == BENIGN_PROMPT
        assert single.seed == seed

        suite = engine.generate_suite(
            prompt=BENIGN_PROMPT,
            category=category,
            dataset_id=dataset_id,
        )
        for item, expected_strategy in zip(suite, DEFAULT_STRATEGIES, strict=True):
            assert item.dataset_id == dataset_id
            assert item.category == category
            assert item.strategy == expected_strategy
            assert item.original_prompt == BENIGN_PROMPT
            assert item.seed == seed

        composed = engine.compose(
            prompt=BENIGN_PROMPT,
            category=category,
            strategies=["roleplay_framing", "delimiter_escape"],
            dataset_id=dataset_id,
        )
        assert composed.dataset_id == dataset_id
        assert composed.category == category
        assert composed.strategy == "roleplay_framing+delimiter_escape"
        assert composed.original_prompt == BENIGN_PROMPT
        assert composed.seed == seed


class TestMaxLengthSafetyClamping:
    """Verify mutated output respects max_length while keeping original_prompt intact."""

    def test_max_length_clamps_generate_suite_and_compose_outputs(self) -> None:
        max_length = 120
        long_prompt = (
            "Summarize the security policy for handling API keys and rotation "
            "schedules across staging and production environments. "
        ) * 5
        assert len(long_prompt) > max_length

        engine = MutationEngine(seed=42, max_length=max_length)

        suite = engine.generate_suite(
            prompt=long_prompt,
            category=TEST_CATEGORY,
            dataset_id=11,
        )
        assert len(suite) == len(DEFAULT_STRATEGIES)
        for item in suite:
            assert 0 < len(item.mutated_prompt) <= max_length
            assert item.original_prompt == long_prompt

        composed = engine.compose(
            prompt=long_prompt,
            category=TEST_CATEGORY,
            strategies=["roleplay_framing", "delimiter_escape"],
            dataset_id=11,
        )
        assert 0 < len(composed.mutated_prompt) <= max_length
        assert composed.original_prompt == long_prompt
