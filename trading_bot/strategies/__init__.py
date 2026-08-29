"""Strategy sleeves.

Each sleeve is a self-contained way of turning bars into target weights. They
share the data layer and nothing else -- no shared state, no shared policy, and
deliberately no shared signal code. A sleeve that borrowed sleeve A's ranking
would inherit sleeve A's correlations, which is the one thing it must not do.
"""
