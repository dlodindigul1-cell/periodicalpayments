# Q2 / Partial Invoice & Grouped Quarter update

## Implemented
- Removed the `magazine + quarter` single-invoice restriction. Multiple invoice rows can now exist for one magazine/quarter.
- Invoice entry asks whether the invoice covers the full quarter.
- For partial invoices, July/August/September (or the corresponding quarter months) can be selected individually.
- QTR Issues is recalculated from the selected coverage.
- Q1/Q2/Q3/Q4 can be grouped into one invoice, intended for publishers who send several completed quarters together at Q4.
- Payment selection is invoice-based, so separate invoices for the same magazine/quarter can be paid independently.
- Partial/grouped coverage is shown in the payment screen; partial coverage is highlighted in red and can be clicked to view the exact months/quarters.
- The quarter non-supply deduction is applied only once to the first invoice recorded for that magazine/quarter, preventing duplicate deduction across split invoices.
- Payment voucher/advice data carries grouped quarter / partial-month information.
- Existing databases are migrated automatically on first authenticated request: new coverage columns are added and the old unique `(magazine, quarter)` constraint is removed.

## Important
- Grouped-quarter invoices are treated as full quarters; partial-month selection is disabled when grouping quarters.
- Existing data is preserved. The migration does not delete existing payment rows.
