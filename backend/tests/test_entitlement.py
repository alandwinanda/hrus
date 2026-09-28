"""require_feature dan status AI per tenant dites di test_ai_settings.py (butuh DB)."""

from app.entitlement.features import AI_FEATURES, AiFeature, Feature


def test_ai_features_are_all_prefixed() -> None:
    assert all(f.value.startswith("ai_") for f in AI_FEATURES)
    assert not any(f.value.startswith("ai_") for f in set(Feature) - AI_FEATURES)


def test_every_ai_feature_can_be_toggled() -> None:
    assert {f.value for f in AiFeature} == {f.value for f in AI_FEATURES}
