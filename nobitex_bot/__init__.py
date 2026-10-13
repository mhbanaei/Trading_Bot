"""Nobitex Smart Trading Bot — contract-compliant implementation.

Implements the Data Contract & Schema Specification (docs/data-contract.md):
snapshot -> validation -> normalization -> features -> quality gate ->
quant scanner -> AI context -> AI decision -> AI output validation ->
risk engine -> revalidation -> execution -> order/fill ->
reconciliation -> PnL -> performance -> historical summary.

Safety first (contract SS79, SS101, SS102):
    IF UNCERTAIN -> NO TRADE
"""
__version__ = "1.0.0"
