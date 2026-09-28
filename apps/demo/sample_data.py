"""Fictional sample content for the gated demo mode.

Everything here describes a made-up laboratory ("Riverbend Clinical Laboratory").
Requirement wording is paraphrased; do not paste licensed checklist text or any
real laboratory records into this file.
"""

import copy

LAB_NAME = "Riverbend Clinical Laboratory"
DEMO_SHEET = "GEN"
DEMO_SHEETS = ["GEN"]

DOCUMENTS = {
    "qm-001": {
        "title": "QM-001 Quality Manual",
        "folder": "Quality System",
        "effective": "2026-01-15",
        "body": [
            "Purpose: This manual describes the quality management system (QMS) of Riverbend "
            "Clinical Laboratory, including its structure, responsibilities, and the policies "
            "that govern pre-examination, examination, and post-examination processes.",
            "Scope: The QMS applies to every section of the laboratory (chemistry, hematology, "
            "microbiology, and specimen processing) and to all users of laboratory services, "
            "including clinicians, patients, and referral clients.",
            "Quality policy: The laboratory director is accountable for the QMS. The quality "
            "manager reviews quality indicators monthly and reports results at the quarterly "
            "management review.",
        ],
    },
    "svc-003": {
        "title": "SVC-003 Scope of Service",
        "folder": "Quality System",
        "effective": "2025-11-02",
        "body": [
            "Services offered: Routine chemistry, complete blood count, coagulation, urinalysis, "
            "and bacterial culture. Esoteric testing is sent to approved referral laboratories.",
            "Hours of operation: Specimen receiving is staffed Monday through Saturday, "
            "6:00 AM to 10:00 PM.",
            "Turnaround time targets are maintained by each section supervisor and are not "
            "included in this document.",
        ],
    },
    "dc-002": {
        "title": "DC-002 Document Control Procedure",
        "folder": "Quality System",
        "effective": "2026-02-01",
        "body": [
            "All policies, procedures, and forms are assigned a unique number and revision, "
            "approved by the laboratory director before use, and reviewed every two years.",
            "Superseded documents are removed from points of use, marked OBSOLETE, and retained "
            "in the document archive for at least two years.",
            "The master document index is maintained electronically and lists the current "
            "revision, approval date, and next review date for each controlled document.",
        ],
    },
    "nce-010": {
        "title": "NCE-010 Nonconforming Event Management",
        "folder": "Nonconforming Events",
        "effective": "2025-09-10",
        "body": [
            "Any staff member who identifies a nonconforming event (NCE) records it in the NCE "
            "log (form NCE-010.1) before the end of the shift.",
            "Each event is classified by severity. Events that caused or could have caused "
            "patient harm are escalated to the laboratory director within 24 hours.",
            "The quality manager trends NCEs monthly and presents the summary at management review.",
        ],
    },
    "nce-011": {
        "title": "NCE-011 Root Cause Analysis Procedure",
        "folder": "Nonconforming Events",
        "effective": "2025-09-10",
        "body": [
            "A root cause analysis (RCA) is required for sentinel events and for any NCE rated "
            "high risk. Lower-risk events receive a documented scope-of-investigation decision.",
            "Approved RCA methods include the 5 Whys and fishbone (Ishikawa) diagrams. Results are "
            "recorded on the corrective action report form (CA-FORM-01).",
        ],
    },
    "ca-2026-014": {
        "title": "CA-2026-014 Corrective Action Report: Mislabeled Specimen",
        "folder": "Nonconforming Events/Corrective Actions",
        "effective": "2026-04-22",
        "body": [
            "Event: A specimen was received with a label that did not match the requisition. "
            "The discrepancy was caught at accessioning; no result was released.",
            "Root cause (5 Whys): The outpatient draw station was printing labels in batches "
            "ahead of patient arrival.",
            "Action: Batch label printing discontinued; draw staff retrained on 2026-05-03. "
            "Effectiveness check scheduled for 2026-08-01.",
        ],
    },
    "ret-050": {
        "title": "RET-050 Record and Specimen Retention Policy",
        "folder": "Records",
        "effective": "2024-06-30",
        "body": [
            "Patient test reports are retained for 10 years. Requisitions are retained for 2 years.",
            "Quality control records and instrument maintenance records are retained for 1 year.",
            "Records may be stored electronically provided they remain retrievable for the full "
            "retention period.",
        ],
    },
    "per-020": {
        "title": "PER-020 Training and Competency Assessment",
        "folder": "Personnel",
        "effective": "2025-12-01",
        "body": [
            "Testing personnel complete documented training before performing patient testing.",
            "Competency is assessed semiannually during the first year of testing and annually "
            "thereafter, using direct observation, record review, blind samples, and "
            "problem-solving exercises.",
        ],
    },
    "per-021": {
        "title": "PER-021 Competency Assessment Tracker 2026",
        "folder": "Personnel",
        "effective": "2026-07-31",
        "body": [
            "Staff listed: 9 testing personnel.",
            "Annual assessments completed and signed: 7 of 9.",
            "Two technologists hired in January 2026 have training records on file, but their "
            "six-month competency assessments (due July 2026) are not recorded.",
        ],
    },
}


def _doc_link(slug):
    return f"/demo/document/{slug}"


def _evidence(slug, code, snippet):
    doc = DOCUMENTS[slug]
    return {
        "document_name": doc["title"] + ".pdf",
        "title": doc["title"],
        "document_uri": f"demo://{LAB_NAME}/{doc['folder']}/{doc['title']}.pdf",
        "id": f"demo-{slug}",
        "link": _doc_link(slug),
        "requirement_query": code,
        "snippet": snippet,
    }


# One entry per checklist requirement shown on the demo Gap Analysis page.
REQUIREMENTS = [
    {
        "code": "REQ.13806",
        "subject": "Quality Management System Document",
        "label": "The laboratory maintains a document describing its overall quality management system.",
        "evidence_of_compliance": "Current quality manual or equivalent QMS document.",
        "status": "Compliant",
        "summary": (
            "Riverbend's Quality Manual QM-001 describes the QMS structure, the director's "
            "accountability, and monthly quality indicator review [1]."
        ),
        "evidence": [("qm-001", "This manual describes the quality management system (QMS) of Riverbend Clinical Laboratory...")],
        "gap_analysis": (
            "Status: Compliant. QM-001 Quality Manual (effective 2026-01-15) documents the QMS "
            "structure, responsibilities, and review cycle."
        ),
        "corrective_actions": ["Continue current practice. Keep QM-001 in the inspection packet."],
    },
    {
        "code": "REQ.13820",
        "subject": "Description of Services",
        "label": (
            "A document describes the services the laboratory offers, including tests offered, "
            "hours of operation, and turnaround times."
        ),
        "evidence_of_compliance": "Scope of service document or test menu covering all three elements.",
        "status": "Partial",
        "summary": (
            "SVC-003 Scope of Service lists tests offered and specimen receiving hours [1]. It "
            "states that turnaround time targets are kept by section supervisors and are not "
            "included in the document [1]."
        ),
        "evidence": [("svc-003", "Turnaround time targets are maintained by each section supervisor and are not included in this document.")],
        "gap_analysis": (
            "Status: Partial. SVC-003 covers tests offered and hours of operation, but turnaround "
            "times are missing from the document."
        ),
        "corrective_actions": [
            "1) Add turnaround time targets for each test category to SVC-003.",
            "2) Re-approve SVC-003 under DC-002 and communicate the change to clients.",
        ],
    },
    {
        "code": "REQ.20100",
        "subject": "QMS Scope",
        "label": "The QMS covers every area of the laboratory and all users of its services.",
        "evidence_of_compliance": "QMS document stating scope across sections and service users.",
        "status": "Compliant",
        "summary": (
            "QM-001 states the QMS applies to chemistry, hematology, microbiology, and specimen "
            "processing, and to clinicians, patients, and referral clients [1]. SVC-003 describes "
            "referral testing [2]."
        ),
        "evidence": [
            ("qm-001", "The QMS applies to every section of the laboratory ... and to all users of laboratory services."),
            ("svc-003", "Esoteric testing is sent to approved referral laboratories."),
        ],
        "gap_analysis": "Status: Compliant. QM-001 explicitly defines QMS scope across all sections and service users.",
        "corrective_actions": ["Continue current practice."],
    },
    {
        "code": "REQ.20208",
        "subject": "Nonconforming Event Recording",
        "label": "The QMS includes a process to identify and record nonconforming events.",
        "evidence_of_compliance": "Written NCE procedure and event log.",
        "status": "Compliant",
        "summary": (
            "NCE-010 requires staff to record nonconforming events in the NCE log before the end "
            "of the shift, classify severity, and escalate potential harm within 24 hours [1]."
        ),
        "evidence": [("nce-010", "Any staff member who identifies a nonconforming event (NCE) records it in the NCE log...")],
        "gap_analysis": "Status: Compliant. NCE-010 defines identification, recording, and escalation of nonconforming events.",
        "corrective_actions": ["Continue monthly NCE trending per NCE-010."],
    },
    {
        "code": "REQ.20310",
        "subject": "Investigation of Nonconforming Events",
        "label": (
            "Serious events receive a root cause analysis, and the scope of investigation is "
            "defined for lower-risk events."
        ),
        "evidence_of_compliance": "RCA procedure and records of completed investigations.",
        "status": "Partial",
        "summary": (
            "NCE-011 requires RCA for sentinel and high-risk events [1]. Corrective action report "
            "CA-2026-014 documents a 5 Whys analysis [2]; its effectiveness check due 2026-08-01 "
            "has not been recorded [2]."
        ),
        "evidence": [
            ("nce-011", "A root cause analysis (RCA) is required for sentinel events and for any NCE rated high risk."),
            ("ca-2026-014", "Effectiveness check scheduled for 2026-08-01."),
        ],
        "gap_analysis": (
            "Status: Partial. The RCA procedure and a completed investigation are on file, but the "
            "scheduled effectiveness check for CA-2026-014 is not documented."
        ),
        "corrective_actions": [
            "1) Complete and record the CA-2026-014 effectiveness check.",
            "2) Add an effectiveness-check due date column to the NCE log so overdue checks are visible.",
        ],
    },
    {
        "code": "REQ.20375",
        "subject": "Document Control",
        "label": (
            "Policies and procedures are controlled: approved before use, reviewed on schedule, "
            "and obsolete versions removed."
        ),
        "evidence_of_compliance": "Document control procedure and master document index.",
        "status": "Compliant",
        "summary": (
            "DC-002 requires director approval, biennial review, removal of obsolete documents, "
            "and an electronic master index [1]."
        ),
        "evidence": [("dc-002", "All policies, procedures, and forms are assigned a unique number and revision, approved by the laboratory director...")],
        "gap_analysis": "Status: Compliant. DC-002 covers approval, review, and retirement of controlled documents.",
        "corrective_actions": ["Continue current practice."],
    },
    {
        "code": "REQ.20377",
        "subject": "Record Retention",
        "label": "Records are retained for at least the minimum periods required by the checklist.",
        "evidence_of_compliance": "Retention policy consistent with required retention periods.",
        "status": "Non-Compliant",
        "summary": (
            "RET-050 sets a 1-year retention period for quality control and instrument "
            "maintenance records [1], shorter than the 2-year minimum."
        ),
        "evidence": [("ret-050", "Quality control records and instrument maintenance records are retained for 1 year.")],
        "gap_analysis": (
            "Status: Non-Compliant. RET-050 (last revised 2024-06-30) keeps QC and maintenance "
            "records for 1 year; the required minimum is 2 years."
        ),
        "corrective_actions": [
            "1) Revise RET-050 to retain QC and maintenance records for at least 2 years.",
            "2) Confirm no QC records newer than 2 years have already been discarded.",
            "3) Re-approve RET-050 and train records staff on the change.",
        ],
    },
    {
        "code": "REQ.55500",
        "subject": "Competency Assessment",
        "label": (
            "Competency of testing personnel is assessed semiannually in the first year and "
            "annually thereafter."
        ),
        "evidence_of_compliance": "Competency procedure and completed, signed assessment records.",
        "status": "Non-Compliant",
        "summary": (
            "PER-020 defines semiannual first-year and annual competency assessments [1]. The "
            "2026 tracker shows six-month assessments for two technologists hired in January "
            "2026 are not recorded [2]."
        ),
        "evidence": [
            ("per-020", "Competency is assessed semiannually during the first year of testing and annually thereafter..."),
            ("per-021", "Two technologists hired in January 2026 ... six-month competency assessments (due July 2026) are not recorded."),
        ],
        "gap_analysis": (
            "Status: Non-Compliant. The procedure is adequate, but two first-year technologists are "
            "missing their required six-month competency assessments."
        ),
        "corrective_actions": [
            "1) Complete and sign six-month competency assessments for both technologists.",
            "2) Add due-date reminders to PER-021 so first-year assessments are not missed.",
        ],
    },
]

_REQ_BY_CODE = {r["code"]: r for r in REQUIREMENTS}

_CHECK_STATUS = {
    "Compliant": "COMPLIANT",
    "Partial": "PARTIAL",
    "Non-Compliant": "NON-COMPLIANT",
    "N/A": "NOT APPLICABLE",
}


def requirement(code):
    return _REQ_BY_CODE.get((code or "").strip())


def evidence_for(req):
    return [_evidence(slug, req["code"], snippet) for slug, snippet in req["evidence"]]


def requirements_payload(sheet):
    """Response body for cus_gen_requirements."""
    rows = []
    if sheet == DEMO_SHEET:
        for idx, req in enumerate(REQUIREMENTS):
            rows.append({
                "row_index": idx + 2,
                "code": req["code"],
                "status": "",
                "label": req["label"],
                "subject": req["subject"],
                "revision": "",
                "policy_procedure": "",
                "note": "",
                "evidence_of_compliance": req["evidence_of_compliance"],
                "ai_evidence": "",
                "ai_evidence_docs": "",
                "onsite_note": "",
                "compliance_comments": "",
                "gap_analysis": "",
                "corrective_actions": "",
                "remediation": "",
                "is_ai_search": True,
            })
    return {
        "success": True,
        "requirements": rows,
        "headers": ["Requirement ID", "Subject", "Requirement", "Evidence of Compliance", "Status"],
        "source": "demo",
        "source_info": f"{LAB_NAME} (sample data)",
        "column_indices": {},
    }


def search_payload(req):
    """Response body for cap_ai_search for one known requirement."""
    items = evidence_for(req)
    status = _CHECK_STATUS[req["status"]]
    missing = [] if status == "COMPLIANT" else ["records"]
    return {
        "success": True,
        "results": items,
        "summary": f"**{req['code']}**: {req['summary']}",
        "citations": [],
        "result_count": len(items),
        "inferred_status": status,
        "session": f"demo-session-{req['code']}",
        "related_questions": [],
        "requirement_assessments": [{
            "requirement": req["code"],
            "status": status,
            "missing_evidence": missing,
            "evidence_count": len(items),
            "vertex_evidence_count": len(items),
            "lims_evidence_count": 0,
        }],
        "missing_evidence_categories": missing,
        "max_ai_search_evidence": None,
    }


def free_text_search_payload(query):
    """Keyword match over the fictional documents for the free-text search box."""
    words = {w for w in (query or "").lower().split() if len(w) > 3}
    scored = []
    for slug, doc in DOCUMENTS.items():
        text = (doc["title"] + " " + " ".join(doc["body"])).lower()
        score = sum(1 for w in words if w in text)
        if score:
            scored.append((score, slug))
    scored.sort(reverse=True)
    hits = [slug for _, slug in scored[:3]] or ["qm-001"]

    results = [_evidence(slug, "", DOCUMENTS[slug]["body"][0]) for slug in hits]
    paragraphs = [
        f"In the sample document set for {LAB_NAME}, these documents are the closest match "
        "to your question."
    ]
    for i, slug in enumerate(hits, start=1):
        doc = DOCUMENTS[slug]
        paragraphs.append(f"{doc['title']}: {doc['body'][0]} [{i}]")
    paragraphs.append(
        "(Demo mode: answers come from a fixed set of fictional documents. In the full product "
        "the AI searches your laboratory's own policies and records.)"
    )
    return {
        "success": True,
        "results": results,
        "summary": "\n\n".join(paragraphs),
        "citations": [],
        "result_count": len(results),
        "inferred_status": None,
        "session": "",
        "related_questions": [],
        "requirement_assessments": [],
        "missing_evidence_categories": [],
    }


def gap_assessment_payload(req):
    return {
        "success": True,
        "requirement_id": req["code"],
        "assessment": {
            "requirement_id": req["code"],
            "status": req["status"],
            "gap_analysis": req["gap_analysis"],
            "corrective_actions": list(req["corrective_actions"]),
            "cited_documents": [DOCUMENTS[slug]["title"] + ".pdf" for slug, _ in req["evidence"]],
        },
    }


def _check(req):
    items = evidence_for(req)
    return {
        "checklist_item": req["code"],
        "requirement": req["label"],
        "subject": req["subject"],
        "note": "",
        "evidence_of_compliance": req["evidence_of_compliance"],
        "remediation": "",
        "action_completion": "",
        "status": _CHECK_STATUS[req["status"]],
        "gap_analysis": req["gap_analysis"],
        "corrective_actions": list(req["corrective_actions"]),
        "cited_documents": [DOCUMENTS[slug]["title"] + ".pdf" for slug, _ in req["evidence"]],
        "ai_evidence_summary": f"**{req['code']}**: {req['summary']}",
        "policy_evidence": items,
        "results": items,
        "findings": [f"Evidence count: {len(items)} (AI Search: {len(items)}, LIMS: 0)"],
    }


def _report(checks, report_date):
    counts = {"COMPLIANT": 0, "PARTIAL": 0, "NON-COMPLIANT": 0, "NOT APPLICABLE": 0}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    total = len(checks)
    return {
        "report_date": report_date,
        "checks": checks,
        "total_checks": total,
        "compliant": counts["COMPLIANT"],
        "partial": counts["PARTIAL"],
        "warnings": counts["PARTIAL"],
        "non_compliant": counts["NON-COMPLIANT"],
        "not_applicable": counts["NOT APPLICABLE"],
        "not_checked_report": 0,
        "compliance_rate": round(counts["COMPLIANT"] / total * 100) if total else 0,
        "followup_sessions": {},
        "followup_session": "",
        "summary": "\n\n".join(c["ai_evidence_summary"] for c in checks),
        "citations": [],
        "query": "",
        "sheet": DEMO_SHEET,
    }


_DEMO_OWNER = "demo@xyzsure.com"


def _entry(entry_id, name, date, report, report_type=None):
    entry = {
        "id": entry_id,
        "name": name,
        "date": date,
        "user": _DEMO_OWNER,
        "user_id": "demo",
        "shared": True,
        "report": report,
    }
    if report_type:
        entry["report_type"] = report_type
        entry["report"]["report_type"] = report_type
    return entry


def gap_analysis_reports():
    full = _report([_check(r) for r in REQUIREMENTS], "9/14/2026, 9:30:00 AM")
    return copy.deepcopy([
        _entry(
            900000000001,
            f"GEN full checklist - {LAB_NAME} (sample)",
            "2026-09-14T13:30:00+00:00",
            full,
            "gap_analysis",
        ),
    ])


def search_reports():
    subset = [_check(_REQ_BY_CODE[c]) for c in ("REQ.13806", "REQ.13820", "REQ.20208")]
    return copy.deepcopy([
        _entry(
            900000000002,
            f"REQ.13806, REQ.13820, REQ.20208 - Evidence search (sample)",
            "2026-09-10T15:05:00+00:00",
            _report(subset, "9/10/2026, 11:05:00 AM"),
        ),
    ])


def compliance_sheet_payload(sheet):
    headers = ["Date", "Requirement (ID)", "Subject", "Status", "Gap Analysis", "Corrective Actions", "Cited Documents"]
    rows = []
    if sheet == DEMO_SHEET:
        for req in REQUIREMENTS:
            rows.append([
                "2026-09-14",
                req["code"],
                req["subject"],
                _CHECK_STATUS[req["status"]],
                req["gap_analysis"],
                "\n".join(req["corrective_actions"]),
                ", ".join(DOCUMENTS[slug]["title"] for slug, _ in req["evidence"]),
            ])
    return {"success": True, "headers": headers, "rows": rows, "sheet": sheet, "header_row_index": 0}


def drive_files_payload():
    files = []
    for slug, doc in DOCUMENTS.items():
        files.append({
            "id": f"demo-{slug}",
            "name": doc["title"] + ".pdf",
            "mimeType": "application/pdf",
            "link": _doc_link(slug),
            "createdTime": doc["effective"] + "T12:00:00Z",
            "modifiedTime": doc["effective"] + "T12:00:00Z",
            "size": "184320",
            "folder_path": doc["folder"],
        })
    return {"success": True, "files": files, "count": len(files)}
