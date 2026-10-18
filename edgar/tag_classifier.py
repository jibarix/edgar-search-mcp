"""
XBRL tag classifier using SEC Financial Statement Data Set mapping.

Classification priority:
1. Builtin overrides (hand-curated display names for ~100 common tags)
2. SEC Financial Statement Data Set mapping (3,500+ tags from real filings)

Tags not found in either source are skipped (typically footnote disclosures).
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

# Load SEC Financial Statement Data Set mapping. It sits inside the package
# (and is declared as package data) so an installed copy finds it too.
_SEC_MAPPING = {}
_SEC_MAPPING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "sec_tag_mapping.json")
if os.path.exists(_SEC_MAPPING_FILE):
    try:
        with open(_SEC_MAPPING_FILE, "r", encoding="utf-8") as _f:
            _SEC_MAPPING = json.load(_f)
        logger.info(f"Loaded {len(_SEC_MAPPING)} tags from SEC mapping")
    except (json.JSONDecodeError, IOError):
        pass

# Builtin overrides for the most common tags.
# These provide clean display names and sort order for key line items.
_BUILTIN_TAGS = {
    # ── Balance Sheet: Assets ──
    # order: global sort across all BS categories (Assets 0-199, Liabilities 200-399, Equity 400+)
    "AssetsCurrent":                                 {"statement": "BS", "category": "Assets",      "display_name": "Total current assets",            "order": 90,  "indent": 0, "is_subtotal": True, "section": "Current assets:"},
    "CashAndCashEquivalentsAtCarryingValue":         {"statement": "BS", "category": "Assets",      "display_name": "Cash and cash equivalents",       "order": 10,  "indent": 1},
    "CashAndCashEquivalents":                        {"statement": "BS", "category": "Assets",      "display_name": "Cash and cash equivalents",       "order": 10,  "indent": 1},
    "ShortTermInvestments":                          {"statement": "BS", "category": "Assets",      "display_name": "Short-term investments",          "order": 20,  "indent": 1},
    "MarketableSecuritiesCurrent":                   {"statement": "BS", "category": "Assets",      "display_name": "Marketable securities",           "order": 21,  "indent": 1},
    "AccountsReceivableNet":                         {"statement": "BS", "category": "Assets",      "display_name": "Accounts receivable, net",        "order": 30,  "indent": 1},
    "AccountsReceivableNetCurrent":                  {"statement": "BS", "category": "Assets",      "display_name": "Accounts receivable, net",        "order": 31,  "indent": 1},
    "InventoryNet":                                  {"statement": "BS", "category": "Assets",      "display_name": "Inventories",                     "order": 40,  "indent": 1},
    "Inventory":                                     {"statement": "BS", "category": "Assets",      "display_name": "Inventories",                     "order": 40,  "indent": 1},
    "PrepaidExpenseAndOtherAssetsCurrent":           {"statement": "BS", "category": "Assets",      "display_name": "Prepaid expenses and other current assets", "order": 50, "indent": 1},
    "OtherAssetsCurrent":                            {"statement": "BS", "category": "Assets",      "display_name": "Other current assets",            "order": 60,  "indent": 1},

    "AssetsNoncurrent":                              {"statement": "BS", "category": "Assets",      "display_name": "Total non-current assets",        "order": 190, "indent": 0, "is_subtotal": True, "section": "Non-current assets:"},
    "MarketableSecuritiesNoncurrent":                {"statement": "BS", "category": "Assets",      "display_name": "Marketable securities",           "order": 110, "indent": 1},
    "PropertyPlantAndEquipmentNet":                  {"statement": "BS", "category": "Assets",      "display_name": "Property, plant and equipment, net", "order": 120, "indent": 1},
    "Goodwill":                                      {"statement": "BS", "category": "Assets",      "display_name": "Goodwill",                        "order": 130, "indent": 1},
    "IntangibleAssetsNetExcludingGoodwill":          {"statement": "BS", "category": "Assets",      "display_name": "Intangible assets, net",          "order": 140, "indent": 1},
    "IntangibleAssetsNet":                           {"statement": "BS", "category": "Assets",      "display_name": "Intangible assets, net",          "order": 140, "indent": 1},
    "OtherAssetsNoncurrent":                         {"statement": "BS", "category": "Assets",      "display_name": "Other non-current assets",        "order": 170, "indent": 1},

    "Assets":                                        {"statement": "BS", "category": "Assets",      "display_name": "Total assets",                    "order": 199, "indent": 0, "is_subtotal": True},
    "LiabilitiesAndStockholdersEquity":              {"statement": "BS", "category": "Equity",     "display_name": "Total liabilities and shareholders' equity", "order": 499, "indent": 0, "is_subtotal": True},
    "RedeemableNoncontrollingInterestEquityCarryingAmount": {"statement": "BS", "category": "Equity", "display_name": "Redeemable non-controlling interest", "order": 398, "indent": 0},

    # ── Balance Sheet: Liabilities ──
    "LiabilitiesCurrent":                            {"statement": "BS", "category": "Liabilities", "display_name": "Total current liabilities",       "order": 290, "indent": 0, "is_subtotal": True, "section": "Current liabilities:"},
    "AccountsPayableCurrent":                        {"statement": "BS", "category": "Liabilities", "display_name": "Accounts payable",                "order": 210, "indent": 1},
    "AccountsPayable":                               {"statement": "BS", "category": "Liabilities", "display_name": "Accounts payable",                "order": 211, "indent": 1},
    "AccruedLiabilitiesCurrent":                     {"statement": "BS", "category": "Liabilities", "display_name": "Accrued liabilities",             "order": 220, "indent": 1},
    "DeferredRevenueCurrent":                        {"statement": "BS", "category": "Liabilities", "display_name": "Deferred revenue",                "order": 230, "indent": 1},
    "LongTermDebtCurrent":                           {"statement": "BS", "category": "Liabilities", "display_name": "Term debt, current portion",      "order": 240, "indent": 1},
    "CommercialPaper":                               {"statement": "BS", "category": "Liabilities", "display_name": "Commercial paper",                "order": 250, "indent": 1},
    "ShortTermBorrowings":                           {"statement": "BS", "category": "Liabilities", "display_name": "Short-term borrowings",           "order": 255, "indent": 1},
    "OtherLiabilitiesCurrent":                       {"statement": "BS", "category": "Liabilities", "display_name": "Other current liabilities",       "order": 270, "indent": 1},

    "LiabilitiesNoncurrent":                         {"statement": "BS", "category": "Liabilities", "display_name": "Total non-current liabilities",   "order": 390, "indent": 0, "is_subtotal": True, "section": "Non-current liabilities:"},
    "LongTermDebt":                                  {"statement": "BS", "category": "Liabilities", "display_name": "Long-term debt",                  "order": 310, "indent": 1},
    "LongTermDebtNoncurrent":                        {"statement": "BS", "category": "Liabilities", "display_name": "Term debt, non-current",          "order": 311, "indent": 1},
    "DeferredRevenueNoncurrent":                     {"statement": "BS", "category": "Liabilities", "display_name": "Deferred revenue, non-current",   "order": 330, "indent": 1},
    "DeferredTaxLiabilitiesNoncurrent":              {"statement": "BS", "category": "Liabilities", "display_name": "Deferred tax liabilities",        "order": 340, "indent": 1},
    "OtherLiabilitiesNoncurrent":                    {"statement": "BS", "category": "Liabilities", "display_name": "Other non-current liabilities",   "order": 370, "indent": 1},

    "Liabilities":                                   {"statement": "BS", "category": "Liabilities", "display_name": "Total liabilities",               "order": 399, "indent": 0, "is_subtotal": True},
    "CommitmentsAndContingencies":                   {"statement": "BS", "category": "Liabilities", "display_name": "Commitments and contingencies",   "order": 395, "indent": 0},

    # ── Balance Sheet: Equity ──
    "StockholdersEquity":                                                            {"statement": "BS", "category": "Equity", "display_name": "Total shareholders' equity",  "order": 490, "indent": 0, "is_subtotal": True, "section": "Shareholders' equity:"},
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest":         {"statement": "BS", "category": "Equity", "display_name": "Total equity",                "order": 495, "indent": 0, "is_subtotal": True, "section": "Shareholders' equity:"},
    "CommonStockValue":                                                              {"statement": "BS", "category": "Equity", "display_name": "Common stock",                "order": 410, "indent": 1},
    "CommonStocksIncludingAdditionalPaidInCapital":                                   {"statement": "BS", "category": "Equity", "display_name": "Common stock and additional paid-in capital", "order": 410, "indent": 1},
    "AdditionalPaidInCapital":                                                       {"statement": "BS", "category": "Equity", "display_name": "Additional paid-in capital",  "order": 420, "indent": 1},
    "AdditionalPaidInCapitalCommonStock":                                             {"statement": "BS", "category": "Equity", "display_name": "Additional paid-in capital",  "order": 420, "indent": 1},
    "TreasuryStockValue":                                                            {"statement": "BS", "category": "Equity", "display_name": "Treasury stock",              "order": 430, "indent": 1},
    "RetainedEarningsAccumulatedDeficit":                                             {"statement": "BS", "category": "Equity", "display_name": "Retained earnings/(accumulated deficit)", "order": 440, "indent": 1},
    "AccumulatedOtherComprehensiveIncomeLossNetOfTax":                                {"statement": "BS", "category": "Equity", "display_name": "Accumulated other comprehensive income/(loss)", "order": 450, "indent": 1},
    "CommonStockSharesOutstanding":                                                  {"statement": "BS", "category": "Equity", "display_name": "Shares outstanding",          "order": 470, "indent": 1},
    "CommonStockSharesIssued":                                                       {"statement": "BS", "category": "Equity", "display_name": "Shares issued",               "order": 471, "indent": 1},

    # ── Income Statement ──
    # indent: 0 = top-level, 1 = line item, 2 = sub-detail
    # is_subtotal: True = bold with line above (Gross Profit, Operating Income, etc.)
    "Revenues":                                                          {"statement": "IS", "category": "Revenue", "display_name": "Net revenue",                 "order": 0,  "indent": 0},
    "RevenueFromContractWithCustomerExcludingAssessedTax":               {"statement": "IS", "category": "Revenue", "display_name": "Net revenue",                 "order": 1,  "indent": 0},
    "RevenueFromContractWithCustomerIncludingAssessedTax":               {"statement": "IS", "category": "Revenue", "display_name": "Net revenue (incl. taxes)",   "order": 2,  "indent": 0},
    "SalesRevenueNet":                                                   {"statement": "IS", "category": "Revenue", "display_name": "Net sales",                   "order": 3,  "indent": 0},
    "SalesRevenueGoodsNet":                                              {"statement": "IS", "category": "Revenue", "display_name": "Product revenue",             "order": 4,  "indent": 1},
    "SalesRevenueServicesNet":                                           {"statement": "IS", "category": "Revenue", "display_name": "Service revenue",             "order": 5,  "indent": 1},

    # ── Income Statement: Bank / Financial ──
    # These use the same order space but only appear for financial companies (mutually exclusive with COGS/Gross Profit)
    "InterestAndDividendIncomeOperating":                       {"statement": "IS", "category": "Revenue", "display_name": "Total interest income",           "order": 5,  "indent": 0},
    "InterestAndFeeIncomeLoansAndLeases":                       {"statement": "IS", "category": "Revenue", "display_name": "Interest and fees on loans",     "order": 6,  "indent": 1},
    "InterestIncomeDepositsWithFinancialInstitutions":          {"statement": "IS", "category": "Revenue", "display_name": "Interest on deposits with banks","order": 7,  "indent": 1},
    # InterestRevenueExpenseNet: same as InterestIncomeExpenseNet; left to SEC mapping to avoid duplication
    "InterestIncomeExpenseAfterProvisionForLoanLoss":           {"statement": "IS", "category": "Income",  "display_name": "Net interest income after provision for credit losses", "order": 17, "indent": 0, "is_subtotal": True},
    "ProvisionForLoanLeaseAndOtherLosses":                      {"statement": "IS", "category": "Income",  "display_name": "Provision for credit losses",    "order": 14, "indent": 0},
    "ProvisionForLoanAndLeaseLosses":                           {"statement": "IS", "category": "Income",  "display_name": "Provision for loan losses",      "order": 14, "indent": 0},
    "NoninterestIncome":                                        {"statement": "IS", "category": "Income",  "display_name": "Total noninterest income",       "order": 18, "indent": 0},
    "LaborAndRelatedExpense":                                   {"statement": "IS", "category": "Income",  "display_name": "Salaries and employee benefits", "order": 22, "indent": 1, "section": "Noninterest expense:"},
    "OccupancyNet":                                             {"statement": "IS", "category": "Income",  "display_name": "Occupancy and equipment",        "order": 23, "indent": 1},
    "InformationTechnologyAndDataProcessing":                   {"statement": "IS", "category": "Income",  "display_name": "Technology and data processing", "order": 24, "indent": 1},
    "FederalDepositInsuranceCorporationPremiumExpense":          {"statement": "IS", "category": "Income",  "display_name": "FDIC insurance",                 "order": 25, "indent": 1},
    "NoninterestExpense":                                       {"statement": "IS", "category": "Income",  "display_name": "Total noninterest expense",      "order": 26, "indent": 0, "is_subtotal": True},

    "CostOfRevenue":                                         {"statement": "IS", "category": "Income", "display_name": "Cost of sales",                "order": 10, "indent": 0},
    "CostOfGoodsAndServicesSold":                            {"statement": "IS", "category": "Income", "display_name": "Cost of goods and services sold", "order": 11, "indent": 0},
    "GrossProfit":                                           {"statement": "IS", "category": "Income", "display_name": "Gross margin",                 "order": 15, "indent": 0, "is_subtotal": True},

    "ResearchAndDevelopmentExpense":                         {"statement": "IS", "category": "Income", "display_name": "Research and development",     "order": 20, "indent": 1},
    "SellingGeneralAndAdministrativeExpense":                {"statement": "IS", "category": "Income", "display_name": "Selling, general and administrative", "order": 21, "indent": 1},
    "SellingAndMarketingExpense":                            {"statement": "IS", "category": "Income", "display_name": "Selling and marketing",        "order": 22, "indent": 2},
    "GeneralAndAdministrativeExpense":                       {"statement": "IS", "category": "Income", "display_name": "General and administrative",   "order": 23, "indent": 2},
    "OperatingExpenses":                                     {"statement": "IS", "category": "Income", "display_name": "Total operating expenses",     "order": 25, "indent": 1, "is_subtotal": True},
    "OperatingIncomeLoss":                                   {"statement": "IS", "category": "Income", "display_name": "Operating income",             "order": 30, "indent": 0, "is_subtotal": True},

    "InterestExpense":                                       {"statement": "IS", "category": "Revenue", "display_name": "Total interest expense",       "order": 8,  "indent": 0},
    "InterestIncome":                                        {"statement": "IS", "category": "Revenue", "display_name": "Interest income",              "order": 6,  "indent": 0},
    "InterestIncomeExpenseNet":                               {"statement": "IS", "category": "Income",  "display_name": "Net interest income",          "order": 12, "indent": 0, "is_subtotal": True},
    "OtherNonoperatingIncomeExpense":                        {"statement": "IS", "category": "Income", "display_name": "Other non-operating income/(expense)", "order": 38, "indent": 1},
    "NonoperatingIncomeExpense":                             {"statement": "IS", "category": "Income", "display_name": "Other income/(expense), net",  "order": 39, "indent": 0},

    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": {"statement": "IS", "category": "Income", "display_name": "Income before provision for income taxes", "order": 40, "indent": 0, "is_subtotal": True},
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments": {"statement": "IS", "category": "Income", "display_name": "Income before provision for income taxes", "order": 40, "indent": 0, "is_subtotal": True},
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic":              {"statement": "IS", "category": "Income", "display_name": "Domestic pre-tax income",   "order": 41, "indent": 1, "section": "Pre-tax income by jurisdiction:"},
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesForeign":               {"statement": "IS", "category": "Income", "display_name": "Foreign pre-tax income",    "order": 42, "indent": 1},

    "IncomeTaxExpenseBenefit":                                                  {"statement": "IS", "category": "Income", "display_name": "Provision for income taxes",         "order": 45,   "indent": 0},
    # Tax detail block: jurisdiction summary (indent 1) precedes current sub-row (indent 2).
    "FederalIncomeTaxExpenseBenefitContinuingOperations":                       {"statement": "IS", "category": "Income", "display_name": "Federal income tax expense",         "order": 46.0, "indent": 1, "section": "Provision for income taxes detail:"},
    "CurrentFederalTaxExpenseBenefit":                                          {"statement": "IS", "category": "Income", "display_name": "Federal current tax",                "order": 46.1, "indent": 2},
    "ForeignIncomeTaxExpenseBenefitContinuingOperations":                       {"statement": "IS", "category": "Income", "display_name": "Foreign income tax expense",         "order": 47.0, "indent": 1},
    "CurrentForeignTaxExpenseBenefit":                                          {"statement": "IS", "category": "Income", "display_name": "Foreign current tax",                "order": 47.1, "indent": 2},
    "StateAndLocalIncomeTaxExpenseBenefitContinuingOperations":                 {"statement": "IS", "category": "Income", "display_name": "State and local income tax expense", "order": 48.0, "indent": 1},
    "CurrentStateAndLocalTaxExpenseBenefit":                                    {"statement": "IS", "category": "Income", "display_name": "State and local current tax",        "order": 48.1, "indent": 2},
    # Across-jurisdiction current/deferred summary lines, in case companies report this view instead.
    "CurrentIncomeTaxExpenseBenefit":                                           {"statement": "IS", "category": "Income", "display_name": "Current tax expense",                "order": 48.5, "indent": 1},
    "DeferredIncomeTaxExpenseBenefitContinuingOperations":                      {"statement": "IS", "category": "Income", "display_name": "Deferred tax expense/(benefit)",     "order": 48.6, "indent": 1},
    "IncomeTaxReconciliationChangeInDeferredTaxAssetsValuationAllowance":       {"statement": "IS", "category": "Income", "display_name": "Change in valuation allowance",      "order": 48.7, "indent": 2},
    "EffectiveIncomeTaxRateContinuingOperations":                               {"statement": "IS", "category": "Income", "display_name": "Effective tax rate",                 "order": 49,   "indent": 0},

    "NetIncomeLoss":                                         {"statement": "IS", "category": "Income", "display_name": "Net income",                   "order": 50, "indent": 0, "is_subtotal": True},
    "ProfitLoss":                                            {"statement": "IS", "category": "Income", "display_name": "Net income",                   "order": 50, "indent": 0, "is_subtotal": True},
    "NetIncomeLossAvailableToCommonStockholdersBasic":       {"statement": "IS", "category": "Income", "display_name": "Net income attributable to common shareholders", "order": 52, "indent": 0},
    "ComprehensiveIncomeNetOfTax":                           {"statement": "IS", "category": "Income", "display_name": "Comprehensive income",         "order": 55, "indent": 0},

    # ── Income Statement: EPS ──
    "EarningsPerShareBasic":                                 {"statement": "IS", "category": "EPS", "display_name": "Basic",                           "order": 60, "indent": 1, "section": "Earnings per share:"},
    "EarningsPerShareDiluted":                               {"statement": "IS", "category": "EPS", "display_name": "Diluted",                         "order": 61, "indent": 1},
    "CommonStockDividendsPerShareDeclared":                  {"statement": "IS", "category": "EPS", "display_name": "Dividends declared per share",    "order": 65, "indent": 1, "section": "Dividends:"},
    "CommonStockDividendsPerShareCashPaid":                  {"statement": "IS", "category": "EPS", "display_name": "Dividends paid per share",        "order": 66, "indent": 1},
    "WeightedAverageNumberOfSharesOutstandingBasic":         {"statement": "IS", "category": "EPS", "display_name": "Basic",                           "order": 70, "indent": 1, "section": "Shares used in computing earnings per share:"},
    "WeightedAverageNumberOfDilutedSharesOutstanding":       {"statement": "IS", "category": "EPS", "display_name": "Diluted",                         "order": 71, "indent": 1},
    "WeightedAverageNumberOfShareOutstandingBasicAndDiluted": {"statement": "IS", "category": "EPS", "display_name": "Basic and diluted",              "order": 72, "indent": 1},
    "IncrementalCommonSharesAttributableToShareBasedPaymentArrangements": {"statement": "IS", "category": "EPS", "display_name": "Dilutive effect of share-based awards", "order": 73, "indent": 2},

    # ── Comprehensive Income (OCI) ──
    # Routed to category "OCI"; only the CI statement view includes this category.
    "OtherComprehensiveIncomeLossNetOfTax":                                      {"statement": "CI", "category": "OCI", "display_name": "Other comprehensive income/(loss), net of tax",                 "order": 80, "indent": 0, "is_subtotal": True, "section": "Other comprehensive income/(loss):"},
    "OtherComprehensiveIncomeLossNetOfTaxPortionAttributableToParent":           {"statement": "CI", "category": "OCI", "display_name": "Other comprehensive income/(loss), net of tax",                 "order": 80, "indent": 0, "is_subtotal": True, "section": "Other comprehensive income/(loss):"},
    "OtherComprehensiveIncomeForeignCurrencyTransactionAndTranslationAdjustmentNetOfTax": {"statement": "CI", "category": "OCI", "display_name": "Foreign currency translation adjustments",            "order": 81, "indent": 1},
    "OtherComprehensiveIncomeLossForeignCurrencyTransactionAndTranslationAdjustmentNetOfTax": {"statement": "CI", "category": "OCI", "display_name": "Foreign currency translation adjustments",        "order": 81, "indent": 1},
    "OtherComprehensiveIncomeLossAvailableForSaleSecuritiesAdjustmentNetOfTax":  {"statement": "CI", "category": "OCI", "display_name": "Unrealized gains/(losses) on available-for-sale securities",    "order": 82, "indent": 1},
    "OtherComprehensiveIncomeUnrealizedHoldingGainLossOnSecuritiesArisingDuringPeriodNetOfTax": {"statement": "CI", "category": "OCI", "display_name": "Unrealized holding gains/(losses) on securities", "order": 82, "indent": 1},
    "OtherComprehensiveIncomeLossDerivativesQualifyingAsHedgesNetOfTax":         {"statement": "CI", "category": "OCI", "display_name": "Unrealized gains/(losses) on cash flow hedges",                 "order": 83, "indent": 1},
    "OtherComprehensiveIncomeLossCashFlowHedgeGainLossBeforeReclassificationAfterTax": {"statement": "CI", "category": "OCI", "display_name": "Cash flow hedge gains/(losses) before reclassification",  "order": 83.2, "indent": 1},
    "OtherComprehensiveIncomeLossCashFlowHedgeGainLossReclassificationAfterTax": {"statement": "CI", "category": "OCI", "display_name": "Reclassification adjustment for (gains)/losses included in net income", "order": 83.5, "indent": 1},
    "OtherComprehensiveIncomeLossCashFlowHedgeGainLossAfterReclassificationAndTax": {"statement": "CI", "category": "OCI", "display_name": "Cash flow hedge gains/(losses) after reclassification",     "order": 83, "indent": 1},
    "OtherComprehensiveIncomeLossPensionAndOtherPostretirementBenefitPlansAdjustmentNetOfTax": {"statement": "CI", "category": "OCI", "display_name": "Pension and post-retirement plan adjustments",   "order": 84, "indent": 1},
    "OtherComprehensiveIncomeLossReclassificationAdjustmentFromAOCIForSaleOfSecuritiesNetOfTax": {"statement": "CI", "category": "OCI", "display_name": "Reclassification adjustment for securities sold", "order": 85, "indent": 1},

    "ComprehensiveIncomeNetOfTaxIncludingPortionAttributableToNoncontrollingInterest": {"statement": "CI", "category": "OCI", "display_name": "Comprehensive income (incl. non-controlling interest)", "order": 89, "indent": 0, "is_subtotal": True},

    # ── Cash Flow: Operating ──
    "NetCashProvidedByUsedInOperatingActivities":            {"statement": "CF", "category": "OperatingCashFlow",  "display_name": "Net Cash from Operations",             "order": 0},
    "DepreciationDepletionAndAmortization":                  {"statement": "CF", "category": "OperatingCashFlow",  "display_name": "Depreciation & Amortization",          "order": 10},
    "ShareBasedCompensation":                                {"statement": "CF", "category": "OperatingCashFlow",  "display_name": "Stock-Based Compensation",             "order": 20},
    "DeferredIncomeTaxExpenseBenefit":                       {"statement": "CF", "category": "OperatingCashFlow",  "display_name": "Deferred Income Taxes",                "order": 30},
    "IncreaseDecreaseInAccountsReceivable":                  {"statement": "CF", "category": "OperatingCashFlow",  "display_name": "Change in Accounts Receivable",        "order": 40},
    "IncreaseDecreaseInInventories":                         {"statement": "CF", "category": "OperatingCashFlow",  "display_name": "Change in Inventories",                "order": 50},
    "IncreaseDecreaseInAccountsPayable":                     {"statement": "CF", "category": "OperatingCashFlow",  "display_name": "Change in Accounts Payable",           "order": 60},

    # ── Cash Flow: Investing ──
    "NetCashProvidedByUsedInInvestingActivities":            {"statement": "CF", "category": "InvestingCashFlow",  "display_name": "Net Cash from Investing",              "order": 0},
    "PaymentsToAcquirePropertyPlantAndEquipment":            {"statement": "CF", "category": "InvestingCashFlow",  "display_name": "Capital Expenditures",                 "order": 10},
    "PaymentsToAcquireBusinessesNetOfCashAcquired":          {"statement": "CF", "category": "InvestingCashFlow",  "display_name": "Acquisitions",                         "order": 20},
    "PaymentsToAcquireInvestments":                          {"statement": "CF", "category": "InvestingCashFlow",  "display_name": "Purchases of Investments",             "order": 30},
    "ProceedsFromSaleAndMaturityOfInvestments":              {"statement": "CF", "category": "InvestingCashFlow",  "display_name": "Proceeds from Investments",            "order": 31},
    "ProceedsFromMaturitiesPrepaymentsAndCallsOfAvailableForSaleSecurities": {"statement": "CF", "category": "InvestingCashFlow", "display_name": "Maturities of Securities", "order": 32},

    # ── Cash Flow: Financing ──
    "NetCashProvidedByUsedInFinancingActivities":            {"statement": "CF", "category": "FinancingCashFlow",  "display_name": "Net Cash from Financing",              "order": 0},
    "PaymentsOfDividends":                                   {"statement": "CF", "category": "FinancingCashFlow",  "display_name": "Dividends Paid",                       "order": 10},
    "PaymentsOfDividendsCommonStock":                        {"statement": "CF", "category": "FinancingCashFlow",  "display_name": "Common Dividends Paid",                "order": 11},
    "PaymentsForRepurchaseOfCommonStock":                    {"statement": "CF", "category": "FinancingCashFlow",  "display_name": "Share Repurchases",                    "order": 20},
    "ProceedsFromIssuanceOfLongTermDebt":                   {"statement": "CF", "category": "FinancingCashFlow",  "display_name": "Proceeds from Debt Issuance",          "order": 30},
    "RepaymentsOfLongTermDebt":                              {"statement": "CF", "category": "FinancingCashFlow",  "display_name": "Repayments of Debt",                   "order": 35},
    "ProceedsFromIssuanceOfCommonStock":                    {"statement": "CF", "category": "FinancingCashFlow",  "display_name": "Proceeds from Stock Issuance",         "order": 40},
}

# Tags that should never be classified. These are footnote / axis-member /
# rate-reconciliation disclosures the auto-derived SEC mapping placed on a
# statement, but they aren't real statement lines.
_SKIP_TAGS = frozenset({
    # Share-count of buybacks — lives in equity changes, not the IS.
    "StockRepurchasedAndRetiredDuringPeriodShares",
    "StockRepurchasedAndRetiredDuringPeriodValue",
    # Tax reconciliation footnote disclosures (not BS lines).
    "DeferredTaxAssetsGross",
    "DeferredTaxAssetsTaxCreditCarryforwards",
    "DeferredTaxAssetsTaxDeferredExpense",
    "DeferredTaxAssetsValuationAllowance",
    "UnrecognizedTaxBenefits",
    "UnrecognizedTaxBenefitsIncomeTaxPenaltiesAndInterestExpense",
    # Auto-mapping classified these as Assets — they belong on the liability side
    # but `TaxesPayableCurrent` already provides the canonical view.
    "AccruedIncomeTaxes",
    "AccruedIncomeTaxesCurrent",
    "AccruedIncomeTaxesNoncurrent",
    # Buyback excise tax and accelerated-program footnote items.
    "ShareRepurchaseProgramExciseTax",
    "ShareRepurchaseProgramExciseTaxPayable",
    "AcceleratedShareRepurchasesAdjustmentToRecordedAmount",
    # Non-standard total-noncurrent-assets variant; canonical tag is AssetsNoncurrent.
    "NoncurrentAssets",
})

# Categories that belong to each statement type
_STATEMENT_CATEGORIES = {
    "BS":  ["Assets", "Liabilities", "Equity"],
    "IS":  ["Revenue", "Income", "EPS"],
    "CF":  ["OperatingCashFlow", "InvestingCashFlow", "FinancingCashFlow"],
    "CI":  ["Revenue", "Income", "EPS", "OCI"],
    "EQ":  ["Equity"],
}


class TagClassifier:
    """
    Classifies XBRL concept tags into financial statement categories.

    Uses a two-tier lookup:
    1. Builtin overrides (hand-curated, ~100 common tags with clean labels)
    2. SEC Financial Statement Data Set mapping (3,500+ tags from real filings)

    Tags not found in either source are skipped.
    """

    def classify_tags(self, concept_names, statement_type=None):
        """
        Classify a list of XBRL concept names.

        Args:
            concept_names: list of concept name strings
            statement_type: optional filter — "BS", "IS", "CF", or "ALL"/None

        Returns:
            dict mapping concept_name -> classification dict
        """
        results = {}
        skipped = 0

        for name in concept_names:
            info = self._lookup(name)
            if info is not None:
                results[name] = info
            else:
                skipped += 1

        if skipped:
            logger.debug(f"Skipped {skipped} unrecognized tags (not in SEC mapping or builtins)")

        # Filter by statement type
        if statement_type and statement_type != "ALL":
            allowed_categories = set(_STATEMENT_CATEGORIES.get(statement_type, []))
            results = {
                k: v for k, v in results.items()
                if v.get("category") in allowed_categories
            }

        return results

    def _lookup(self, concept_name):
        """Check builtin overrides first, then SEC mapping. Tags source.

        Returns None for tags listed in ``_SKIP_TAGS`` even if the SEC mapping
        has them — that filters out footnote / disclosure pollution.
        """
        if concept_name in _SKIP_TAGS:
            return None
        if concept_name in _BUILTIN_TAGS:
            result = dict(_BUILTIN_TAGS[concept_name])
            result['source'] = 'builtin'
            return result
        if concept_name in _SEC_MAPPING:
            result = dict(_SEC_MAPPING[concept_name])
            result['source'] = 'sec'
            return result
        return None
