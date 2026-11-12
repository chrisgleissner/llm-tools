# Security requirements crosswalk

This is a traceability aid for the follow-up requirements in [SPEC_STATUS.md](SPEC_STATUS.md), not a security assessment or certification. A mapped clause is a useful verification reference; it does not mean the requirement is fully covered, the product conforms to that standard, or the mapped clause is the only relevant one. This document maps clauses only. The status and evidence of each ID are kept in the register alone, so they cannot disagree with it.

## Sources and ID conventions

* [OWASP ASVS 5.0.0 taxonomy](https://cornucopia.owasp.org/taxonomy/asvs-5.0/) is the official OWASP-hosted section index. References use its two-digit chapter and section numbering (for example, `ASVS 04.02`, HTTP Message Structure Validation). Use the linked section for the exact individual verification requirements; section mapping is intentionally not a claim that every requirement in a section applies.
* [OWASP API Security Top 10 2023](https://api-security.owasp.org/editions/2023/en/0x11-t10/) uses risk IDs `API1:2023` through `API10:2023`. It is an awareness/risk taxonomy, not a clause-level control standard.
* [NIST SP 800-63B-4 final](https://csrc.nist.gov/pubs/sp/800/63/b/4/final) (July 2025) is linked to its [normative HTML](https://pages.nist.gov/800-63-4/sp800-63b.html). NIST references below are exact section IDs. The clauses are relevant where this application acts as a verifier/authenticator service; they do not automatically apply to every deployment or establish NIST conformance.

## REST API follow-up

| ID | Relevant clauses |
|---|---|
| API-001 | ASVS 04.01, 04.02; API1:2023, API3:2023 |
| API-002 | ASVS 04.01, 08.02, 08.03; API5:2023 |
| API-003 | ASVS 02.04, 04.02, 07.02; API4:2023 |
| API-004 | ASVS 04.02, 16.05; API3:2023, API4:2023 |
| API-005 | ASVS 08.02, 08.03, 10.03; API1:2023, API3:2023, API5:2023 |
| API-006 | ASVS 06.04, 08.03, 16.03; API2:2023, API5:2023 |
| API-007 | ASVS 03.03, 03.04, 03.05, 03.07, 08.02, 12.01, 16.03; API4:2023, API6:2023, API8:2023 |
| API-008 | ASVS 04.04, 08.02, 08.03; API1:2023, API3:2023, API5:2023 |
| API-009 | ASVS 04.01, 04.02; API9:2023 |
| API-010 | ASVS 03.02, 04.01, 04.02; API8:2023, API9:2023 |
| API-011 | ASVS 04.01, 04.02; API9:2023 |
| API-012 | ASVS 04.01, 04.02; API1:2023, API3:2023, API4:2023, API8:2023 |
| API-013 | ASVS 04.01, 08.02, 08.03; API1:2023, API5:2023 |
| API-014 | ASVS 04.01, 04.02; API9:2023 |
| API-015 | ASVS 04.01; API9:2023 |
| API-016 | ASVS 06.07, 12.03; API2:2023, API8:2023 |

## Security hardening

| ID | Relevant clauses |
|---|---|
| SEC2-001 | ASVS 12.01, 12.02; API8:2023 |
| SEC2-002 | ASVS 03.04, 03.05, 03.07; API8:2023 |
| SEC2-003 | ASVS 02.02, 04.02; API4:2023, API8:2023 |
| SEC2-004 | ASVS 03.05, 03.07; API8:2023 |
| SEC2-005 | ASVS 03.05, 04.01, 04.02; API8:2023 |
| SEC2-006 | ASVS 06.04, 06.05, 06.07; NIST 3.2.5, 4.1, 4.2; API2:2023 |
| SEC2-007 | ASVS 06.02, 06.03; NIST 3.1.1.2; API2:2023 |
| SEC2-008 | ASVS 03.03, 03.04, 07.02, 07.04, 07.05; API2:2023 |
| SEC2-009 | ASVS 07.02, 08.02, 09.01, 10.03; NIST 3.2.7; API2:2023, API1:2023 |
| SEC2-010 | ASVS 02.03, 02.04, 06.03, 08.02; NIST 3.2.2; API4:2023, API6:2023 |
| SEC2-011 | ASVS 08.02, 08.03; API1:2023, API3:2023, API5:2023 |
| SEC2-012 | ASVS 04.02, 16.05; API3:2023, API4:2023 |
| SEC2-013 | ASVS 16.02, 16.03, 16.04; API8:2023 |
| SEC2-014 | ASVS 06.04, 16.02, 16.03; NIST 4.1, 4.2; API2:2023 |
| SEC2-015 | ASVS 15.02; API8:2023, API9:2023 |
| SEC2-016 | ASVS 15.01, 04.01, 04.02; API1:2023–API10:2023 |
| SEC2-017 | ASVS 12.01, 12.02, 12.03, 04.01; API8:2023, API9:2023 |
| SEC2-018 | ASVS 13.01, 13.02, 13.04; API8:2023 |
| SEC2-019 | ASVS 06.07, 08.02, 09.01, 10.03, 11.06; NIST 3.2.5, 3.2.7, 4.1; API2:2023 |
| SEC2-020 | ASVS 06.07, 12.03, 13.03; NIST 3.2.5, 4.1; API2:2023, API8:2023 |

## Passkeys

| ID | Relevant clauses |
|---|---|
| PK-001 | ASVS 06.07, 12.01; NIST 3.2.5, 4.1 |
| PK-002 | ASVS 06.07, 06.04, 08.02; NIST 3.2.5, 4.1.2 |
| PK-003 | ASVS 06.07, 06.04, 11.04, 11.06; NIST 3.1.7, 4.1 |
| PK-004 | ASVS 06.03, 06.05, 06.07; NIST 2, 3.1.7, 3.2.5 |
| PK-005 | ASVS 06.04, 06.07; NIST 4.1, 4.1.2 |
| PK-006 | ASVS 06.04, 06.05, 07.04; NIST 4.1, 4.2 |
| PK-007 | ASVS 06.04; NIST 4.2, 4.2.1.1 |
| PK-008 | ASVS 07.06, 08.03; NIST 2, 3.2.5 |
| PK-009 | ASVS 06.05, 08.03; NIST 2, 3.2.5 |
| PK-010 | ASVS 06.01, 06.03, 06.07; NIST 3.1.7, 3.2.5 |
| PK-011 | ASVS 06.07, 15.01; NIST 3.1.7, 3.2.5, 3.2.7, 4.1.2 |
| PK-012 | ASVS 06.07, 15.01; NIST 3.1.7, 3.2.5 |

## Mapping limits

ASVS references are exact chapter/section IDs from the v5.0 taxonomy. They identify the closest sections, not a claim that every individual requirement in each section is applicable. The API Top 10 IDs identify associated risk areas, not pass/fail controls. NIST 800-63B-4 applies to its stated identity and authenticator roles; several product-specific items (browser UX, deployment profiles, scanning methods and device testing) have no direct normative clause and are mapped only to adjacent verification topics. Reassess applicability against the final design and the exact requirements before using this crosswalk as a verification plan.
