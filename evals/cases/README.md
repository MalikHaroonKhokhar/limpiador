# evals/cases/ — Eval cases (ARCHITECTURE.md §11.3, .clauderules §6)

One case per reasoning behavior under test. Each runs the agent against a
committed fixture in `../fixtures/` with a **known seeded defect**, and asserts
on both **outcome** (the failing test now passes; the symbol is renamed
everywhere; the planted regression was flagged) and **trace** (called
`ast.find_references` before renaming; sensible tool order; under the call
ceiling).

At least one case must carry a **planted red herring** — an innocent
recently-changed file beside the truly-broken one. Real mode, costs credits,
keep the count low.
