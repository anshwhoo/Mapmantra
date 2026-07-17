"""
seed.py — Database Seeder for MapMantra
Populates the SQLite database with rich, realistic Indian banking regulatory data,
MAPs, evidence history, active conflicts (Type B/C), and a valid audit chain.
This makes the premium React dashboard visually dense and fully interactive out of the box!
"""

import db
from datetime import datetime, timedelta, timezone

def seed_database():
    db.init_db()
    
    # 1. Clear existing data to ensure a clean slate
    db.execute("DELETE FROM audit_log")
    db.execute("DELETE FROM conflicts")
    db.execute("DELETE FROM evidence")
    db.execute("DELETE FROM maps")
    db.execute("DELETE FROM regulations")
    print("Database cleared.")

    now = datetime.now(timezone.utc)
    ts_now = now.isoformat()
    ts_1d = (now - timedelta(days=1)).isoformat()
    ts_2d = (now - timedelta(days=2)).isoformat()
    ts_5d = (now - timedelta(days=5)).isoformat()

    # 2. Insert Regulations
    reg1_id = db.new_id()
    reg2_id = db.new_id()
    reg3_id = db.new_id()

    db.execute(
        """INSERT INTO regulations (id, title, source, url, raw_text, doc_hash, ingested_at, status) 
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            reg1_id,
            "RBI Master Direction on IT Governance and Cyber Security Risk Management",
            "RBI",
            "https://www.rbi.org.in/scripts/BS_ViewMasDirections.aspx?id=12580",
            "Master Direction - Information Technology Governance, Risk, Controls and Cyber Security. Banks must establish a Board-approved IT Strategy Committee, implement physical HSM key storage inside India, and perform quarterly vulnerability assessments.",
            "md5_rbi_it_governance_2024",
            ts_5d,
            "processed"
        )
    )

    db.execute(
        """INSERT INTO regulations (id, title, source, url, raw_text, doc_hash, ingested_at, status) 
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            reg2_id,
            "SEBI Circular on Cyber Security Framework for Stock Brokers and Depositories",
            "SEBI",
            "https://www.sebi.gov.in/legal/circulars/aug-2023/cybersecurity-framework_75124.html",
            "SEBI guidelines on cybersecurity and cyber resilience. Core requirement mandates cloud-based multi-region key management services (KMS) for active-active redundancy, MFA enforcement for all remote connections, and annual threat modeling.",
            "md5_sebi_cyber_2023",
            ts_2d,
            "processing"
        )
    )

    db.execute(
        """INSERT INTO regulations (id, title, source, url, raw_text, doc_hash, ingested_at, status) 
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            reg3_id,
            "IRDAI Guidelines on Information and Cyber Security for Insurers",
            "IRDAI",
            "https://www.irdai.gov.in/ADMINCMS/cms/Circulars_Layout.aspx?page=insur_cyber",
            "Guidelines on Information and Cyber Security. Requires robust customer data privacy controls, quarterly penetration testing, and prompt 6-hour incident reporting.",
            "md5_irdai_cyber_2024",
            ts_1d,
            "new"
        )
    )
    print("Regulations seeded.")

    # 3. Insert MAPs (extracted compliance requirements)
    maps = []
    
    # IT governance (RBI)
    map1_id = db.new_id() # IT department
    db.execute(
        """INSERT INTO maps (id, regulation_id, action_description, department, priority, deadline_days, deadline_date, compliance_metric, verification_method, status, attack_techniques, conflict_flag, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            map1_id, reg1_id,
            "Formulate a board-approved Cybersecurity and IT Governance Policy, to be reviewed annually by the IT Strategy Committee.",
            "IT", "high", 30, (now + timedelta(days=30)).isoformat(),
            "Signed policy document with Board resolution",
            "Review of Board minutes and signed PDF copy of the Cybersecurity policy",
            "in_progress", "T1078,T1566", 0, ts_5d
        )
    )

    # Key Storage Conflict (RBI Side - IT)
    map2_id = db.new_id()
    db.execute(
        """INSERT INTO maps (id, regulation_id, action_description, department, priority, deadline_days, deadline_date, compliance_metric, verification_method, status, attack_techniques, conflict_flag, conflict_type, conflict_detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            map2_id, reg1_id,
            "Store all database backup encryption keys strictly inside on-premises physical Hardware Security Modules (HSMs) located inside primary and DR data centers in India.",
            "IT", "high", -5, (now - timedelta(days=5)).isoformat(), # Overdue!
            "HSM device certificates and key storage configuration audits",
            "Physical inspection and shell command check of cryptographic key storage locations",
            "conflict", "T1552,T1003", 1, "B",
            "Contradicts SEBI cloud KMS mandate", ts_5d
        )
    )

    # Key Storage Conflict (SEBI Side - IT)
    map3_id = db.new_id()
    db.execute(
        """INSERT INTO maps (id, regulation_id, action_description, department, priority, deadline_days, deadline_date, compliance_metric, verification_method, status, attack_techniques, conflict_flag, conflict_type, conflict_detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            map3_id, reg2_id,
            "Store and manage all cryptographic keys inside a cloud-native KMS with automated multi-region active-active replication across international cloud data centers.",
            "IT", "medium", 15, (now + timedelta(days=15)).isoformat(),
            "Cloud KMS replication configuration audit",
            "Review cloud architecture console and active replication status records",
            "conflict", "T1552", 1, "B",
            "Contradicts RBI physical on-premises HSM mandate", ts_2d
        )
    )

    # MFA Overlap (RBI Side)
    map4_id = db.new_id()
    db.execute(
        """INSERT INTO maps (id, regulation_id, action_description, department, priority, deadline_days, deadline_date, compliance_metric, verification_method, status, attack_techniques, conflict_flag, conflict_type, conflict_detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            map4_id, reg1_id,
            "Implement multi-factor authentication (MFA) for all critical internal administrative systems and vendor connections.",
            "IT", "high", 60, (now + timedelta(days=60)).isoformat(),
            "Active Directory MFA coverage dashboard at 100%",
            "Verify remote logs and verify active group policies requiring authenticators",
            "superseded", "T1078.003", 1, "C",
            "Merged into unified MFA MAP", ts_5d
        )
    )

    # MFA Overlap (SEBI Side)
    map5_id = db.new_id()
    db.execute(
        """INSERT INTO maps (id, regulation_id, action_description, department, priority, deadline_days, deadline_date, compliance_metric, verification_method, status, attack_techniques, conflict_flag, conflict_type, conflict_detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            map5_id, reg2_id,
            "Enforce active 2-factor authentication (2FA) across all administrative access channels, vendor portals, and production databases.",
            "IT", "high", 45, (now + timedelta(days=45)).isoformat(),
            "All administrator credentials mapped to hardware keys or soft tokens",
            "Check production IAM policy checks and MFA status files",
            "superseded", "T1078.003", 1, "C",
            "Merged into unified MFA MAP", ts_2d
        )
    )

    # Merged MFA MAP (Type C Resolution)
    map6_id = db.new_id()
    db.execute(
        """INSERT INTO maps (id, regulation_id, action_description, department, priority, deadline_days, deadline_date, compliance_metric, verification_method, status, attack_techniques, conflict_flag, conflict_detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            map6_id, reg1_id,
            "Implement multi-factor authentication (MFA/2FA) for all critical internal administrative channels, remote vendor access portals, and production database environments.",
            "IT", "high", 45, (now + timedelta(days=45)).isoformat(),
            "Universal MFA coverage across internal, vendor, and production admin interfaces",
            "Verify group policies, IAM authentication rules, and connection audit logs",
            "assigned", "T1078.003,T1556", 0,
            "Merged from Map 4 and Map 5", ts_2d
        )
    )

    # Risk Assessment (SEBI - Risk Department)
    map7_id = db.new_id()
    db.execute(
        """INSERT INTO maps (id, regulation_id, action_description, department, priority, deadline_days, deadline_date, compliance_metric, verification_method, status, attack_techniques, conflict_flag, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            map7_id, reg2_id,
            "Conduct a comprehensive cyber risk assessment and threat modeling exercise, and submit a detailed report to the board quarterly.",
            "Risk", "high", -10, (now - timedelta(days=10)).isoformat(), # Overdue!
            "Quarterly Cyber Risk Assessment and threat catalog files",
            "Review signed report and Board presentation deck",
            "overdue", "T1566,T1190", 0, ts_2d
        )
    )

    # Compliance audit (SEBI - Compliance Department)
    map8_id = db.new_id()
    db.execute(
        """INSERT INTO maps (id, regulation_id, action_description, department, priority, deadline_days, deadline_date, compliance_metric, verification_method, status, attack_techniques, conflict_flag, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            map8_id, reg2_id,
            "Perform annual independent audits of the cybersecurity architecture and maintain a comprehensive audit log.",
            "Compliance", "medium", 120, (now + timedelta(days=120)).isoformat(),
            "Auditor validation certificates",
            "Check auditor letters and review detailed reports",
            "completed", "T1078", 0, ts_2d
        )
    )
    print("MAPs seeded.")

    # 4. Insert Conflicts
    conflict1_id = db.new_id()
    db.execute(
        """INSERT INTO conflicts (id, map_id_1, map_id_2, conflict_type, description, resolution, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            conflict1_id, map2_id, map3_id, "B",
            "Contradiction: RBI Master Direction requires database encryption keys stored in on-premises physical HSM devices in primary/DR centers in India. Conversely, SEBI circular requires storage inside active-active replicated cloud KMS across multi-region international data centers.",
            "pending_legal", ts_2d
        )
    )

    conflict2_id = db.new_id()
    db.execute(
        """INSERT INTO conflicts (id, map_id_1, map_id_2, conflict_type, description, resolution, resolved_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            conflict2_id, map4_id, map5_id, "C",
            "Overlap: Both RBI and SEBI regulations require implementing multi-factor authentication (MFA/2FA) for administrative and vendor access channels.",
            "merged", ts_2d, ts_2d
        )
    )
    print("Conflicts seeded.")

    # 5. Insert Evidence
    evidence1_id = db.new_id()
    db.execute(
        """INSERT INTO evidence (id, map_id, submitted_by, evidence_text, submitted_at, validation_status, validation_verdict, criteria_scores, gap_analysis)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            evidence1_id, map8_id, "Compliance Officer",
            "Seeded evidence: We appointed EY to perform the annual independent audit of our banking cybersecurity framework from March 1st to April 15th. The auditor verified 48 control points. Final validation certificate is attached as ref cyber-audit-2026-v2.pdf.",
            ts_1d, "pass", "satisfied",
            '{"criterion_1": "pass", "criterion_2": "pass", "criterion_3": "pass", "criterion_4": "pass"}',
            "No gaps identified. Independent audit reports high compliance scores."
        )
    )
    print("Evidence history seeded.")

    # 6. Seed Cryptographic Audit Log
    # Genesis entry
    db.insert_audit_entry(
        agent="ingestion",
        action="genesis system start",
        detail={"version": "1.0.0", "env": "local"}
    )
    
    db.insert_audit_entry(
        agent="ingestion",
        action="circular ingested",
        detail={"title": "RBI Master Direction on IT Governance", "source": "RBI"},
        regulation_id=reg1_id
    )

    db.insert_audit_entry(
        agent="extractor",
        action="MAPs extracted",
        detail={"total_extracted": 4},
        regulation_id=reg1_id
    )

    db.insert_audit_entry(
        agent="conflict_detector",
        action="contradiction detected",
        detail={"map_1": map2_id, "map_2": map3_id, "type": "B"},
        map_id=map2_id
    )

    db.insert_audit_entry(
        agent="conflict_detector",
        action="MAPs merged",
        detail={"merged_from": [map4_id, map5_id], "merged_id": map6_id},
        map_id=map6_id
    )

    db.insert_audit_entry(
        agent="validator",
        action="evidence validated",
        detail={"map_id": map8_id, "verdict": "satisfied"},
        map_id=map8_id
    )

    db.insert_audit_entry(
        agent="risk_engine",
        action="risk scores computed",
        detail={"global_score": 38, "overdue_count": 2}
    )
    
    print("Audit log chain successfully seeded and verified.")
    print("Seeding process completed successfully!")

if __name__ == "__main__":
    seed_database()
