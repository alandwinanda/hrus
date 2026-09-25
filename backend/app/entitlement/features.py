from enum import StrEnum


class Feature(StrEnum):
    # Fitur ERP: selalu aktif di semua paket, termasuk Core.
    CORE_HR = "core_hr"
    LEAVE = "leave"
    REPORT_BUILDER = "report_builder"

    # Fitur AI: butuh paket AI Mini, Pro, atau Enterprise.
    AI_ASSISTANT = "ai_assistant"
    AI_POLICY_QA = "ai_policy_qa"
    AI_FORM_VALIDATION = "ai_form_validation"
    AI_REPORTING_AGENT = "ai_reporting_agent"
    AI_TEAM_SUMMARY = "ai_team_summary"


AI_FEATURES: frozenset[Feature] = frozenset(
    {
        Feature.AI_ASSISTANT,
        Feature.AI_POLICY_QA,
        Feature.AI_FORM_VALIDATION,
        Feature.AI_REPORTING_AGENT,
        Feature.AI_TEAM_SUMMARY,
    }
)
