from pydantic import BaseModel, Field

from review_agent.models.context import ToolEvidence
from review_agent.models.finding import Finding
from review_agent.models.pr import PRMeta


class ReviewReport(BaseModel):
    pr_meta: PRMeta
    findings: list[Finding] = Field(default_factory=list)
    final_report: str
    tool_evidence: list[ToolEvidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
