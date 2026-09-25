from app.core.config import Settings
from app.entitlement.features import AI_FEATURES, Feature


def is_feature_enabled(feature: Feature, settings: Settings) -> bool:
    """Stub: fitur ERP selalu aktif, fitur AI hanya mengikuti AI_ENABLED.

    TODO: cek tenant_subscription, feature_flag, dan sisa kredit AI per tenant.
    """
    if feature in AI_FEATURES:
        return settings.ai_enabled
    return True
