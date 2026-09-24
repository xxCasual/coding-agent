from review_agent.models.finding import Finding

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def dedupe_findings(findings: list[Finding]) -> list[Finding]:
    by_key: dict[tuple[str, int, int, str, str], Finding] = {}
    for finding in findings:
        key = (
            finding.file_path,
            finding.start_line,
            finding.end_line,
            finding.title.lower(),
            finding.severity,
        )
        existing = by_key.get(key)
        if existing is None or finding.confidence > existing.confidence:
            by_key[key] = finding
    return list(by_key.values())


def rank_findings(findings: list[Finding]) -> list[Finding]:
    calibrated = [calibrate_confidence(finding) for finding in findings]
    return sorted(
        calibrated,
        key=lambda item: (SEVERITY_ORDER[item.severity], -item.confidence, item.file_path, item.start_line),
    )


def calibrate_confidence(finding: Finding) -> Finding:
    confidence = finding.confidence
    if finding.is_blocking and finding.severity in {"critical", "high"}:
        confidence = min(confidence + 0.05, 1.0)
    if not finding.evidence.strip():
        confidence = max(confidence - 0.2, 0.0)
    return finding.model_copy(update={"confidence": round(confidence, 3)})
