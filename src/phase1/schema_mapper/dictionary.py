"""
Neural Ledger — Pakistani Accounting Column Dictionary (Step 4a)
=================================================================
The knowledge base for the Schema Mapper.

This dictionary maps every known column name variation used by Pakistani
accountants and accounting software to the Neural Ledger standard field names.

Organized by:
  1. Standard fields — what we want to produce in Silver
  2. Per-software mappings — exact column names per software
  3. General mappings — common variations across all software
  4. Urdu/mixed mappings — Urdu and bilingual column names
  5. Abbreviation mappings — short forms Pakistani accountants use
  6. FBR tax category mappings — for compliance signals

Research basis:
  - Tally/TallyPrime exports (Particulars, Vch Type, Vch No., Debit, Credit)
  - QuickBooks Desktop exports (Date, Transaction Type, Num, Name, Memo, Amount)
  - QuickBooks Online exports (Date, Description, Amount, Balance)
  - LedgerMax exports (Transaction Date, Account, Narration, Dr Amount, Cr Amount)
  - Manual Pakistani Excel ledgers (Taareekh, Raqam, Tafseelat, Dr, Cr)
  - Bank statement exports (HBL, UBL, Alfalah, Meezan, NBP)
  - Urdu accounting terminology from ICAP-Pakistan standards
"""

from typing import Dict, List, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# STANDARD TARGET FIELDS
# These are the field names that Silver produces.
# Every mapping points to one of these.
# ─────────────────────────────────────────────────────────────────────────────

STANDARD_FIELDS = {
    "transaction_date",    # ISO date of the transaction
    "amount_debit",        # Debit amount (money out)
    "amount_credit",       # Credit amount (money in)
    "net_amount",          # Single net amount (when Dr/Cr not split)
    "vendor",              # Vendor / party / payee name
    "description",         # Narration / memo / particulars
    "account_code",        # Chart of accounts code
    "tx_type",             # Transaction type (Payment/Receipt/Journal)
    "reference_number",    # Voucher / invoice / cheque number
    "balance",             # Running balance (informational only)
    "currency",            # Currency code (default PKR)
    "cost_center",         # Department / cost center
    "project_code",        # Project code if applicable
    "tax_amount",          # Tax / GST / WHT amount
    "tax_rate",            # Tax rate applied
    "invoice_number",      # Invoice number (AP/AR)
    "due_date",            # Payment due date
    "employee_name",       # For payroll entries
    "bank_name",           # Bank name for bank entries
    "cheque_number",       # Cheque / payment reference
    # Metadata (not mapped from user data — set by system)
    # source_software, source_type, ingested_at, etc.
}

# ─────────────────────────────────────────────────────────────────────────────
# RESERVED INTERNAL NAMES
# If a user file contains these column names, they are renamed with raw_ prefix
# to prevent schema confusion attacks.
# ─────────────────────────────────────────────────────────────────────────────

RESERVED_INTERNAL_NAMES = {
    "transaction_id", "silver_id", "bronze_id", "gold_id",
    "quality_score", "pii_masked", "embedding_text", "gold_version",
    "qdrant_indexed", "category_confidence", "fbr_category",
    "processing_status", "ingestion_batch_id", "source_file_hash",
    "normalisation_confidence", "is_duplicate", "duplicate_of",
}

# ─────────────────────────────────────────────────────────────────────────────
# PER-SOFTWARE EXACT MAPPINGS
# Column names as they appear exactly in each software's export.
# Confidence = 0.95 for exact software-specific matches.
# ─────────────────────────────────────────────────────────────────────────────

SOFTWARE_EXACT_MAPPINGS: Dict[str, Dict[str, str]] = {

    # ── Tally / TallyPrime ──────────────────────────────────────────────────
    # Source: Tally ledger reports, daybook exports, journal exports
    "tally": {
        "Date":                 "transaction_date",
        "Particulars":          "vendor",          # Can also be description — resolved by context
        "Vch Type":             "tx_type",
        "Vch No.":              "reference_number",
        "Vch No":               "reference_number",
        "Voucher No":           "reference_number",
        "Voucher No.":          "reference_number",
        "Debit":                "amount_debit",
        "Credit":               "amount_credit",
        "Debit Amount":         "amount_debit",
        "Credit Amount":        "amount_credit",
        "Dr":                   "amount_debit",
        "Cr":                   "amount_credit",
        "Ledger":               "account_code",
        "Narration":            "description",
        "Cost Centre":          "cost_center",
        "Cost Center":          "cost_center",
        "Employee":             "employee_name",
        "Balance":              "balance",
        "Closing Balance":      "balance",
    },

    # ── QuickBooks Desktop ──────────────────────────────────────────────────
    # Source: Transaction Detail Report, General Ledger Report
    "quickbooks_desktop": {
        "Date":                 "transaction_date",
        "Transaction Type":     "tx_type",
        "Type":                 "tx_type",
        "Num":                  "reference_number",
        "Name":                 "vendor",
        "Memo":                 "description",
        "Split":                "account_code",
        "Amount":               "net_amount",
        "Debit":                "amount_debit",
        "Credit":               "amount_credit",
        "Balance":              "balance",
        "Account":              "account_code",
        "Class":                "cost_center",
        "Customer:Job":         "vendor",
        "Vendor":               "vendor",
        "Payee":                "vendor",
        "Ref No.":              "reference_number",
        "Chk No":               "cheque_number",
    },

    # ── QuickBooks Online ───────────────────────────────────────────────────
    "quickbooks_online": {
        "Date":                 "transaction_date",
        "Transaction Type":     "tx_type",
        "No.":                  "reference_number",
        "Posting":              "tx_type",
        "Name":                 "vendor",
        "Memo/Description":     "description",
        "Description":          "description",
        "Account":              "account_code",
        "Debit":                "amount_debit",
        "Credit":               "amount_credit",
        "Amount":               "net_amount",
        "Balance":              "balance",
        "Tax Amount":           "tax_amount",
        "Tax Code":             "tax_rate",
    },

    # ── LedgerMax ───────────────────────────────────────────────────────────
    # Source: LedgerMax Pakistan transaction reports
    "ledgermax": {
        "Transaction Date":     "transaction_date",
        "Date":                 "transaction_date",
        "Account":              "account_code",
        "Account Name":         "vendor",
        "Narration":            "description",
        "Reference":            "reference_number",
        "Ref No":               "reference_number",
        "Dr Amount":            "amount_debit",
        "Cr Amount":            "amount_credit",
        "Debit":                "amount_debit",
        "Credit":               "amount_credit",
        "Balance":              "balance",
        "Transaction Type":     "tx_type",
        "Voucher No":           "reference_number",
        "Remarks":              "description",
        "Party Name":           "vendor",
        "Cost Center":          "cost_center",
    },

    # ── Xero ────────────────────────────────────────────────────────────────
    "xero": {
        "Date":                 "transaction_date",
        "Description":          "description",
        "Reference":            "reference_number",
        "Debit":                "amount_debit",
        "Credit":               "amount_credit",
        "Gross":                "net_amount",
        "Tax":                  "tax_amount",
        "Account":              "account_code",
        "Account Code":         "account_code",
        "Source":               "tx_type",
        "Contact":              "vendor",
        "Tracking":             "cost_center",
        "Invoice Number":       "invoice_number",
        "Due Date":             "due_date",
        "Currency":             "currency",
    },

    # ── Odoo / ERPNext ──────────────────────────────────────────────────────
    "odoo": {
        "Date":                 "transaction_date",
        "Journal Entry":        "reference_number",
        "Reference":            "reference_number",
        "Partner":              "vendor",
        "Account":              "account_code",
        "Label":                "description",
        "Debit":                "amount_debit",
        "Credit":               "amount_credit",
        "Balance":              "balance",
        "Analytic":             "cost_center",
        "Currency":             "currency",
        "Tax":                  "tax_amount",
    },

    # ── Sage 50 ─────────────────────────────────────────────────────────────
    "sage": {
        "Date":                 "transaction_date",
        "Reference":            "reference_number",
        "Details":              "description",
        "Net Activity":         "net_amount",
        "Debit":                "amount_debit",
        "Credit":               "amount_credit",
        "Balance":              "balance",
        "Account Code":         "account_code",
        "Name":                 "vendor",
        "Tax Code":             "tax_rate",
        "Tax Amount":           "tax_amount",
    },

    # ── Moneypex (Pakistani software) ───────────────────────────────────────
    "moneypex": {
        "Date":                 "transaction_date",
        "Description":          "description",
        "Category":             "account_code",
        "Amount":               "net_amount",
        "Debit":                "amount_debit",
        "Credit":               "amount_credit",
        "Reference":            "reference_number",
        "Contact":              "vendor",
        "Tax":                  "tax_amount",
        "Currency":             "currency",
    },

    # ── Bank PDF — Generic (HBL, UBL, Alfalah, Meezan, NBP) ────────────────
    # These column names appear in Pakistani bank e-statement PDFs after OCR
    "bank_pdf_generic": {
        "Date":                 "transaction_date",
        "Value Date":           "transaction_date",
        "Transaction Date":     "transaction_date",
        "Description":          "description",
        "Narration":            "description",
        "Particulars":          "description",
        "Details":              "description",
        "Debit":                "amount_debit",
        "Credit":               "amount_credit",
        "Withdrawal":           "amount_debit",
        "Deposit":              "amount_credit",
        "Dr":                   "amount_debit",
        "Cr":                   "amount_credit",
        "Amount":               "net_amount",
        "Balance":              "balance",
        "Running Balance":      "balance",
        "Closing Balance":      "balance",
        "Cheque No":            "cheque_number",
        "Cheque Number":        "cheque_number",
        "Reference":            "reference_number",
        "Transaction ID":       "reference_number",
        "Txn ID":               "reference_number",
        "Branch":               "bank_name",
    },
}

# Same mappings apply to all Pakistani bank PDF variants
SOFTWARE_EXACT_MAPPINGS["bank_pdf_hbl"]     = SOFTWARE_EXACT_MAPPINGS["bank_pdf_generic"]
SOFTWARE_EXACT_MAPPINGS["bank_pdf_ubl"]     = SOFTWARE_EXACT_MAPPINGS["bank_pdf_generic"]
SOFTWARE_EXACT_MAPPINGS["bank_pdf_alfalah"] = SOFTWARE_EXACT_MAPPINGS["bank_pdf_generic"]
SOFTWARE_EXACT_MAPPINGS["bank_pdf_meezan"]  = SOFTWARE_EXACT_MAPPINGS["bank_pdf_generic"]
SOFTWARE_EXACT_MAPPINGS["bank_pdf_nbp"]     = SOFTWARE_EXACT_MAPPINGS["bank_pdf_generic"]


# ─────────────────────────────────────────────────────────────────────────────
# GENERAL MAPPINGS
# Column names that appear across many software types and manual Excel files.
# Used when source_software is unknown or as fallback for all software.
# Confidence = 0.80 for general matches.
# ─────────────────────────────────────────────────────────────────────────────

GENERAL_MAPPINGS: Dict[str, str] = {
    # Date variations
    "Date":                     "transaction_date",
    "Transaction Date":         "transaction_date",
    "Txn Date":                 "transaction_date",
    "Posting Date":             "transaction_date",
    "Value Date":               "transaction_date",
    "Entry Date":               "transaction_date",
    "Invoice Date":             "transaction_date",
    "Payment Date":             "transaction_date",
    "Voucher Date":             "transaction_date",
    "Trans Date":               "transaction_date",

    # Amount — single column
    "Amount":                   "net_amount",
    "Net Amount":               "net_amount",
    "Total":                    "net_amount",
    "Total Amount":             "net_amount",
    "Transaction Amount":       "net_amount",
    "Value":                    "net_amount",
    "Sum":                      "net_amount",
    "PKR":                      "net_amount",
    "Rs.":                      "net_amount",
    "Rs":                       "net_amount",
    "Amount (PKR)":             "net_amount",
    "Amount PKR":               "net_amount",
    "Amount (Rs)":              "net_amount",

    # Debit variations
    "Debit":                    "amount_debit",
    "Debit Amount":             "amount_debit",
    "Dr":                       "amount_debit",
    "Dr.":                      "amount_debit",
    "Dr Amount":                "amount_debit",
    "Withdrawal":               "amount_debit",
    "Payment":                  "amount_debit",
    "Outflow":                  "amount_debit",
    "Expense":                  "amount_debit",
    "Paid":                     "amount_debit",
    "Debit (PKR)":              "amount_debit",

    # Credit variations
    "Credit":                   "amount_credit",
    "Credit Amount":            "amount_credit",
    "Cr":                       "amount_credit",
    "Cr.":                      "amount_credit",
    "Cr Amount":                "amount_credit",
    "Deposit":                  "amount_credit",
    "Receipt":                  "amount_credit",
    "Inflow":                   "amount_credit",
    "Income":                   "amount_credit",
    "Received":                 "amount_credit",
    "Credit (PKR)":             "amount_credit",

    # Vendor / party
    "Vendor":                   "vendor",
    "Party":                    "vendor",
    "Party Name":               "vendor",
    "Name":                     "vendor",
    "Payee":                    "vendor",
    "Customer":                 "vendor",
    "Supplier":                 "vendor",
    "Contact":                  "vendor",
    "Account Name":             "vendor",
    "Beneficiary":              "vendor",
    "Client":                   "vendor",
    "Creditor":                 "vendor",
    "Debtor":                   "vendor",
    "Paid To":                  "vendor",
    "Received From":            "vendor",

    # Description / narration
    "Description":              "description",
    "Narration":                "description",
    "Particulars":              "description",
    "Memo":                     "description",
    "Details":                  "description",
    "Remarks":                  "description",
    "Note":                     "description",
    "Notes":                    "description",
    "Comments":                 "description",
    "Detail":                   "description",
    "Transaction Detail":       "description",
    "Purpose":                  "description",
    "Reason":                   "description",
    "Subject":                  "description",

    # Account / ledger
    "Account":                  "account_code",
    "Account Code":             "account_code",
    "Account No":               "account_code",
    "Account Number":           "account_code",
    "Ledger":                   "account_code",
    "GL Code":                  "account_code",
    "GL Account":               "account_code",
    "Chart of Account":         "account_code",
    "COA":                      "account_code",
    "Head":                     "account_code",
    "Account Head":             "account_code",

    # Transaction type
    "Type":                     "tx_type",
    "Transaction Type":         "tx_type",
    "Voucher Type":             "tx_type",
    "Vch Type":                 "tx_type",
    "Entry Type":               "tx_type",
    "Journal Type":             "tx_type",
    "Mode":                     "tx_type",
    "Payment Mode":             "tx_type",

    # Reference numbers
    "Reference":                "reference_number",
    "Ref":                      "reference_number",
    "Ref No":                   "reference_number",
    "Ref No.":                  "reference_number",
    "Reference No":             "reference_number",
    "Reference Number":         "reference_number",
    "Voucher No":               "reference_number",
    "Vch No":                   "reference_number",
    "Vch No.":                  "reference_number",
    "Voucher Number":           "reference_number",
    "Invoice No":               "invoice_number",
    "Invoice Number":           "invoice_number",
    "Invoice #":                "invoice_number",
    "PO Number":                "reference_number",
    "Document No":              "reference_number",
    "Trans ID":                 "reference_number",
    "Transaction ID":           "reference_number",
    "Txn ID":                   "reference_number",
    "Receipt No":               "reference_number",
    "Journal No":               "reference_number",

    # Cheque
    "Cheque No":                "cheque_number",
    "Cheque Number":            "cheque_number",
    "Cheque #":                 "cheque_number",
    "Chk No":                   "cheque_number",
    "Check No":                 "cheque_number",
    "Chq No":                   "cheque_number",

    # Balance
    "Balance":                  "balance",
    "Running Balance":          "balance",
    "Closing Balance":          "balance",
    "Outstanding":              "balance",
    "Book Balance":             "balance",

    # Tax
    "Tax":                      "tax_amount",
    "GST":                      "tax_amount",
    "Sales Tax":                "tax_amount",
    "Tax Amount":               "tax_amount",
    "WHT":                      "tax_amount",
    "Withholding Tax":          "tax_amount",
    "FED":                      "tax_amount",
    "Tax Rate":                 "tax_rate",
    "GST Rate":                 "tax_rate",
    "Tax %":                    "tax_rate",

    # Cost center / department
    "Cost Center":              "cost_center",
    "Cost Centre":              "cost_center",
    "Department":               "cost_center",
    "Dept":                     "cost_center",
    "Division":                 "cost_center",
    "Branch":                   "cost_center",
    "Project":                  "project_code",
    "Project Code":             "project_code",

    # Currency
    "Currency":                 "currency",
    "Curr":                     "currency",
    "CCY":                      "currency",

    # Dates
    "Due Date":                 "due_date",
    "Payment Due":              "due_date",
    "Maturity Date":            "due_date",

    # Employee / payroll
    "Employee":                 "employee_name",
    "Employee Name":            "employee_name",
    "Staff":                    "employee_name",
    "Staff Name":               "employee_name",
    "Salary":                   "net_amount",
}


# ─────────────────────────────────────────────────────────────────────────────
# URDU AND MIXED-LANGUAGE MAPPINGS
# Common Urdu column names used by Pakistani accountants.
# Also handles transliterated Urdu written in Roman script.
# Confidence = 0.90 for exact Urdu matches (high confidence — very specific).
# ─────────────────────────────────────────────────────────────────────────────

URDU_MAPPINGS: Dict[str, str] = {
    # Urdu script
    "تاریخ":        "transaction_date",   # Taareekh — date
    "تاريخ":        "transaction_date",   # alternate spelling
    "رقم":          "net_amount",         # Raqam — amount
    "رقم کل":       "net_amount",         # Raqam-e-Kul — total amount
    "تفصیل":        "description",        # Tafseelat — details
    "تفصيل":        "description",        # alternate spelling
    "بیان":         "description",        # Bayan — statement/description
    "نام":          "vendor",             # Naam — name
    "پارٹی":        "vendor",             # Party
    "پارٹی نام":    "vendor",             # Party name
    "جمع":          "amount_credit",      # Jama — credit/deposit
    "خرچ":          "amount_debit",       # Kharch — expense/debit
    "واجب":         "amount_debit",       # Wajib — debit/payable
    "بقیہ":         "balance",            # Baqiya — balance/remainder
    "بقایا":        "balance",            # Baqaya — outstanding balance
    "کل":           "balance",            # Kul — total
    "حوالہ":        "reference_number",   # Hawala — reference
    "حوالہ نمبر":   "reference_number",   # Hawala number
    "نوعیت":        "tx_type",            # Noiyyat — type/nature
    "اکاؤنٹ":       "account_code",       # Account
    "محکمہ":        "cost_center",        # Mahkama — department
    "شعبہ":         "cost_center",        # Shuba — division/branch
    "ٹیکس":         "tax_amount",         # Tax
    "ملازم":        "employee_name",      # Mulazim — employee

    # Roman Urdu (Urdu words written in English letters)
    "Taareekh":     "transaction_date",
    "Taarikh":      "transaction_date",
    "Tarikh":       "transaction_date",
    "Raqam":        "net_amount",
    "Raqm":         "net_amount",
    "Tafseelat":    "description",
    "Tafsilat":     "description",
    "Tafseel":      "description",
    "Bayan":        "description",
    "Karwai":       "description",        # Karwai — transaction/action
    "Naam":         "vendor",
    "Party Naam":   "vendor",
    "Jama":         "amount_credit",
    "Kharch":       "amount_debit",
    "Aamdani":      "amount_credit",      # Income
    "Baqiya":       "balance",
    "Baqaya":       "balance",
    "Hawala":       "reference_number",
    "Noiyyat":      "tx_type",
    "Kul":          "balance",
    "Kul Raqam":    "net_amount",
    "Mulazim":      "employee_name",
    "Mahkama":      "cost_center",
    "Ittala":       "description",        # Information/note
}


# ─────────────────────────────────────────────────────────────────────────────
# ABBREVIATION MAPPINGS
# Single-letter and short abbreviations Pakistani accountants commonly use.
# These are context-sensitive — confidence = 0.70 (lower, needs validation).
# ─────────────────────────────────────────────────────────────────────────────

ABBREVIATION_MAPPINGS: Dict[str, str] = {
    # Single letter
    "D":    "amount_debit",
    "C":    "amount_credit",

    # Two letter
    "Dt":   "transaction_date",
    "Dt.":  "transaction_date",
    "Vd":   "transaction_date",   # Value date
    "Pd":   "transaction_date",   # Posting date
    "Tp":   "tx_type",            # Type
    "Ac":   "account_code",
    "Ac.":  "account_code",

    # Three letter
    "Dbt":  "amount_debit",
    "Crt":  "amount_credit",
    "Amt":  "net_amount",
    "Amt.": "net_amount",
    "Ref":  "reference_number",
    "Nar":  "description",        # Narration
    "Pty":  "vendor",             # Party
    "Bal":  "balance",
    "Bal.": "balance",
    "Inv":  "invoice_number",
    "Chq":  "cheque_number",
    "Tax":  "tax_amount",
    "Gst":  "tax_amount",

    # Common abbreviations with periods
    "Acc.":  "account_code",
    "Acc. No.": "account_code",
    "Ref.":  "reference_number",
    "No.":   "reference_number",
    "Desc.": "description",
    "Desc":  "description",
    "Amt":   "net_amount",
    "Vch":   "reference_number",  # Voucher
    "Dr.":   "amount_debit",
    "Cr.":   "amount_credit",
}


# ─────────────────────────────────────────────────────────────────────────────
# FBR TAX CATEGORY MAPPINGS
# Maps transaction categories / account heads to FBR tax heads.
# Used for compliance signal generation in the value layer.
# ─────────────────────────────────────────────────────────────────────────────

FBR_TAX_CATEGORIES: Dict[str, Dict] = {
    # Salary / payroll
    "salary":           {"fbr_section": "149",    "wht_applicable": True,  "wht_rate_filer": None, "description": "Salary income — progressive tax slabs"},
    "wages":            {"fbr_section": "149",    "wht_applicable": True,  "wht_rate_filer": None, "description": "Wages — salary tax applies"},

    # Services
    "services":         {"fbr_section": "153(1)(b)", "wht_applicable": True,  "wht_rate_filer": 0.08, "description": "Payment for services — 8% WHT (filer)"},
    "consulting":       {"fbr_section": "153(1)(b)", "wht_applicable": True,  "wht_rate_filer": 0.08, "description": "Consulting fee — 8% WHT (filer)"},
    "professional_fee": {"fbr_section": "153(1)(b)", "wht_applicable": True,  "wht_rate_filer": 0.08, "description": "Professional fee — 8% WHT (filer)"},
    "it_services":      {"fbr_section": "153(1)(b)", "wht_applicable": True,  "wht_rate_filer": 0.04, "description": "IT services — 4% WHT (filer)"},

    # Goods / supplies
    "supplies":         {"fbr_section": "153(1)(a)", "wht_applicable": True,  "wht_rate_filer": 0.04, "description": "Supply of goods — 4% WHT (filer)"},
    "purchases":        {"fbr_section": "153(1)(a)", "wht_applicable": True,  "wht_rate_filer": 0.04, "description": "Goods purchase — 4% WHT (filer)"},
    "raw_material":     {"fbr_section": "153(1)(a)", "wht_applicable": True,  "wht_rate_filer": 0.04, "description": "Raw material — 4% WHT (filer)"},

    # Rent
    "rent":             {"fbr_section": "155",    "wht_applicable": True,  "wht_rate_filer": 0.15, "description": "Rent payment — 15% WHT (filer)"},

    # Dividends
    "dividend":         {"fbr_section": "150",    "wht_applicable": True,  "wht_rate_filer": 0.25, "description": "Dividend — 25% WHT"},

    # Bank profit / interest
    "bank_profit":      {"fbr_section": "151",    "wht_applicable": True,  "wht_rate_filer": 0.20, "description": "Profit on bank deposit — 20% WHT (filer)"},
    "interest":         {"fbr_section": "151",    "wht_applicable": True,  "wht_rate_filer": 0.20, "description": "Interest income — 20% WHT (filer)"},

    # Transport / freight
    "freight":          {"fbr_section": "153A",   "wht_applicable": True,  "wht_rate_filer": 0.02, "description": "Freight/transport — 2% WHT (filer)"},
    "transport":        {"fbr_section": "153A",   "wht_applicable": True,  "wht_rate_filer": 0.02, "description": "Transport service — 2% WHT (filer)"},

    # Utilities (generally not WHT applicable for payments — just GST)
    "electricity":      {"fbr_section": None,     "wht_applicable": False, "wht_rate_filer": None, "description": "Electricity — GST applies at 18%"},
    "gas":              {"fbr_section": None,     "wht_applicable": False, "wht_rate_filer": None, "description": "Gas — GST applies"},
    "telephone":        {"fbr_section": None,     "wht_applicable": False, "wht_rate_filer": None, "description": "Telecom — WHT at source by telco"},

    # Not applicable
    "fuel":             {"fbr_section": None,     "wht_applicable": False, "wht_rate_filer": None, "description": "Fuel — GST included in price"},
    "stationery":       {"fbr_section": None,     "wht_applicable": False, "wht_rate_filer": None, "description": "Stationery — no WHT below threshold"},
    "miscellaneous":    {"fbr_section": None,     "wht_applicable": False, "wht_rate_filer": None, "description": "Miscellaneous — review individually"},
}


# ─────────────────────────────────────────────────────────────────────────────
# TRANSACTION TYPE NORMALISATION
# Maps source tx_type values to Neural Ledger standard tx types.
# ─────────────────────────────────────────────────────────────────────────────

TX_TYPE_NORMALISATION: Dict[str, str] = {
    # Payments
    "Payment":              "payment",
    "Pay":                  "payment",
    "Cash Payment":         "payment",
    "Bank Payment":         "payment",
    "Pmt":                  "payment",
    "CP":                   "payment",
    "BP":                   "payment",
    "Bhugtan":              "payment",    # Urdu — payment

    # Receipts
    "Receipt":              "receipt",
    "Rcpt":                 "receipt",
    "Cash Receipt":         "receipt",
    "Bank Receipt":         "receipt",
    "CR":                   "receipt",
    "BR":                   "receipt",
    "Received":             "receipt",
    "Wasool":               "receipt",    # Urdu — receipt/collection

    # Journals
    "Journal":              "journal",
    "Journal Entry":        "journal",
    "JV":                   "journal",
    "J/V":                  "journal",
    "Journal Voucher":      "journal",
    "Adjustment":           "journal",

    # Sales
    "Sales":                "sales",
    "Sale":                 "sales",
    "Invoice":              "sales",
    "Sales Invoice":        "sales",
    "Tax Invoice":          "sales",

    # Purchases
    "Purchase":             "purchase",
    "Bill":                 "purchase",
    "Vendor Bill":          "purchase",
    "Purchase Invoice":     "purchase",
    "Expense":              "purchase",

    # Transfers
    "Transfer":             "transfer",
    "Bank Transfer":        "transfer",
    "Fund Transfer":        "transfer",
    "TRF":                  "transfer",

    # Payroll
    "Salary":               "payroll",
    "Payroll":              "payroll",
    "Wages":                "payroll",

    # Contra
    "Contra":               "contra",
    "Contra Entry":         "contra",
}


# ─────────────────────────────────────────────────────────────────────────────
# HELPER — Lookup API
# Used by the fuzzy mapper and schema mapper.
# ─────────────────────────────────────────────────────────────────────────────

def get_software_mapping(software: str) -> Dict[str, str]:
    """Return the exact column mapping for a given software. Empty dict if unknown."""
    return SOFTWARE_EXACT_MAPPINGS.get(software, {})


def get_all_general_mappings() -> Dict[str, str]:
    """Return merged general + urdu + abbreviation mappings."""
    merged = {}
    merged.update(ABBREVIATION_MAPPINGS)   # Lowest priority
    merged.update(GENERAL_MAPPINGS)        # Medium priority
    merged.update(URDU_MAPPINGS)           # High priority (specific)
    return merged


def is_reserved(column_name: str) -> bool:
    """Return True if this column name conflicts with internal schema fields."""
    return column_name.lower() in {n.lower() for n in RESERVED_INTERNAL_NAMES}


def get_fbr_info(category: str) -> dict:
    """Return FBR tax info for a given category. None if not found."""
    return FBR_TAX_CATEGORIES.get(category.lower(), {})


def normalise_tx_type(raw_type: str) -> str:
    """Normalise a raw transaction type string to a standard value."""
    if not raw_type:
        return "unknown"
    return TX_TYPE_NORMALISATION.get(raw_type.strip(), raw_type.strip().lower())


def get_all_known_columns() -> List[Tuple[str, str, float]]:
    """
    Return all known column mappings as a flat list of (original, target, confidence).
    Used by the fuzzy matcher to build its lookup table.
    """
    results = []

    # Software-specific (highest confidence)
    for software, mapping in SOFTWARE_EXACT_MAPPINGS.items():
        for original, target in mapping.items():
            results.append((original, target, 0.95))

    # Urdu (high confidence — very specific terms)
    for original, target in URDU_MAPPINGS.items():
        results.append((original, target, 0.90))

    # General (medium confidence)
    for original, target in GENERAL_MAPPINGS.items():
        results.append((original, target, 0.80))

    # Abbreviations (lower confidence)
    for original, target in ABBREVIATION_MAPPINGS.items():
        results.append((original, target, 0.70))

    return results