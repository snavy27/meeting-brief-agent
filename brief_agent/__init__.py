"""Meeting-brief agent — drafts a one-page brief from the Notion CRM."""

from .agent import DEFAULT_MODEL, BriefResult, draft_brief

__all__ = ["draft_brief", "BriefResult", "DEFAULT_MODEL"]
