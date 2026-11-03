# Company debt model and metric semantics

## Decision table

| Question | Use | Do not use |
|---|---|---|
| How many bond rows belong to a company? | `COUNT(bonds_catalog.isin)` grouped by verified `company_id` | Name-prefix grouping |
| What was originally placed? | `SUM(issue_size * nominal)` as `issued_bond_volume_rub` | Call it current debt |
| What is current bond debt? | Source-provided outstanding principal with `as_of` and provenance | Reusing issue size after amortization |
| What is total debt? | Source-provided total debt snapshot | Summing only catalog bonds |
| What is leverage? | `net_debt / EBITDA` from comparable report fields | Coupon, YTM, issue count, or yield as leverage |
| Is the metric usable? | Check report date, retrieval date, source, coverage, quality | Treat NULL/stale/partial as zero |

## Identity merge gate

Merge two company rows only when an authoritative identifier agrees (normally
INN) or a verified manual override records the legal-entity decision. Keep the
old IDs and historical rows auditable; update references through a controlled,
reviewable mapping rather than destructive deletion.

## Snapshot contract

A debt snapshot must carry:

- company identity;
- report date and retrieval timestamp;
- source and source reference;
- units (rubles unless explicitly stated otherwise);
- total debt, bond debt, bank debt, lease debt, cash, net debt;
- revenue and EBITDA when available;
- coverage and quality status;
- explicit NULL reasons for missing or incomparable values.

A ratio is publishable only when its numerator and denominator come from a
compatible reporting basis. If not, emit the components and mark the ratio
unknown.

## Rebalance overlay boundary

Use debt data in three stages:

1. **Diagnostic:** show company debt concentration and data quality, with no
   effect on the plan.
2. **Advisory:** rank alternatives or flag concentration for user review,
   still without changing canonical validity or creating sell intents.
3. **Hard constraint:** consider only after coverage, historical validation,
   explicit user thresholds, and a canonical engine contract exist. Never
   infer a threshold from generic finance guidance.
