from enum import StrEnum


class Feature(StrEnum):
    # Fitur ERP: selalu aktif untuk semua tenant.
    CORE_HR = "core_hr"
    LEAVE = "leave"
    REPORT_BUILDER = "report_builder"

    # Fitur AI: aktif kalau tenant menyalakannya di setting AI dan API key-nya siap (ADR 011).
    AI_ASSISTANT = "ai_assistant"
    AI_POLICY_QA = "ai_policy_qa"
    AI_FORM_VALIDATION = "ai_form_validation"
    AI_REPORTING_AGENT = "ai_reporting_agent"
    AI_TEAM_SUMMARY = "ai_team_summary"


class AiFeature(StrEnum):
    """Subset Feature yang bisa di-toggle HR di setting AI."""

    AI_ASSISTANT = Feature.AI_ASSISTANT.value
    AI_POLICY_QA = Feature.AI_POLICY_QA.value
    AI_FORM_VALIDATION = Feature.AI_FORM_VALIDATION.value
    AI_REPORTING_AGENT = Feature.AI_REPORTING_AGENT.value
    AI_TEAM_SUMMARY = Feature.AI_TEAM_SUMMARY.value


AI_FEATURES: frozenset[Feature] = frozenset(Feature(f.value) for f in AiFeature)
