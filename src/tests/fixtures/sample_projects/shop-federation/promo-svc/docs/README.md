# promo-svc

Human-authored documentation for the sample promo service.

This tree was added so the dashboard's **Docs** view had something real to list, and that view is
gone — the dashboard is an operator console now. The page stays because its second role outlived
the first: it is the sample federation's only markdown, so it is what `search(scope="docs")`
retrieves over this project. Delete it and the docs scope is exercised against a corpus with
nothing in it.

## Rules

`promo/rules.go` holds the discount rules; `promo/rules_engine.go` evaluates them against a cart.

## Fulfillment

`promo/fulfillment.go` applies an evaluated promotion to an order.
